"""GET /audit-events: permission-gated, tenant-scoped read."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MembershipRole
from tests.auth_helpers import create_tenant_user_membership, login_and_get_headers

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_security_analyst_can_view_audit_events(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="analyst@example.com",
        password=PASSWORD,
        tenant_slug="analyst-audit-t",
        role=MembershipRole.SECURITY_ANALYST,
    )
    headers = await login_and_get_headers(auth_client, "analyst@example.com", PASSWORD)

    # The login above itself produced an auth.login AuditEvent for this tenant.
    response = await auth_client.get("/api/v1/audit-events", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    assert any(item["action"] == "auth.login" for item in body["items"])


async def test_viewer_cannot_view_audit_events(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="restricted-viewer@example.com",
        password=PASSWORD,
        tenant_slug="restricted-audit-t",
        role=MembershipRole.VIEWER,
    )
    headers = await login_and_get_headers(auth_client, "restricted-viewer@example.com", PASSWORD)

    response = await auth_client.get("/api/v1/audit-events", headers=headers)
    assert response.status_code == 403


async def test_developer_cannot_view_audit_events(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="dev-audit@example.com",
        password=PASSWORD,
        tenant_slug="dev-audit-t",
        role=MembershipRole.DEVELOPER,
    )
    headers = await login_and_get_headers(auth_client, "dev-audit@example.com", PASSWORD)

    response = await auth_client.get("/api/v1/audit-events", headers=headers)
    assert response.status_code == 403
