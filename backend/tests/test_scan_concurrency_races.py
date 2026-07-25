"""Real cross-connection concurrency races for scan state transitions —
the actual point of `ScanRepository.conditional_update`/
`claim_for_execution`'s SQL-level guards (see
docs/decisions/0008-transaction-and-concurrency-model.md).

Uses `real_session_factory` — genuinely independent connections, real
commits — since a single, SAVEPOINT-joined `db_session` shared by one
test cannot produce an actual race, only simulate one within a single
transaction (see test_scan_state_machine.py's module docstring).

Races here are deterministically sequenced, not `asyncio.gather`-raced:
the "losing" side's statement is made to block on Postgres's own row lock
(held by the winning side's still-uncommitted UPDATE) by starting it as a
background task, then the winner commits to release the lock and unblock
it — so which side wins is controlled by the test, not left to chance,
while the blocking/unblocking itself is real cross-connection contention.
"""

import asyncio

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.asset import Asset
from app.models.enums import AssetType, ScanStatus
from app.models.project import Project
from app.models.scan import Scan
from app.models.tenant import Tenant
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate, ScanUpdate
from app.schemas.tenant import TenantCreate


async def _make_scan(
    session_factory: async_sessionmaker[AsyncSession], slug: str, *, status: ScanStatus
):
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
        # Direct repository-level status manipulation (not through
        # ScanStateMachine) is fine here: these tests exercise the SQL
        # guard itself, not the state-machine's edge-validity rules.
        scan = await ScanRepository(session).update(scan, ScanUpdate(status=status))
        await session.commit()
        return tenant.id, project.id, asset.id, scan.id


async def _cleanup(
    real_session_factory: async_sessionmaker[AsyncSession],
    *,
    scan_id,
    asset_id,
    project_id,
    tenant_id,
) -> None:
    async with real_session_factory() as session:
        await session.execute(delete(Scan).where(Scan.id == scan_id))
        await session.execute(delete(Asset).where(Asset.id == asset_id))
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


