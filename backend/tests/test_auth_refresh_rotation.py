"""POST /auth/refresh: rotation and reuse detection.

`test_replaying_a_rotated_refresh_token_revokes_the_entire_family` is the
important one here: presenting an already-rotated refresh token is the
signature of a stolen token, and must revoke every token descended from
the same login, not just reject the one replayed request.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.models.audit_event import AuditEvent
from app.models.enums import AuditOutcome, MembershipRole
from app.repositories.membership_repository import TenantMembershipRepository
from app.schemas.membership import TenantMembershipUpdate
from tests.auth_helpers import create_tenant_user_membership, login

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_refresh_returns_new_token_pair(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="rotate@example.com", password=PASSWORD, tenant_slug="rotate-tenant"
    )
    token = await login(auth_client, "rotate@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert response.status_code == 200
    new_token = response.json()

    assert new_token["access_token"] != token["access_token"]
    assert new_token["refresh_token"] != token["refresh_token"]
    # New access token still carries the same identity/tenant/role.
    old_payload = decode_token(token["access_token"], expected_type="access")
    new_payload = decode_token(new_token["access_token"], expected_type="access")
    assert new_payload["sub"] == old_payload["sub"]
    assert new_payload["tenant_id"] == old_payload["tenant_id"]


async def test_rotated_refresh_token_can_no_longer_be_used(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="onceonly@example.com", password=PASSWORD, tenant_slug="onceonly-t"
    )
    token = await login(auth_client, "onceonly@example.com", PASSWORD)

    first = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert first.status_code == 200

    # Using the OLD (already-rotated) token again must fail.
    second = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert second.status_code == 401


async def test_replaying_a_rotated_refresh_token_revokes_the_entire_family(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="stolen@example.com", password=PASSWORD, tenant_slug="stolen-tenant"
    )
    original = await login(auth_client, "stolen@example.com", PASSWORD)

    rotated_response = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original["refresh_token"]}
    )
    assert rotated_response.status_code == 200
    rotated = rotated_response.json()

    # Replay the OLD token (simulating an attacker who stole it before
    # the legitimate rotation happened).
    replay_response = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original["refresh_token"]}
    )
    assert replay_response.status_code == 401

    # The legitimate, newly-rotated token must ALSO now be dead — the
    # whole family was revoked, not just the replayed request rejected.
    legit_followup = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    )
    assert legit_followup.status_code == 401


async def test_refresh_token_reuse_writes_a_denied_audit_event(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="auditreuse@example.com", password=PASSWORD, tenant_slug="reuse-tenant"
    )
    original = await login(auth_client, "auditreuse@example.com", PASSWORD)
    await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original["refresh_token"]}
    )
    await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": original["refresh_token"]}
    )

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == user.id,
                AuditEvent.action == "auth.refresh_token_reuse_detected",
            )
        )
    ).scalar_one()
    assert event.outcome == AuditOutcome.DENIED


async def test_refresh_with_unknown_token_is_rejected(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": "not-a-real-token"}
    )
    assert response.status_code == 401


async def test_refresh_new_access_token_reflects_role_change_since_login(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    """A role change should take effect on the next refresh, not require a
    fresh login — the service re-reads the live membership row rather
    than trusting the role embedded in the presented refresh token."""
    tenant, user, membership = await create_tenant_user_membership(
        db_session,
        email="rolechange@example.com",
        password=PASSWORD,
        tenant_slug="rolechange-tenant",
        role=MembershipRole.VIEWER,
    )
    token = await login(auth_client, "rolechange@example.com", PASSWORD)

    await TenantMembershipRepository(db_session).update(
        membership, TenantMembershipUpdate(role=MembershipRole.ADMINISTRATOR)
    )

    refreshed = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert refreshed.status_code == 200
    payload = decode_token(refreshed.json()["access_token"], expected_type="access")
    assert payload["role"] == MembershipRole.ADMINISTRATOR.value
    assert user.id and tenant.id
