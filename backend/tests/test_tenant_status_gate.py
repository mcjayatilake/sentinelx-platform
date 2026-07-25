"""`get_current_principal`'s tenant-active gate (`app.api.deps.
_ensure_tenant_active`) — previously only `TenantMembership.status` was
checked, so suspending/archiving a *tenant* had no effect on an
already-issued token. Applied identically to both the JWT and API-key
authentication paths. Also proves a client cannot override tenant
context via a header or body field — there simply is no such field
anywhere a request is parsed; `principal.tenant_id` always comes from a
server-verified source.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, TenantStatus
from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.api_key import APIKeyIssueRequest
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.tenant import TenantUpdate
from app.services.api_key_service import APIKeyService
from tests.auth_helpers import create_tenant_user_membership, login_and_get_headers

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_suspended_tenant_is_rejected_for_a_jwt_principal(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="suspend1@example.com", password=PASSWORD, tenant_slug="suspend-t1"
    )
    headers = await login_and_get_headers(auth_client, "suspend1@example.com", PASSWORD)

    # Token is valid and the membership is ACTIVE; only the tenant itself
    # is suspended after the token was already issued.
    await TenantRepository(db_session).update(tenant, TenantUpdate(status=TenantStatus.SUSPENDED))

    response = await auth_client.get("/api/v1/scans", headers=headers)
    assert response.status_code == 401


async def test_archived_tenant_is_rejected_for_a_jwt_principal(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="archive1@example.com", password=PASSWORD, tenant_slug="archive-t1"
    )
    headers = await login_and_get_headers(auth_client, "archive1@example.com", PASSWORD)

    await TenantRepository(db_session).update(tenant, TenantUpdate(status=TenantStatus.ARCHIVED))

    response = await auth_client.get("/api/v1/scans", headers=headers)
    assert response.status_code == 401


async def test_active_tenant_is_unaffected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sanity check the gate doesn't false-positive on the common case."""
    await create_tenant_user_membership(
        db_session, email="active1@example.com", password=PASSWORD, tenant_slug="active-t1"
    )
    headers = await login_and_get_headers(auth_client, "active1@example.com", PASSWORD)

    response = await auth_client.get("/api/v1/scans", headers=headers)
    assert response.status_code == 200


async def test_suspended_tenant_is_rejected_for_an_api_key_principal(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="suspend2@example.com", password=PASSWORD, tenant_slug="suspend-t2"
    )
    issued = await APIKeyService(APIKeyMetadataRepository(db_session)).issue(
        tenant_id=tenant.id,
        user_id=None,
        data=APIKeyIssueRequest(name="gate-key", scopes=["scans:view"]),
    )
    await TenantRepository(db_session).update(tenant, TenantUpdate(status=TenantStatus.SUSPENDED))

    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": issued.api_key})
    assert response.status_code == 401


async def test_client_supplied_tenant_header_never_changes_the_request_scope(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    """No endpoint reads a client-supplied tenant identifier from a
    header — `principal.tenant_id` always comes from the verified token.
    Proven by creating a scan in tenant A, then requesting the scan list
    as tenant A's principal with a header naming tenant B: the response
    must still be tenant A's own scan, completely unaffected by the
    header's presence or value."""
    tenant_a, _, _ = await create_tenant_user_membership(
        db_session, email="isolation-a@example.com", password=PASSWORD, tenant_slug="isolation-a"
    )
    tenant_b, _, _ = await create_tenant_user_membership(
        db_session, email="isolation-b@example.com", password=PASSWORD, tenant_slug="isolation-b"
    )
    project_a = await ProjectRepository(db_session).create(
        ProjectCreate(tenant_id=tenant_a.id, name="proj-a", slug="proj-a")
    )
    asset_a = await AssetRepository(db_session).create(
        AssetCreate(
            tenant_id=tenant_a.id,
            project_id=project_a.id,
            asset_type=AssetType.WEBSITE,
            name="asset-a",
            locator="https://a.example.com",
        )
    )
    headers = await login_and_get_headers(auth_client, "isolation-a@example.com", PASSWORD)

    create_response = await auth_client.post(
        "/api/v1/scans",
        json={
            "project_id": str(project_a.id),
            "asset_id": str(asset_a.id),
            "scanner_type": "fake",
        },
        headers=headers,
    )
    assert create_response.status_code == 201
    scan_id = create_response.json()["id"]

    # A plausible-looking (but entirely ignored) attempt to override
    # tenant context via a header, pointed at tenant B instead.
    spoofed_headers = {**headers, "X-Tenant-Id": str(tenant_b.id)}
    list_response = await auth_client.get("/api/v1/scans", headers=spoofed_headers)
    assert list_response.status_code == 200
    returned_ids = {item["id"] for item in list_response.json()["items"]}
    assert returned_ids == {scan_id}  # tenant A's scan, not affected by the header


async def test_client_supplied_tenant_body_field_is_ignored(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    """`ScanCreateRequest` has no `tenant_id` field at all — even a
    client that tries to inject one into the JSON body has nothing to
    inject into; the scan is created in the caller's own tenant
    regardless of what an extra, unrecognized `tenant_id` body field
    claims."""
    tenant_a, _, _ = await create_tenant_user_membership(
        db_session, email="isolation-c@example.com", password=PASSWORD, tenant_slug="isolation-c"
    )
    tenant_b, _, _ = await create_tenant_user_membership(
        db_session, email="isolation-d@example.com", password=PASSWORD, tenant_slug="isolation-d"
    )
    project_a = await ProjectRepository(db_session).create(
        ProjectCreate(tenant_id=tenant_a.id, name="proj-c", slug="proj-c")
    )
    asset_a = await AssetRepository(db_session).create(
        AssetCreate(
            tenant_id=tenant_a.id,
            project_id=project_a.id,
            asset_type=AssetType.WEBSITE,
            name="asset-c",
            locator="https://c.example.com",
        )
    )
    headers = await login_and_get_headers(auth_client, "isolation-c@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/scans",
        json={
            "project_id": str(project_a.id),
            "asset_id": str(asset_a.id),
            "scanner_type": "fake",
            "tenant_id": str(tenant_b.id),  # extra, unrecognized field
        },
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["tenant_id"] == str(tenant_a.id)
    assert body["tenant_id"] != str(tenant_b.id)