async def test_only_one_of_two_concurrent_execution_claims_for_the_same_attempt_wins(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two independent workers deliver the same logical attempt (a
    duplicate/at-least-once Celery redelivery). Only one may claim
    execution ownership; the other must get a clean `None`, never a
    second, conflicting claim."""
    tenant_id, project_id, asset_id, scan_id = await _make_scan(
        real_session_factory, "race1", status=ScanStatus.QUEUED
    )

    try:
        session_a = real_session_factory()
        session_b = real_session_factory()
        try:
            claimed_a = await ScanRepository(session_a).claim_for_execution(
                tenant_id, scan_id, expected_attempt=0, worker_id="worker-a"
            )
            assert claimed_a is not None

            # session_b's UPDATE blocks on the row lock session_a's
            # (still uncommitted) UPDATE holds. Start it as a background
            # task so it can genuinely wait on the DB server rather than
            # deadlocking this coroutine.
            claim_b_task = asyncio.create_task(
                ScanRepository(session_b).claim_for_execution(
                    tenant_id, scan_id, expected_attempt=0, worker_id="worker-b"
                )
            )
            await asyncio.sleep(0.2)  # let session_b's query reach the server and block
            assert not claim_b_task.done()

            await session_a.commit()  # releases the lock

            claimed_b = await asyncio.wait_for(claim_b_task, timeout=5)
            await session_b.commit()
        finally:
            await session_a.close()
            await session_b.close()

        assert claimed_a.worker_id == "worker-a"
        assert claimed_a.status == ScanStatus.PREPARING
        assert claimed_a.attempt == 1
        # The loser sees the already-updated row (status != QUEUED, or
        # attempt already incremented) and matches zero rows.
        assert claimed_b is None

        async with real_session_factory() as verify:
            final = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert final is not None
            assert final.worker_id == "worker-a"
            assert final.attempt == 1
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_stale_claim_attempt_after_the_winner_already_committed_is_a_clean_noop(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The non-blocking case of the same race: a duplicate delivery that
    arrives *after* the winning claim already committed gets `None`
    immediately, no lock contention involved."""
    tenant_id, project_id, asset_id, scan_id = await _make_scan(
        real_session_factory, "race2", status=ScanStatus.QUEUED
    )

    try:
        async with real_session_factory() as session_a:
            claimed_a = await ScanRepository(session_a).claim_for_execution(
                tenant_id, scan_id, expected_attempt=0, worker_id="worker-a"
            )
            assert claimed_a is not None
            await session_a.commit()

        async with real_session_factory() as session_b:
            claimed_b = await ScanRepository(session_b).claim_for_execution(
                tenant_id, scan_id, expected_attempt=0, worker_id="worker-b"
            )
            assert claimed_b is None
            await session_b.commit()
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_completion_wins_a_race_against_a_concurrent_cancellation_and_cancel_is_rejected(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A worker finishing a scan races a `POST /scans/{id}/cancel`
    arriving for the same RUNNING scan. Whichever transition commits
    first wins; the other's conditional UPDATE matches zero rows and must
    never overwrite the terminal state that already won."""
    tenant_id, project_id, asset_id, scan_id = await _make_scan(
        real_session_factory, "race3", status=ScanStatus.RUNNING
    )

    try:
        completion_session = real_session_factory()
        cancel_session = real_session_factory()
        try:
            completed = await ScanRepository(completion_session).conditional_update(
                tenant_id,
                scan_id,
                expected_status=ScanStatus.RUNNING,
                data=ScanUpdate(status=ScanStatus.SUCCEEDED),
            )
            assert completed is not None

            cancel_task = asyncio.create_task(
                ScanRepository(cancel_session).conditional_update(
                    tenant_id,
                    scan_id,
                    expected_status=ScanStatus.RUNNING,
                    data=ScanUpdate(status=ScanStatus.CANCELLED),
                )
            )
            await asyncio.sleep(0.2)
            assert not cancel_task.done()

            await completion_session.commit()  # worker's completion wins

            cancelled = await asyncio.wait_for(cancel_task, timeout=5)
            await cancel_session.commit()
        finally:
            await completion_session.close()
            await cancel_session.close()

        # The cancel request's conditional UPDATE re-evaluates against the
        # now-committed SUCCEEDED row, matches zero rows, and must not
        # have overwritten it.
        assert cancelled is None

        async with real_session_factory() as verify:
            final = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert final is not None
            assert final.status == ScanStatus.SUCCEEDED
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_cancellation_wins_a_race_against_a_concurrent_completion_and_completion_is_rejected(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The mirror ordering of the previous test: a cancel request that
    commits first must leave the row CANCELLED, and the worker's later
    attempt to mark it SUCCEEDED must be rejected, not silently overwrite
    a state the caller has already been told is final."""
    tenant_id, project_id, asset_id, scan_id = await _make_scan(
        real_session_factory, "race4", status=ScanStatus.RUNNING
    )

    try:
        cancel_session = real_session_factory()
        completion_session = real_session_factory()
        try:
            cancelled = await ScanRepository(cancel_session).conditional_update(
                tenant_id,
                scan_id,
                expected_status=ScanStatus.RUNNING,
                data=ScanUpdate(status=ScanStatus.CANCELLED),
            )
            assert cancelled is not None

            completion_task = asyncio.create_task(
                ScanRepository(completion_session).conditional_update(
                    tenant_id,
                    scan_id,
                    expected_status=ScanStatus.RUNNING,
                    data=ScanUpdate(status=ScanStatus.SUCCEEDED),
                )
            )
            await asyncio.sleep(0.2)
            assert not completion_task.done()

            await cancel_session.commit()  # cancellation wins

            completed = await asyncio.wait_for(completion_task, timeout=5)
            await completion_session.commit()
        finally:
            await cancel_session.close()
            await completion_session.close()

        assert completed is None

        async with real_session_factory() as verify:
            final = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert final is not None
            assert final.status == ScanStatus.CANCELLED
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_conditional_update_against_an_already_terminal_row_is_rejected(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No lock contention needed for this one: the row is already
    committed FAILED (a genuinely separate, prior transaction) before a
    late writer — believing it's still RUNNING — tries to finalize it.
    Terminal states are never overwritten, from any connection, at any
    time after the fact."""
    tenant_id, project_id, asset_id, scan_id = await _make_scan(
        real_session_factory, "race5", status=ScanStatus.RUNNING
    )

    try:
        async with real_session_factory() as session_a:
            failed = await ScanRepository(session_a).conditional_update(
                tenant_id,
                scan_id,
                expected_status=ScanStatus.RUNNING,
                data=ScanUpdate(status=ScanStatus.FAILED, error_code="x", error_summary="x"),
            )
            assert failed is not None
            await session_a.commit()

        async with real_session_factory() as session_b:
            late = await ScanRepository(session_b).conditional_update(
                tenant_id,
                scan_id,
                expected_status=ScanStatus.RUNNING,
                data=ScanUpdate(status=ScanStatus.SUCCEEDED),
            )
            assert late is None
            await session_b.commit()

        async with real_session_factory() as verify:
            final = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert final is not None
            assert final.status == ScanStatus.FAILED
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
