"""Security primitives: JWT encoding/decoding and password hashing.

Generic, reusable infrastructure only. Authentication endpoints, user
lookup, and token issuance/revocation *policy* belong in
`app.modules`/`app.services`, not here.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TokenType = Literal["access", "refresh"]


def hash_password(plain_password: str) -> str:
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _pwd_context.verify(plain_password, hashed_password)


def create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed JWT for `subject` (typically a user ID)."""
    settings = get_settings()
    now = datetime.now(UTC)

    if expires_delta is None:
        expires_delta = (
            timedelta(minutes=settings.jwt_access_token_expire_minutes)
            if token_type == "access"
            else timedelta(days=settings.jwt_refresh_token_expire_days)
        )

    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iss": settings.jwt_issuer,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT, raising `jwt.PyJWTError` on failure."""
    settings = get_settings()
    return jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
    )
