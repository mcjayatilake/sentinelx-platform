"""Email verification: request/confirm framework."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.auth_service import InvalidVerificationTokenError
from tests.auth_helpers import (
    RecordingEmailSender,
    auth_service_with_recording_sender,
    create_tenant_user_membership,
    extract_token,
)

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_confirming_email_verification_sets_email_verified_at(
    db_session: AsyncSession,
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="verifyme@example.com", password=PASSWORD, tenant_slug="verify-tenant"
    )
    assert user.email_verified_at is None

    sender = RecordingEmailSender()
    service = auth_service_with_recording_sender(db_session, sender)
    await service.request_email_verification(user)
    assert len(sender.sent) == 1
    raw_token = extract_token(sender.sent[0][2])

    await service.confirm_email_verification(raw_token)

    await db_session.refresh(user)
    assert user.email_verified_at is not None


async def test_email_verification_token_is_single_use(db_session: AsyncSession) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="singleuse@example.com", password=PASSWORD, tenant_slug="single-t"
    )
    sender = RecordingEmailSender()
    service = auth_service_with_recording_sender(db_session, sender)
    await service.request_email_verification(user)
    raw_token = extract_token(sender.sent[0][2])

    await service.confirm_email_verification(raw_token)

    try:
        await service.confirm_email_verification(raw_token)
        raise AssertionError("expected InvalidVerificationTokenError")
    except InvalidVerificationTokenError:
        pass


async def test_confirm_rejects_unknown_token(db_session: AsyncSession) -> None:
    service = auth_service_with_recording_sender(db_session, RecordingEmailSender())
    try:
        await service.confirm_email_verification("not-a-real-token")
        raise AssertionError("expected InvalidVerificationTokenError")
    except InvalidVerificationTokenError:
        pass


async def test_requesting_verification_when_already_verified_is_a_no_op(
    db_session: AsyncSession,
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="alreadydone@example.com", password=PASSWORD, tenant_slug="already-t"
    )
    sender = RecordingEmailSender()
    service = auth_service_with_recording_sender(db_session, sender)
    await service.request_email_verification(user)
    raw_token = extract_token(sender.sent[0][2])
    await service.confirm_email_verification(raw_token)

    sender.sent.clear()
    await service.request_email_verification(user)
    assert sender.sent == []


async def test_email_verify_request_endpoint_requires_authentication(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.post("/api/v1/auth/email/verify/request")
    assert response.status_code == 401


async def test_email_verify_confirm_endpoint_rejects_invalid_token(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/email/verify/confirm", json={"token": "not-a-real-token"}
    )
    assert response.status_code == 400
