"""Proves the actual point of `ScanStateMachine.transition()` committing
immediately at each stage boundary (see `app.scan_engine.state_machine`
and `app.scan_engine.orchestrator`) rather than holding one long
transaction for a whole run: mid-run status/progress is visible from a
genuinely separate connection *before* the run finishes, and whatever
already committed survives a simulated worker crash later in the run.

Uses `real_session_factory` (independent connections, real commits) —
`db_session` is a single SAVEPOINT-joined connection shared by the whole
test, which cannot demonstrate cross-connection visibility (see
test_scan_state_machine.py's module docstring).
"""

import asyncio
import contextlib

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
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.bootstrap import build_orchestrator
from app.scan_engine.interfaces import ScannerCapabilities
from app.scan_engine.registry import ScannerRegistry
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.tenant import TenantCreate
from tests.scan_engine_fakes import FakeJobQueue, FakePlugin


async def _make_queued_scan(session_factory: async_sessionmaker[AsyncSession], slug: str):
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
        scan = await ScanStateMachine(session).transition(scan, ScanStatus.QUEUED)
        return tenant.id, project.id, asset.id, scan.id


def _registry(plugin_factory) -> ScannerRegistry:
    registry = ScannerRegistry()
    capabilities = ScannerCapabilities(
        scanner_type="fake",
        display_name="Fake Scanner",
        supported_asset_types=frozenset({AssetType.WEBSITE}),
        default_timeout_seconds=60,
    )
    registry.register(capabilities, plugin_factory)
    return registry


def _settings_with_artifact_dir(tmp_path):
    return get_settings().model_copy(update={"artifact_storage_local_path": str(tmp_path)})


async def _cleanup(
    real_session_factory: async_sessionmaker[AsyncSession],
    *,
    scan_id,
    asset_id,
    project_id,
    tenant_id,
) -> None:
    async with real_session_factory() as session:
        # ScanEvent cascade-deletes with its Scan (ON DELETE CASCADE);
        # asset/project/tenant are RESTRICT-protected, so they must be
        # deleted in dependency order.
        await session.execute(delete(Scan).where(Scan.id == scan_id))
        await session.execute(delete(Asset).where(Asset.id == asset_id))
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


async def test_running_status_is_visible_from_a_separate_connection_before_the_run_finishes(
    real_session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    tenant_id, project_id, asset_id, scan_id = await _make_queued_scan(
        real_session_factory, "durable1"
    )

    reached_execute = asyncio.Event()
    continue_execute = asyncio.Event()

    async def _pause_in_execute() -> None:
        reached_execute.set()
        await continue_execute.wait()

    plugin = FakePlugin(hooks={"execute": _pause_in_execute})
    job_queue = FakeJobQueue()

    try:
        run_session = real_session_factory()
        orchestrator = build_orchestrator(
            run_session,
            registry=_registry(lambda: plugin),
            job_queue=job_queue,
            settings=_settings_with_artifact_dir(tmp_path),
        )
        try:
            run_task = asyncio.create_task(
                orchestrator.run(tenant_id, scan_id, worker_id="worker-1")
            )
            # The plugin blocks partway through execute(); by the time it
            # gets there, the RUNNING transition (and its commit — see
            # ScanStateMachine.transition) has already happened.
            await asyncio.wait_for(reached_execute.wait(), timeout=5)

            # A genuinely separate connection must see RUNNING *before*
            # the run has finished — the whole point of committing at
            # each stage boundary instead of once at the very end.
            async with real_session_factory() as observer:
                observed = await ScanRepository(observer).get_by_id(tenant_id, scan_id)
                assert observed is not None
                assert observed.status == ScanStatus.RUNNING
                assert observed.started_at is not None

            continue_execute.set()
            await asyncio.wait_for(run_task, timeout=5)
        finally:
            await run_session.close()

        async with real_session_factory() as final:
            updated = await ScanRepository(final).get_by_id(tenant_id, scan_id)
            assert updated is not None
            assert updated.status == ScanStatus.SUCCEEDED
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_partial_progress_survives_a_simulated_worker_crash_mid_run(
    real_session_factory: async_sessionmaker[AsyncSession], tmp_path
) -> None:
    """A worker process that dies mid-run (here: its task is cancelled and
    its session/connection dropped, standing in for a killed process)
    must not lose stages that already committed — the row is left durably
    at its last-committed status, a legitimate state for a future
    stuck-scan reaper to find (see docs/orchestrator.md), not silently
    reverted."""
    tenant_id, project_id, asset_id, scan_id = await _make_queued_scan(
        real_session_factory, "durable2"
    )

    reached_execute = asyncio.Event()
    never_continues = asyncio.Event()

    async def _pause_in_execute() -> None:
        reached_execute.set()
        await never_continues.wait()

    plugin = FakePlugin(hooks={"execute": _pause_in_execute})
    job_queue = FakeJobQueue()

    try:
        run_session = real_session_factory()
        orchestrator = build_orchestrator(
            run_session,
            registry=_registry(lambda: plugin),
            job_queue=job_queue,
            settings=_settings_with_artifact_dir(tmp_path),
        )
        run_task = asyncio.create_task(orchestrator.run(tenant_id, scan_id, worker_id="worker-1"))
        await asyncio.wait_for(reached_execute.wait(), timeout=5)

        # Simulate the worker process dying: cancel its task and drop its
        # connection without ever reaching the later transitions/commits.
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
        await run_session.close()

        async with real_session_factory() as observer:
            observed = await ScanRepository(observer).get_by_id(tenant_id, scan_id)
            assert observed is not None
            # RUNNING committed before the crash; COLLECTING/PROCESSING/
            # SUCCEEDED never happened — the row reflects exactly the last
            # thing that was durably committed, nothing more.
            assert observed.status == ScanStatus.RUNNING
            assert observed.started_at is not None
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
