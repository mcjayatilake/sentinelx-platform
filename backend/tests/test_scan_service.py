"""`ScanService` — the request-scoped scan lifecycle operations, tested
directly against a `FakeJobQueue` (no Celery/Redis dependency needed
here; `tests/test_scans_endpoints.py` already covers the real
`CeleryJobQueue` wiring end-to-end through the API)."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, ScanStatus
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.common import PaginationParams
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreateRequest, ScanUpdate
from app.schemas.tenant import TenantCreate
from app.services.scan_service import (
    AssetNotFoundError,
    AssetProjectMismatchError,
    ProjectNotFoundError,
    ScanNotCancellableError,
    ScanNotFoundError,
    ScanNotRetryableError,
    ScanService,
)
from tests.scan_engine_fakes import FakeJobQueue


async def _make_tenant_project_asset(session: AsyncSession, slug: str):
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
    return tenant, project, asset


def _service(session: AsyncSession, job_queue: FakeJobQueue | None = None) -> ScanService:
    return ScanService(session, job_queue or FakeJobQueue())


async def test_create_scan_transitions_to_queued_and_writes_an_outbox_row(
    db_session: AsyncSession,
) -> None:
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc1")
    job_queue = FakeJobQueue()
    scan = await _service(db_session, job_queue).create_scan(
        tenant.id,
        None,
        ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
    )
    assert scan.status == ScanStatus.QUEUED
    # No direct JobQueue call from the service — see _enqueue's
    # docstring: a separate dispatcher publishes the outbox row once
    # this transaction has committed.
    assert job_queue.enqueued == []
    outbox_rows = await ScanOutboxRepository(db_session).list_by_scan(tenant.id, scan.id)
    assert len(outbox_rows) == 1
    assert outbox_rows[0].scan_attempt == 0
    assert outbox_rows[0].published_at is None
    assert outbox_rows[0].payload["scan_id"] == str(scan.id)
    assert outbox_rows[0].payload["tenant_id"] == str(tenant.id)


async def test_create_scan_unknown_project_raises(db_session: AsyncSession) -> None:
    tenant, _, asset = await _make_tenant_project_asset(db_session, "svc2")
    with pytest.raises(ProjectNotFoundError):
        await _service(db_session).create_scan(
            tenant.id,
            None,
            ScanCreateRequest(
                project_id=tenant.id, asset_id=asset.id, scanner_type="fake"
            ),  # tenant.id is not a real project id
        )


async def test_create_scan_unknown_asset_raises(db_session: AsyncSession) -> None:
    tenant, project, _ = await _make_tenant_project_asset(db_session, "svc3")
    with pytest.raises(AssetNotFoundError):
        await _service(db_session).create_scan(
            tenant.id,
            None,
            ScanCreateRequest(project_id=project.id, asset_id=project.id, scanner_type="fake"),
        )


async def test_create_scan_asset_project_mismatch_raises(db_session: AsyncSession) -> None:
    tenant, project_a, _ = await _make_tenant_project_asset(db_session, "svc4a")
    # A second project + asset under the *same* tenant, so the mismatch is
    # genuinely "wrong project, right tenant" rather than a cross-tenant
    # 404 (already covered by test_create_scan_unknown_asset_raises-style
    # tenant scoping elsewhere).
    project_b = await ProjectRepository(db_session).create(
        ProjectCreate(tenant_id=tenant.id, name="svc4b", slug="svc4b")
    )
    asset_b = await AssetRepository(db_session).create(
        AssetCreate(
            tenant_id=tenant.id,
            project_id=project_b.id,
            asset_type=AssetType.WEBSITE,
            name="svc4b",
            locator="https://example.com",
        )
    )
    with pytest.raises(AssetProjectMismatchError):
        await _service(db_session).create_scan(
            tenant.id,
            None,
            ScanCreateRequest(project_id=project_a.id, asset_id=asset_b.id, scanner_type="fake"),
        )


async def test_get_scan_not_found_raises(db_session: AsyncSession) -> None:
    tenant, _, _ = await _make_tenant_project_asset(db_session, "svc5")
    with pytest.raises(ScanNotFoundError):
        await _service(db_session).get_scan(tenant.id, tenant.id)


async def test_list_scans_paginates_and_filters_by_status(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc6")
    service = _service(db_session)
    for _ in range(2):
        await service.create_scan(
            tenant.id,
            None,
            ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
        )

    page = await service.list_scans(tenant.id, PaginationParams(limit=50, offset=0))
    assert page.total == 2

    page_queued = await service.list_scans(
        tenant.id, PaginationParams(limit=50, offset=0), status=ScanStatus.QUEUED
    )
    assert page_queued.total == 2

    page_failed = await service.list_scans(
        tenant.id, PaginationParams(limit=50, offset=0), status=ScanStatus.FAILED
    )
    assert page_failed.total == 0


async def test_cancel_scan_before_dispatch_does_not_call_job_queue_cancel(
    db_session: AsyncSession,
) -> None:
    """`Scan.job_id` is only ever set by the outbox dispatcher, after a
    real publish (not exercised here — see
    tests/test_scan_outbox_dispatcher.py). Cancelling before that has
    happened has nothing to tell the queue to cancel; the scan's
    committed CANCELLED status is what a worker checks before it ever
    starts (see ScanOrchestrator.run's duplicate-or-stale-delivery
    guard), so this is still fully safe."""
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc7")
    job_queue = FakeJobQueue()
    service = _service(db_session, job_queue)
    scan = await service.create_scan(
        tenant.id,
        None,
        ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
    )
    scan_row = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert scan_row is not None
    assert scan_row.job_id is None

    cancelled = await service.cancel_scan(tenant.id, scan.id)
    assert cancelled.status == ScanStatus.CANCELLED
    assert job_queue.cancelled == []


async def test_cancel_scan_after_dispatch_cancels_the_queued_job(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc7b")
    job_queue = FakeJobQueue()
    service = _service(db_session, job_queue)
    scan = await service.create_scan(
        tenant.id,
        None,
        ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
    )
    # Simulates the outbox dispatcher having already published and
    # recorded a job id.
    scan_row = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert scan_row is not None
    await ScanRepository(db_session).update(scan_row, ScanUpdate(job_id="job-1"))
    await db_session.commit()

    cancelled = await service.cancel_scan(tenant.id, scan.id)
    assert cancelled.status == ScanStatus.CANCELLED
    assert job_queue.cancelled == ["job-1"]


async def test_cancel_terminal_scan_raises_not_cancellable(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc8")
    service = _service(db_session)
    scan = await service.create_scan(
        tenant.id,
        None,
        ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
    )
    await service.cancel_scan(tenant.id, scan.id)

    with pytest.raises(ScanNotCancellableError):
        await service.cancel_scan(tenant.id, scan.id)


async def test_retry_scan_requires_terminal_status(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc9")
    service = _service(db_session)
    scan = await service.create_scan(
        tenant.id,
        None,
        ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
    )
    with pytest.raises(ScanNotRetryableError):
        await service.retry_scan(tenant.id, scan.id, None)


async def test_retry_scan_creates_new_scan_with_lineage(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc10")
    scan_repo = ScanRepository(db_session)
    service = _service(db_session)
    scan = await service.create_scan(
        tenant.id,
        None,
        ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
    )

    machine = ScanStateMachine(db_session)
    original = await scan_repo.get_by_id(tenant.id, scan.id)
    assert original is not None
    original = await machine.transition(original, ScanStatus.PREPARING)
    await machine.transition(original, ScanStatus.FAILED, error_code="x", error_summary="x")

    retried = await service.retry_scan(tenant.id, scan.id, None)
    assert retried.id != scan.id
    assert retried.retry_of_scan_id == scan.id
    assert retried.status == ScanStatus.QUEUED


async def test_get_progress_defaults_when_no_progress_events(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_tenant_project_asset(db_session, "svc11")
    service = _service(db_session)
    scan = await service.create_scan(
        tenant.id,
        None,
        ScanCreateRequest(project_id=project.id, asset_id=asset.id, scanner_type="fake"),
    )

    progress = await service.get_progress(tenant.id, scan.id)
    assert progress.status == ScanStatus.QUEUED
    assert progress.percent == 0
    assert progress.stage == ScanStatus.QUEUED.value
