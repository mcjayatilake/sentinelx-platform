"""Password change, session listing/revocation, breached-password hook,
transparent rehash-on-login, and refresh-time user/membership checks.

Each of these is a distinct production-facing behavior in its own right
(not written merely to pad coverage), grouped here because none needed a
whole file of its own.
"""

import uuid

from httpx import AsyncClient
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import PasswordPolicyError, create_token, verify_password
from app.models.enums import MembershipStatus, UserStatus
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterRequest
from app.schemas.membership import TenantMembershipUpdate
from app.schemas.user import UserUpdate
from app.services.auth_service import AuthService, InvalidCredentialsError
from tests.auth_helpers import (
    AlwaysBreachedPasswordChecker,
    create_tenant_user_membership,
    login,
    login_and_get_headers,
)

PASSWORD = "Correct-Horse-Battery-Staple-9!"
NEW_PASSWORD = "Even-Stronger-Password-42!"


async def test_change_password_endpoint_succeeds_and_invalidates_old_password(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="changepw@example.com", password=PASSWORD, tenant_slug="changepw-t"
    )
    headers = await login_and_get_headers(auth_client, "changepw@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/auth/password/change",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert response.status_code == 204

    await db_session.refresh(user)
    assert verify_password(NEW_PASSWORD, user.hashed_password or "")
    assert not verify_password(PASSWORD, user.hashed_password or "")


async def test_change_password_rejects_wrong_current_password(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="wrongcurrent@example.com", password=PASSWORD, tenant_slug="wrongcur-t"
    )
    headers = await login_and_get_headers(auth_client, "wrongcurrent@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/auth/password/change",
        json={"current_password": "not-the-current-password", "new_password": NEW_PASSWORD},
        headers=headers,
    )
    assert response.status_code == 401


async def test_change_password_service_raises_for_user_with_no_password(
    db_session: AsyncSession,
) -> None:
    tenant, user, _ = await create_tenant_user_membership(
        db_session, email="nopass@example.com", password=PASSWORD, tenant_slug="nopass-t"
    )
    await UserRepository(db_session).set_password(user, "", invalidate_sessions=False)
    user.hashed_password = None
    await db_session.flush()

    service = AuthService(db_session)
    try:
        await service.change_password(user, PASSWORD, NEW_PASSWORD)
        raise AssertionError("expected InvalidCredentialsError")
    except InvalidCredentialsError:
        pass
    assert tenant.id


async def test_list_sessions_returns_active_session(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="sessionlist@example.com", password=PASSWORD, tenant_slug="sesslist-t"
    )
    headers = await login_and_get_headers(auth_client, "sessionlist@example.com", PASSWORD)

    response = await auth_client.get("/api/v1/auth/sessions", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert "id" in body[0]


async def test_revoke_one_session_by_id(auth_client: AsyncClient, db_session: AsyncSession) -> None:
    await create_tenant_user_membership(
        db_session, email="revokeone@example.com", password=PASSWORD, tenant_slug="revokeone-t"
    )
    headers = await login_and_get_headers(auth_client, "revokeone@example.com", PASSWORD)
    sessions = (await auth_client.get("/api/v1/auth/sessions", headers=headers)).json()
    session_id = sessions[0]["id"]

    response = await auth_client.delete(f"/api/v1/auth/sessions/{session_id}", headers=headers)
    assert response.status_code == 204


async def test_revoke_one_session_with_unknown_id_is_rejected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="revokeunknown@example.com", password=PASSWORD, tenant_slug="revunk-t"
    )
    headers = await login_and_get_headers(auth_client, "revokeunknown@example.com", PASSWORD)

    response = await auth_client.delete(f"/api/v1/auth/sessions/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 401


async def test_revoke_all_sessions_invalidates_refresh_and_access_tokens(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="revokeall@example.com", password=PASSWORD, tenant_slug="revokeall-t"
    )
    token = await login(auth_client, "revokeall@example.com", PASSWORD)
    headers = {"Authorization": f"Bearer {token['access_token']}"}

    response = await auth_client.delete("/api/v1/auth/sessions", headers=headers)
    assert response.status_code == 204

    refresh_response = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert refresh_response.status_code == 401

    # token_version was bumped, so the already-issued access token is
    # rejected too, not just future refreshes.
    me_response = await auth_client.get("/api/v1/auth/me", headers=headers)
    assert me_response.status_code == 401


async def test_email_verify_request_endpoint_succeeds_when_authenticated(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="verifyauthed@example.com", password=PASSWORD, tenant_slug="verauth-t"
    )
    headers = await login_and_get_headers(auth_client, "verifyauthed@example.com", PASSWORD)

    response = await auth_client.post("/api/v1/auth/email/verify/request", headers=headers)
    assert response.status_code == 200


async def test_login_transparently_rehashes_a_deprecated_password_hash(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, user, _ = await create_tenant_user_membership(
        db_session, email="rehash@example.com", password=PASSWORD, tenant_slug="rehash-t"
    )
    # A hash produced with weaker-than-configured Argon2id parameters —
    # e.g. from before an `ARGON2_*` setting was tightened — is exactly
    # what `needs_rehash` exists to catch; it isn't scheme-deprecation
    # specific.
    weak_hash = CryptContext(
        schemes=["argon2"], argon2__time_cost=1, argon2__memory_cost=8, argon2__parallelism=1
    ).hash(PASSWORD)
    await UserRepository(db_session).set_password(user, weak_hash, invalidate_sessions=False)
    assert user.hashed_password == weak_hash

    response = await auth_client.post(
        "/api/v1/auth/login", json={"email": "rehash@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200

    await db_session.refresh(user)
    assert user.hashed_password is not None
    assert user.hashed_password != weak_hash
    assert tenant.id


async def test_register_rejects_a_breached_password(db_session: AsyncSession) -> None:
    service = AuthService(db_session, breached_password_checker=AlwaysBreachedPasswordChecker())
    try:
        await service.register(
            RegisterRequest(
                email="breached@example.com",
                display_name="Breached",
                password="Correct-Horse-Battery-Staple-9!",
            )
        )
        raise AssertionError("expected PasswordPolicyError")
    except PasswordPolicyError as exc:
        assert "breach" in exc.violations[0]


async def test_refresh_rejects_when_user_becomes_inactive(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="refreshinactive@example.com", password=PASSWORD, tenant_slug="ref-in-t"
    )
    token = await login(auth_client, "refreshinactive@example.com", PASSWORD)

    await UserRepository(db_session).update(user, UserUpdate(status=UserStatus.SUSPENDED))

    response = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert response.status_code == 401


async def test_refresh_rejects_when_membership_becomes_inactive(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, membership = await create_tenant_user_membership(
        db_session, email="refreshnomem@example.com", password=PASSWORD, tenant_slug="ref-nm-t"
    )
    token = await login(auth_client, "refreshnomem@example.com", PASSWORD)

    await TenantMembershipRepository(db_session).update(
        membership, TenantMembershipUpdate(status=MembershipStatus.SUSPENDED)
    )

    response = await auth_client.post(
        "/api/v1/auth/refresh", json={"refresh_token": token["refresh_token"]}
    )
    assert response.status_code == 401


async def test_access_token_with_no_tenant_claim_is_rejected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="notenant@example.com", password=PASSWORD, tenant_slug="notenant-t"
    )
    token, _ = create_token(subject=str(user.id), token_type="access")  # no tenant_id claim

    response = await auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_access_token_with_invalid_tenant_claim_is_rejected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="badtenant@example.com", password=PASSWORD, tenant_slug="badtenant-t"
    )
    token, _ = create_token(
        subject=str(user.id), token_type="access", extra_claims={"tenant_id": "not-a-uuid"}
    )

    response = await auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_access_token_with_invalid_subject_is_rejected(auth_client: AsyncClient) -> None:
    token, _ = create_token(subject="not-a-uuid", token_type="access")

    response = await auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_garbage_bearer_token_is_rejected(auth_client: AsyncClient) -> None:
    response = await auth_client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt-at-all"}
    )
    assert response.status_code == 401


async def test_access_token_rejected_after_token_version_bump(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="verbump@example.com", password=PASSWORD, tenant_slug="verbump-t"
    )
    headers = await login_and_get_headers(auth_client, "verbump@example.com", PASSWORD)

    await UserRepository(db_session).bump_token_version(user)

    response = await auth_client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401


async def test_access_token_rejected_when_user_suspended(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="suspendme@example.com", password=PASSWORD, tenant_slug="suspendme-t"
    )
    headers = await login_and_get_headers(auth_client, "suspendme@example.com", PASSWORD)

    await UserRepository(db_session).update(user, UserUpdate(status=UserStatus.SUSPENDED))

    response = await auth_client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 401
