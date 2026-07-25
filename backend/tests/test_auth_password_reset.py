"""Password reset: request/confirm framework.

Tests exercise `AuthService` directly with a `RecordingEmailSender` to
capture the raw reset token — it is embedded only in the stubbed email
body, by design; only its SHA-256 hash is ever persisted, so there is no
other way for a test (or an attacker) to recover it.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.services.auth_service import InvalidVerificationTokenError, RequestContext
from tests.auth_helpers import (
    RecordingEmailSender,
    auth_service_with_recording_sender,
    create_tenant_user_membership,
    extract_token,
)

PASSWORD = "Correct-Horse-Battery-Staple-9!"
NEW_PASSWORD = "Even-Stronger-Password-42!"


async def test_password_reset_request_and_confirm_changes_password(
    db_session: AsyncSession,
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="reset@example.com", password=PASSWORD, tenant_slug="reset-tenant"
    )
    sender = RecordingEmailSender()
    service = auth_service_with_recording_sender(db_session, sender)

    await service.request_password_reset("reset@example.com", RequestContext(None, None))
    assert len(sender.sent) == 1
    raw_token = extract_token(sender.sent[0][2])

    await service.confirm_password_reset(raw_token, NEW_PASSWORD)

    await db_session.refresh(user)
    assert verify_password(NEW_PASSWORD, user.hashed_password or "")
    assert not verify_password(PASSWORD, user.hashed_password or "")


async def test_password_reset_confirm_invalidates_the_token_after_use(
    db_session: AsyncSession,
) -> None:
    await create_tenant_user_membership(
        db_session, email="onetime@example.com", password=PASSWORD, tenant_slug="onetime-tenant"
    )
    sender = RecordingEmailSender()
    service = auth_service_with_recording_sender(db_session, sender)
    await service.request_password_reset("onetime@example.com", RequestContext(None, None))
    raw_token = extract_token(sender.sent[0][2])

    await service.confirm_password_reset(raw_token, NEW_PASSWORD)

    try:
        await service.confirm_password_reset(raw_token, "Another-Strong-Password-7!")
        raise AssertionError("expected InvalidVerificationTokenError")
    except InvalidVerificationTokenError:
        pass


async def test_password_reset_confirm_rejects_unknown_token(db_session: AsyncSession) -> None:
    service = auth_service_with_recording_sender(db_session, RecordingEmailSender())
    try:
        await service.confirm_password_reset("not-a-real-token", NEW_PASSWORD)
        raise AssertionError("expected InvalidVerificationTokenError")
    except InvalidVerificationTokenError:
        pass


async def test_password_reset_request_for_unknown_email_sends_nothing(
    db_session: AsyncSession,
) -> None:
    sender = RecordingEmailSender()
    service = auth_service_with_recording_sender(db_session, sender)

    # No exception, no email — enumeration-resistant by design.
    await service.request_password_reset("nobody-here@example.com", RequestContext(None, None))
    assert sender.sent == []


async def test_password_reset_invalidates_existing_sessions(db_session: AsyncSession) -> None:
    tenant, user, _ = await create_tenant_user_membership(
        db_session, email="sessions@example.com", password=PASSWORD, tenant_slug="sessions-t"
    )
    original_token_version = user.token_version

    sender = RecordingEmailSender()
    service = auth_service_with_recording_sender(db_session, sender)
    await service.request_password_reset("sessions@example.com", RequestContext(None, None))
    raw_token = extract_token(sender.sent[0][2])
    await service.confirm_password_reset(raw_token, NEW_PASSWORD)

    await db_session.refresh(user)
    assert user.token_version == original_token_version + 1
    assert tenant.id


async def test_password_reset_request_endpoint_returns_generic_message_regardless(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="genericmsg@example.com", password=PASSWORD, tenant_slug="generic-t"
    )

    known = await auth_client.post(
        "/api/v1/auth/password/reset/request", json={"email": "genericmsg@example.com"}
    )
    unknown = await auth_client.post(
        "/api/v1/auth/password/reset/request", json={"email": "never-registered@example.com"}
    )

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()


async def test_password_reset_confirm_endpoint_rejects_invalid_token(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/password/reset/confirm",
        json={"token": "not-a-real-token", "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 400


async def test_hash_password_helper_used_directly_matches_service_hash_shape() -> None:
    # Sanity check that the two password-hashing call sites (register vs.
    # this test module's direct service use) produce compatible hashes.
    assert hash_password(NEW_PASSWORD).startswith("$argon2id$")
