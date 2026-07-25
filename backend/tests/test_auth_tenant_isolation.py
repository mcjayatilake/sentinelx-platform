"""A valid access token for tenant A must never reach tenant B's data.

Every endpoint scopes its repository queries by `principal.tenant_id`
(resolved server-side from the verified token, never from a client-
supplied path/query parameter) — these tests attack that boundary
directly by presenting tenant A's token against tenant B's resources.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MembershipRole, MembershipStatus
from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.repositories.membership_repository import TenantMembershipRepository
from app.schemas.api_key import APIKeyIssueRequest
from app.schemas.membership import TenantMembershipUpdate
from app.services.api_key_service import APIKeyService
from tests.auth_helpers import create_tenant_user_membership, login_and_get_headers

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_tenant_a_token_cannot_see_tenant_b_api_keys(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant_a, _, _ = await create_tenant_user_membership(
        db_session, email="isoa@example.com", password=PASSWORD, tenant_slug="iso-tenant-a"
    )
    tenant_b, user_b, _ = await create_tenant_user_membership(
        db_session, email="isob@example.com", password=PASSWORD, tenant_slug="iso-tenant-b"
    )
    key_b = await APIKeyService(APIKeyMetadataRepository(db_session)).issue(
        tenant_id=tenant_b.id, user_id=user_b.id, data=APIKeyIssueRequest(name="tenant-b-key")
    )

    headers_a = await login_and_get_headers(auth_client, "isoa@example.com", PASSWORD)

    list_response = await auth_client.get("/api/v1/api-keys", headers=headers_a)
    assert list_response.json()["total"] == 0

    revoke_response = await auth_client.post(
        f"/api/v1/api-keys/{key_b.id}/revoke", headers=headers_a
    )
    assert revoke_response.status_code == 404
    assert tenant_a.id


async def test_tenant_a_token_cannot_see_tenant_b_memberships(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="isomema@example.com", password=PASSWORD, tenant_slug="iso-mem-a"
    )
    tenant_b, user_b, membership_b = await create_tenant_user_membership(
        db_session, email="isomemb@example.com", password=PASSWORD, tenant_slug="iso-mem-b"
    )
    headers_a = await login_and_get_headers(auth_client, "isomema@example.com", PASSWORD)

    list_response = await auth_client.get("/api/v1/memberships", headers=headers_a)
    assert list_response.json()["total"] == 1  # only tenant A's own owner membership

    patch_response = await auth_client.patch(
        f"/api/v1/memberships/{membership_b.id}",
        json={"role": MembershipRole.ADMINISTRATOR.value},
        headers=headers_a,
    )
    assert patch_response.status_code == 404
    assert tenant_b.id and user_b.id


async def test_tenant_a_token_cannot_see_tenant_b_audit_events(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="isoaudita@example.com",
        password=PASSWORD,
        tenant_slug="iso-audit-a",
        role=MembershipRole.SECURITY_ANALYST,
    )
    await create_tenant_user_membership(
        db_session,
        email="isoauditb@example.com",
        password=PASSWORD,
        tenant_slug="iso-audit-b",
        role=MembershipRole.SECURITY_ANALYST,
    )
    # Generates an auth.login AuditEvent scoped to tenant B.
    await login_and_get_headers(auth_client, "isoauditb@example.com", PASSWORD)

    headers_a = await login_and_get_headers(auth_client, "isoaudita@example.com", PASSWORD)
    response = await auth_client.get("/api/v1/audit-events", headers=headers_a)
    body = response.json()

    assert all("isoauditb" not in str(item) for item in body["items"])


async def test_switch_tenant_cannot_be_used_to_join_a_tenant_without_membership(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="noswitch@example.com", password=PASSWORD, tenant_slug="noswitch-a"
    )
    tenant_b, _, _ = await create_tenant_user_membership(
        db_session, email="tenantbowner@example.com", password=PASSWORD, tenant_slug="noswitch-b"
    )
    headers = await login_and_get_headers(auth_client, "noswitch@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/auth/switch-tenant", json={"tenant_id": str(tenant_b.id)}, headers=headers
    )
    assert response.status_code == 403


async def test_removed_membership_immediately_loses_tenant_access(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A revoked membership must deny access on the very next request
    that resolves `CurrentPrincipalDep` — not wait for the access token
    to expire — because `get_current_principal` re-checks the live
    membership row on every request."""
    tenant, user, membership = await create_tenant_user_membership(
        db_session, email="revokedaccess@example.com", password=PASSWORD, tenant_slug="revoked-t"
    )
    headers = await login_and_get_headers(auth_client, "revokedaccess@example.com", PASSWORD)

    me_before = await auth_client.get("/api/v1/auth/me", headers=headers)
    assert me_before.status_code == 200

    await TenantMembershipRepository(db_session).update(
        membership, TenantMembershipUpdate(status=MembershipStatus.REMOVED)
    )

    me_after = await auth_client.get("/api/v1/auth/me", headers=headers)
    assert me_after.status_code == 401
    assert tenant.id and user.id
