"""GET /auth/me and POST /auth/switch-tenant."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.models.enums import MembershipRole
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.membership import TenantMembershipCreate
from app.schemas.tenant import TenantCreate
from tests.auth_helpers import create_tenant_user_membership, login

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_me_returns_current_user_tenant_and_role(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, user, _ = await create_tenant_user_membership(
        db_session,
        email="me@example.com",
        password=PASSWORD,
        tenant_slug="me-tenant",
        role=MembershipRole.SECURITY_ANALYST,
    )
    token = await login(auth_client, "me@example.com", PASSWORD)

    response = await auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token['access_token']}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(user.id)
    assert body["tenant_id"] == str(tenant.id)
    assert body["role"] == MembershipRole.SECURITY_ANALYST.value


async def test_me_rejects_missing_token(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_switch_tenant_issues_token_for_new_tenant(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant_a, user, _ = await create_tenant_user_membership(
        db_session, email="switch@example.com", password=PASSWORD, tenant_slug="switch-tenant-a"
    )
    tenant_b = await TenantRepository(db_session).create(
        TenantCreate(name="switch-tenant-b", slug="switch-tenant-b")
    )
    await TenantMembershipRepository(db_session).create(
        TenantMembershipCreate(
            tenant_id=tenant_b.id, user_id=user.id, role=MembershipRole.DEVELOPER
        )
    )
    token = await login(auth_client, "switch@example.com", PASSWORD, tenant_id=str(tenant_a.id))

    response = await auth_client.post(
        "/api/v1/auth/switch-tenant",
        json={"tenant_id": str(tenant_b.id)},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert response.status_code == 200
    payload = decode_token(response.json()["access_token"], expected_type="access")
    assert payload["tenant_id"] == str(tenant_b.id)
    assert payload["role"] == MembershipRole.DEVELOPER.value


async def test_switch_tenant_rejects_tenant_the_user_is_not_a_member_of(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="noaccess@example.com", password=PASSWORD, tenant_slug="noaccess-t"
    )
    other_tenant = await TenantRepository(db_session).create(
        TenantCreate(name="off-limits", slug="off-limits")
    )
    token = await login(auth_client, "noaccess@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/auth/switch-tenant",
        json={"tenant_id": str(other_tenant.id)},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert response.status_code == 403
