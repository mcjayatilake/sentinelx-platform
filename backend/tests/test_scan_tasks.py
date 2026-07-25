"""`app.workers.tasks.scan_tasks` — the Celery entrypoint that wires a
fresh per-invocation engine to `ScanOrchestrator`.

Deliberately does NOT use the `db_session` fixture: `_run_async` opens
its *own* engine/connection against `settings.database_url` (that's the
whole point — see the module's own docstring on the event-loop-per-
invocation pitfall), so it cannot see uncommitted rows held in another
connection's transaction. Test data is created and *committed* on a
separate connection here, then explicitly cleaned up in `finally`
(scan_events cascade-delete with their scan; asset/project/tenant are
RESTRICT-protected, so they're deleted in dependency order).
"""

import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.tenant import TenantCreate
from app.workers.tasks.scan_tasks import _run_async, execute_scan


async def test_run_async_finalizes_failed_for_unregistered_scanner() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    slug = f"scantask-{uuid.uuid4().hex[:8]}"
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
        scan_repo = ScanRepository(session)
        scan = await scan_repo.create(
            ScanCreate(
                tenant_id=tenant.id,
                project_id=project.id,
                asset_id=asset.id,
                # No scanner is registered this phase
                # (build_default_registry() is empty), so this
                # deliberately exercises the scanner-not-registered ->
                # FAILED path end-to-end through the real
                # engine/session/orchestrator wiring.
                scanner_type="does-not-exist",
            )
        )
        scan = await ScanStateMachine(session).transition(scan, ScanStatus.QUEUED)
        scan_id, tenant_id = scan.id, tenant.id

    try:
        await _run_async(scan_id, tenant_id, correlation_id="corr-1", worker_id="worker-1")

        async with session_factory() as session:
            updated = await ScanRepository(session).get_by_id(tenant_id, scan_id)
            assert updated is not None
            assert updated.status == ScanStatus.FAILED
            assert updated.error_code == "scanner_not_registered"
    finally:
        async with session_factory() as session:
            # scan_events cascade-delete with their scan (ON DELETE
            # CASCADE); asset/project/tenant are RESTRICT-protected, so
            # they must be deleted in dependency order.
            await session.execute(delete(Scan).where(Scan.id == scan_id))
            await session.execute(delete(Asset).where(Asset.id == asset.id))
            await session.execute(delete(Project).where(Project.id == project.id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()
        await engine.dispose()


def test_execute_scan_task_is_registered_with_expected_name() -> None:
    assert execute_scan.name == "app.workers.tasks.scan_tasks.execute_scan"
