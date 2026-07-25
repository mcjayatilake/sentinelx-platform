"""Transactional-outbox atomicity: the outbox row and the scan's QUEUED
transition must land together or not at all — the actual point of
writing the outbox row on the same session, before the transition whose
commit makes both durable (see `app.services.scan_service._enqueue` and
`app.scan_engine.orchestrator._handle_failure`).

Uses `real_session_factory` for genuine cross-connection visibility
proofs — see test_scan_state_machine.py's module docstring for why a
single shared `db_session` can't demonstrate this.
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models.asset import Asset
from app.models.enums import AssetType, ScanStatus
from app.models.project import Project
from app.models.scan import Scan
from app.models.tenant import Tenant
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.bootstrap import build_orchestrator
from app.scan_engine.exceptions import StaleScanStateError, TransientScanError
from app.scan_engine.interfaces import ScannerCapabilities
from app.scan_engine.job_queue import ScanJobPayload, scan_job_payload_to_dict
from app.scan_engine.registry import ScannerRegistry
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.scan_outbox import ScanJobOutboxCreate
from app.schemas.tenant import TenantCreate
from tests.scan_engine_fakes import FakeJobQueue, FakePlugin


async def _make_scan(session_factory: async_sessionmaker[AsyncSession], slug: str):
    async with session_factory() as session:
        tenant = await TenantRepository(session).create(TenantCreate(name=slug, slug=slug))
        project = await ProjectRepository(session).create(
            ProjectCreate(tenant_id=tenant.id, name=slug, slug=slug)
        )
        asset = await AssetRepository(session).create(
            AssetCreate(
                tenant_id=tenant.id,
                project_id=project.id,
                asset_type=AssetType.WEBSITE,
                name=slug,
                locator="https://example.com",
            )
        )
        scan = await ScanRepository(session).create(
            ScanCreate(
                tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="fake"
            )
        )
        await session.commit()
        return tenant.id, project.id, asset.id, scan.id


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scan_id,
    asset_id,
    project_id,
    tenant_id,
) -> None:
    async with session_factory() as session:
        await session.execute(delete(Scan).where(Scan.id == scan_id))
        await session.execute(delete(Asset).where(Asset.id == asset_id))
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


async def test_outbox_row_and_queued_transition_commit_together_and_are_both_visible(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, project_id, asset_id, scan_id = await _make_scan(real_session_factory, "obdur1")

    try:
        async with real_session_factory() as session:
            scan = await ScanRepository(session).get_by_id(tenant_id, scan_id)
            assert scan is not None
            await ScanOutboxRepository(session).create(
                ScanJobOutboxCreate(
                    tenant_id=tenant_id,
                    scan_id=scan_id,
                    scan_attempt=scan.attempt,
                    payload=scan_job_payload_to_dict(
                        ScanJobPayload(
                            scan_id=scan_id, tenant_id=tenant_id, correlation_id="corr-1"
                        )
                    ),
                )
            )
            # This commits both the outbox insert above (flush-only
            # until now) and the QUEUED transition together, in one
            # transaction.
            await ScanStateMachine(session).transition(scan, ScanStatus.QUEUED)

        async with real_session_factory() as verify:
            scan = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert scan is not None
            assert scan.status == ScanStatus.QUEUED

            outbox_rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert len(outbox_rows) == 1
            assert outbox_rows[0].scan_attempt == 0
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_a_rolled_back_enqueue_leaves_neither_the_outbox_row_nor_the_status_change(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Simulates the request-boundary rollback `get_db()` performs on any
    exception (see test_transaction_commit_boundary.py for that proof at
    the request layer). A genuinely separate connection moves the scan
    out from under session A's stale, already-loaded belief (PENDING) —
    a real race, not a same-session forced mutation, which a default
    `autoflush=True` session (like `real_session_factory`'s) would
    silently make un-observable by flushing the forced mutation before
    the conditional UPDATE's own WHERE clause runs (see
    test_scan_state_machine.py's module docstring for that pitfall).
    session A's outbox insert, flushed but never committed before the
    resulting `StaleScanStateError`, must not survive the rollback."""
    tenant_id, project_id, asset_id, scan_id = await _make_scan(real_session_factory, "obdur2")

    try:
        async with real_session_factory() as session_a:
            scan = await ScanRepository(session_a).get_by_id(tenant_id, scan_id)
            assert scan is not None
            assert scan.status == ScanStatus.PENDING

            await ScanOutboxRepository(session_a).create(
                ScanJobOutboxCreate(
                    tenant_id=tenant_id,
                    scan_id=scan_id,
                    scan_attempt=scan.attempt,
                    payload=scan_job_payload_to_dict(
                        ScanJobPayload(
                            scan_id=scan_id, tenant_id=tenant_id, correlation_id="corr-2"
                        )
                    ),
                )
            )

            # A genuinely separate connection moves the real row to
            # QUEUED and commits, out from under session_a's still-PENDING
            # belief.
            async with real_session_factory() as session_b:
                scan_b = await ScanRepository(session_b).get_by_id(tenant_id, scan_id)
                assert scan_b is not None
                await ScanStateMachine(session_b).transition(scan_b, ScanStatus.QUEUED)

            with pytest.raises(StaleScanStateError):
                await ScanStateMachine(session_a).transition(scan, ScanStatus.QUEUED)
            await session_a.rollback()

        async with real_session_factory() as verify:
            scan = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert scan is not None
            # session_b's commit stands; session_a's rollback undid only
            # its own (never-committed) work.
            assert scan.status == ScanStatus.QUEUED

            outbox_rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert outbox_rows == []  # session_a's flushed insert never committed
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_retry_writes_a_new_outbox_row_for_the_next_attempt_atomically_with_requeue(
    real_session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """The orchestrator's retry path (`_handle_failure`), exercised
    end-to-end with a real `FakePlugin` transient failure and a genuinely
    separate verifying connection — the same atomicity proof as the
    initial-enqueue test above, but for the retry call site."""
    tenant_id, project_id, asset_id, scan_id = await _make_scan(real_session_factory, "obdur3")

    try:
        async with real_session_factory() as session:
            scan = await ScanRepository(session).get_by_id(tenant_id, scan_id)
            assert scan is not None
            await ScanStateMachine(session).transition(scan, ScanStatus.QUEUED)

        registry = ScannerRegistry()
        registry.register(
            ScannerCapabilities(
                scanner_type="fake",
                display_name="Fake Scanner",
                supported_asset_types=frozenset({AssetType.WEBSITE}),
                default_timeout_seconds=60,
            ),
            lambda: FakePlugin(
                raise_in="execute", error_to_raise=TransientScanError("network blip")
            ),
        )
        settings = get_settings().model_copy(update={"artifact_storage_local_path": str(tmp_path)})
        async with real_session_factory() as session:
            orchestrator = build_orchestrator(
                session,
                registry=registry,
                job_queue=FakeJobQueue(),
                settings=settings,
            )
            await orchestrator.run(tenant_id, scan_id, worker_id="worker-1")

        async with real_session_factory() as verify:
            scan = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert scan is not None
            assert scan.status == ScanStatus.QUEUED
            assert scan.attempt == 1

            outbox_rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert len(outbox_rows) == 1
            assert outbox_rows[0].scan_attempt == 1
            assert outbox_rows[0].published_at is None
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
