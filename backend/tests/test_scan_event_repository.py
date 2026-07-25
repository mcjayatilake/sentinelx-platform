"""`ScanEventRepository` — append-only creation, listing, and the
latest-progress lookup backing `GET /scans/{id}/progress`."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_event_repository import ScanEventRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.asset import AssetCreate
from app.schemas.common import PaginationParams
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.scan_event import ScanEventCreate
from app.schemas.tenant import TenantCreate


async def _make_scan(session: AsyncSession, slug: str):
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
    return tenant, scan


async def test_create_persists_event(db_session: AsyncSession) -> None:
    tenant, scan = await _make_scan(db_session, "se1")
    repo = ScanEventRepository(db_session)
    event = await repo.create(
        ScanEventCreate(
            tenant_id=tenant.id,
            scan_id=scan.id,
            event_type="scan.queued",
            correlation_id="corr-1",
        )
    )
    assert event.id is not None
    assert event.event_type == "scan.queued"


async def test_list_by_scan_orders_newest_first(db_session: AsyncSession) -> None:
    tenant, scan = await _make_scan(db_session, "se2")
    repo = ScanEventRepository(db_session)
    for stage, percent in [("running", 10), ("collecting", 60), ("processing", 85)]:
        await repo.create(
            ScanEventCreate(
                tenant_id=tenant.id,
                scan_id=scan.id,
                event_type="scan.progress",
                stage=stage,
                progress_percent=percent,
                correlation_id="corr-1",
            )
        )

    page = await repo.list_by_scan(tenant.id, scan.id, PaginationParams(limit=50, offset=0))
    assert page.total == 3
    assert [e.stage for e in page.items] == ["processing", "collecting", "running"]


async def test_list_by_scan_scoped_to_tenant_and_scan(db_session: AsyncSession) -> None:
    tenant_a, scan_a = await _make_scan(db_session, "se3a")
    tenant_b, scan_b = await _make_scan(db_session, "se3b")
    repo = ScanEventRepository(db_session)
    await repo.create(
        ScanEventCreate(
            tenant_id=tenant_a.id, scan_id=scan_a.id, event_type="scan.queued", correlation_id="c"
        )
    )
    await repo.create(
        ScanEventCreate(
            tenant_id=tenant_b.id, scan_id=scan_b.id, event_type="scan.queued", correlation_id="c"
        )
    )

    page = await repo.list_by_scan(tenant_a.id, scan_a.id, PaginationParams(limit=50, offset=0))
    assert page.total == 1


async def test_get_latest_progress_returns_none_when_no_progress_events(
    db_session: AsyncSession,
) -> None:
    tenant, scan = await _make_scan(db_session, "se4")
    repo = ScanEventRepository(db_session)
    await repo.create(
        ScanEventCreate(
            tenant_id=tenant.id, scan_id=scan.id, event_type="scan.queued", correlation_id="c"
        )
    )
    assert await repo.get_latest_progress(tenant.id, scan.id) is None


async def test_get_latest_progress_returns_the_most_recent_progress_event(
    db_session: AsyncSession,
) -> None:
    tenant, scan = await _make_scan(db_session, "se5")
    repo = ScanEventRepository(db_session)
    for stage, percent in [("running", 10), ("collecting", 60)]:
        await repo.create(
            ScanEventCreate(
                tenant_id=tenant.id,
                scan_id=scan.id,
                event_type="scan.progress",
                stage=stage,
                progress_percent=percent,
                correlation_id="c",
            )
        )
    # A non-progress event published later must not shadow the latest
    # progress event.
    await repo.create(
        ScanEventCreate(
            tenant_id=tenant.id, scan_id=scan.id, event_type="scan.completed", correlation_id="c"
        )
    )

    latest = await repo.get_latest_progress(tenant.id, scan.id)
    assert latest is not None
    assert latest.stage == "collecting"
    assert latest.progress_percent == 60
