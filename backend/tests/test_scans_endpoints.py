"""Scan orchestration endpoints: create, get, list, cancel, retry,
progress — permission gating, tenant isolation, and lifecycle error
mapping (`app.core.error_handlers`)."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, MembershipRole, ScanStatus
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from tests.auth_helpers import create_tenant_user_membership, login_and_get_headers

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def _make_project_and_asset(session: AsyncSession, tenant_id, slug: str):
    project = await ProjectRepository(session).create(
        ProjectCreate(tenant_id=tenant_id, name=slug, slug=slug)
    )
    asset = await AssetRepository(session).create(
        AssetCreate(
            tenant_id=tenant_id,
            project_id=project.id,
            asset_type=AssetType.WEBSITE,
            name=slug,
            locator="https://example.com",
        )
    )
    return project, asset


async def test_create_scan_returns_201_and_queues_it(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="owner@example.com", password=PASSWORD, tenant_slug="scans-create-t"
    )
    project, asset = await _make_project_and_asset(db_session, tenant.id, "scans-create")
    headers = await login_and_get_headers(auth_client, "owner@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/scans",
        json={
            "project_id": str(project.id),
            "asset_id": str(asset.id),
            "scanner_type": "fake",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == ScanStatus.QUEUED.value
    assert body["scanner_type"] == "fake"
    assert "job_id" not in body


async def test_create_scan_denied_for_viewer(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session,
        email="viewer@example.com",
        password=PASSWORD,
        tenant_slug="scans-viewer-t",
        role=MembershipRole.VIEWER,
    )
    project, asset = await _make_project_and_asset(db_session, tenant.id, "scans-viewer")
    headers = await login_and_get_headers(auth_client, "viewer@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/scans",
        json={"project_id": str(project.id), "asset_id": str(asset.id), "scanner_type": "fake"},
        headers=headers,
    )
    assert response.status_code == 403


async def test_create_scan_unknown_project_returns_404(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="owner2@example.com", password=PASSWORD, tenant_slug="scans-404proj-t"
    )
    _, asset = await _make_project_and_asset(db_session, tenant.id, "scans-404proj")
    headers = await login_and_get_headers(auth_client, "owner2@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/scans",
        json={
            "project_id": "00000000-0000-0000-0000-000000000000",
            "asset_id": str(asset.id),
            "scanner_type": "fake",
        },
        headers=headers,
    )
    assert response.status_code == 404


async def test_create_scan_asset_not_in_project_returns_400(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="owner3@example.com", password=PASSWORD, tenant_slug="scans-mismatch-t"
    )
    project_a, _ = await _make_project_and_asset(db_session, tenant.id, "scans-mismatch-a")
    _, asset_b = await _make_project_and_asset(db_session, tenant.id, "scans-mismatch-b")
    headers = await login_and_get_headers(auth_client, "owner3@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/scans",
        json={
            "project_id": str(project_a.id),
            "asset_id": str(asset_b.id),
            "scanner_type": "fake",
        },
        headers=headers,
    )
    assert response.status_code == 400


async def test_get_scan_and_list_scans(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="owner4@example.com", password=PASSWORD, tenant_slug="scans-list-t"
    )
    project, asset = await _make_project_and_asset(db_session, tenant.id, "scans-list")
    headers = await login_and_get_headers(auth_client, "owner4@example.com", PASSWORD)

    create_response = await auth_client.post(
        "/api/v1/scans",
        json={"project_id": str(project.id), "asset_id": str(asset.id), "scanner_type": "fake"},
        headers=headers,
    )
    scan_id = create_response.json()["id"]

    get_response = await auth_client.get(f"/api/v1/scans/{scan_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == scan_id

    list_response = await auth_client.get("/api/v1/scans", headers=headers)
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 1


async def test_get_unknown_scan_returns_404(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="owner5@example.com", password=PASSWORD, tenant_slug="scans-404-t"
    )
    headers = await login_and_get_headers(auth_client, "owner5@example.com", PASSWORD)

    response = await auth_client.get(
        "/api/v1/scans/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert response.status_code == 404


async def test_cancel_scan_succeeds_then_conflicts_on_second_cancel(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="owner6@example.com", password=PASSWORD, tenant_slug="scans-cancel-t"
    )
    project, asset = await _make_project_and_asset(db_session, tenant.id, "scans-cancel")
    headers = await login_and_get_headers(auth_client, "owner6@example.com", PASSWORD)

    create_response = await auth_client.post(
        "/api/v1/scans",
        json={"project_id": str(project.id), "asset_id": str(asset.id), "scanner_type": "fake"},
        headers=headers,
    )
    scan_id = create_response.json()["id"]

    cancel_response = await auth_client.post(f"/api/v1/scans/{scan_id}/cancel", headers=headers)
    assert cancel_response.status_code == 200
    assert cancel_response.json()["status"] == ScanStatus.CANCELLED.value

    second_cancel = await auth_client.post(f"/api/v1/scans/{scan_id}/cancel", headers=headers)
    assert second_cancel.status_code == 409


async def test_retry_scan_requires_terminal_failure_state(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="owner7@example.com", password=PASSWORD, tenant_slug="scans-retry-t"
    )
    project, asset = await _make_project_and_asset(db_session, tenant.id, "scans-retry")
    headers = await login_and_get_headers(auth_client, "owner7@example.com", PASSWORD)

    create_response = await auth_client.post(
        "/api/v1/scans",
        json={"project_id": str(project.id), "asset_id": str(asset.id), "scanner_type": "fake"},
        headers=headers,
    )
    scan_id = create_response.json()["id"]

    # Freshly created scan is QUEUED, not a terminal failure state.
    retry_response = await auth_client.post(f"/api/v1/scans/{scan_id}/retry", headers=headers)
    assert retry_response.status_code == 409

    # Drive it to FAILED directly via the state machine, then retry.
    scan_repo = ScanRepository(db_session)
    scan = await scan_repo.get_by_id(tenant.id, scan_id)
    assert scan is not None
    machine = ScanStateMachine(db_session)
    scan = await machine.transition(scan, ScanStatus.PREPARING)
    await machine.transition(scan, ScanStatus.FAILED, error_code="x", error_summary="x")

    retry_response = await auth_client.post(f"/api/v1/scans/{scan_id}/retry", headers=headers)
    assert retry_response.status_code == 201
    body = retry_response.json()
    assert body["retry_of_scan_id"] == scan_id
    assert body["status"] == ScanStatus.QUEUED.value


async def test_progress_endpoint_returns_defaults_before_any_events(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="owner8@example.com", password=PASSWORD, tenant_slug="scans-progress-t"
    )
    project, asset = await _make_project_and_asset(db_session, tenant.id, "scans-progress")
    headers = await login_and_get_headers(auth_client, "owner8@example.com", PASSWORD)

    create_response = await auth_client.post(
        "/api/v1/scans",
        json={"project_id": str(project.id), "asset_id": str(asset.id), "scanner_type": "fake"},
        headers=headers,
    )
    scan_id = create_response.json()["id"]

    response = await auth_client.get(f"/api/v1/scans/{scan_id}/progress", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["scan_id"] == scan_id
    assert body["status"] == ScanStatus.QUEUED.value
    assert body["percent"] == 0


async def test_scan_from_another_tenant_is_not_visible(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant_a, _, _ = await create_tenant_user_membership(
        db_session, email="tenant-a@example.com", password=PASSWORD, tenant_slug="scans-iso-a"
    )
    tenant_b, _, _ = await create_tenant_user_membership(
        db_session, email="tenant-b@example.com", password=PASSWORD, tenant_slug="scans-iso-b"
    )
    project_a, asset_a = await _make_project_and_asset(db_session, tenant_a.id, "scans-iso")

    scan = await ScanRepository(db_session).create(
        ScanCreate(
            tenant_id=tenant_a.id,
            project_id=project_a.id,
            asset_id=asset_a.id,
            scanner_type="fake",
        )
    )

    headers_b = await login_and_get_headers(auth_client, "tenant-b@example.com", PASSWORD)
    response = await auth_client.get(f"/api/v1/scans/{scan.id}", headers=headers_b)
    assert response.status_code == 404
