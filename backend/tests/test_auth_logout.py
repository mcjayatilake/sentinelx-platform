"""POST /auth/logout: refresh-family revocation + access-token denylist."""

import redis.asyncio as redis
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.models.audit_event import AuditEvent
from app.models.enums import AuditOutcome
from tests.auth_helpers import create_tenant_user_membership, login

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_logout_revokes_the_refresh_token(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="logout@example.com", password=PASSWORD, tenant_slug="logout-tenant"
    )
    token = await login(auth_client, "logout@example.com", PASSWORD)

    logout_response = await auth_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": token["refresh_token"]},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert logout_response.status_code == 204

    refresh_after_logout = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert refresh_after_logout.status_code == 401


async def test_logout_denylists_the_access_token(
    auth_client: AsyncClient, db_session: AsyncSession, redis_client: redis.Redis
) -> None:
    await create_tenant_user_membership(
        db_session, email="denylist@example.com", password=PASSWORD, tenant_slug="denylist-t"
    )
    token = await login(auth_client, "denylist@example.com", PASSWORD)

    me_before = await auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token['access_token']}"}
    )
    assert me_before.status_code == 200

    await auth_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": token["refresh_token"]},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )

    payload = decode_token(token["access_token"], expected_type="access")
    assert await redis_client.exists(f"auth:denylist:jti:{payload['jti']}")

    me_after = await auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token['access_token']}"}
    )
    assert me_after.status_code == 401


async def test_logout_writes_audit_event(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="logoutaudit@example.com", password=PASSWORD, tenant_slug="logaudit-t"
    )
    token = await login(auth_client, "logoutaudit@example.com", PASSWORD)

    await auth_client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": token["refresh_token"]},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == user.id, AuditEvent.action == "auth.logout"
            )
        )
    ).scalar_one()
    assert event.outcome == AuditOutcome.SUCCESS


async def test_logout_requires_authentication(auth_client: AsyncClient) -> None:
    response = await auth_client.post("/api/v1/auth/logout", json={"refresh_token": "irrelevant"})
    assert response.status_code == 401  # no Authorization header at all
