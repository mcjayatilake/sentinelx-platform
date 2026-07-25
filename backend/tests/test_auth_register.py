"""POST /auth/register."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import VerificationTokenPurpose
from app.models.user import User
from app.models.verification_token import UserVerificationToken


async def test_register_creates_user_and_returns_no_password(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "New.User@Example.com",
            "display_name": "New User",
            "password": "Correct-Horse-Battery-Staple-9!",
        },
    )
    assert response.status_code == 201
    body = response.json()

    assert body["user"]["email"] == "new.user@example.com"  # normalized to lowercase
    assert "password" not in body["user"]
    assert "hashed_password" not in body["user"]
    assert body["email_verification_sent"] is True

    user = (
        await db_session.execute(select(User).where(User.email == "new.user@example.com"))
    ).scalar_one()
    assert user.hashed_password is not None
    assert user.hashed_password != "Correct-Horse-Battery-Staple-9!"
    assert user.email_verified_at is None


async def test_register_issues_an_email_verification_token(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "verify-me@example.com",
            "display_name": "Verify Me",
            "password": "Correct-Horse-Battery-Staple-9!",
        },
    )
    user_id = response.json()["user"]["id"]

    token_row = (
        await db_session.execute(
            select(UserVerificationToken).where(
                UserVerificationToken.user_id == user_id,
                UserVerificationToken.purpose == VerificationTokenPurpose.EMAIL_VERIFICATION,
            )
        )
    ).scalar_one()
    assert token_row.consumed_at is None


async def test_register_rejects_duplicate_email(auth_client: AsyncClient) -> None:
    payload = {
        "email": "dupe@example.com",
        "display_name": "Dupe",
        "password": "Correct-Horse-Battery-Staple-9!",
    }
    first = await auth_client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    second = await auth_client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409


async def test_register_is_case_insensitive_for_duplicate_detection(
    auth_client: AsyncClient,
) -> None:
    await auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "case@example.com",
            "display_name": "Case",
            "password": "Correct-Horse-Battery-Staple-9!",
        },
    )
    response = await auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "CASE@EXAMPLE.COM",
            "display_name": "Case Again",
            "password": "Correct-Horse-Battery-Staple-9!",
        },
    )
    assert response.status_code == 409


async def test_register_rejects_weak_password(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/v1/auth/register",
        json={"email": "weak@example.com", "display_name": "Weak", "password": "weak"},
    )
    assert response.status_code == 422
    assert "violations" in response.json()


async def test_register_never_logs_password_in_response(auth_client: AsyncClient) -> None:
    response = await auth_client.post(
        "/api/v1/auth/register",
        json={
            "email": "noleak@example.com",
            "display_name": "No Leak",
            "password": "Correct-Horse-Battery-Staple-9!",
        },
    )
    assert "Correct-Horse-Battery-Staple-9!" not in response.text
