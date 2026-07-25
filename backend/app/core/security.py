"""Security primitives: password hashing, password policy, and JWT
encoding/decoding.

Generic, reusable infrastructure only. Authentication endpoints, user
lookup, and token issuance/revocation *policy* belong in
`app.services.auth_service`, not here.
"""

import hashlib
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import jwt
from passlib.context import CryptContext

from app.core.config import Settings, get_settings

TokenType = Literal["access", "refresh"]


def _password_context(settings: Settings) -> CryptContext:
    # Argon2id only: `hashed_password` is a brand-new column with no
    # pre-existing hashes in any other scheme to stay compatible with, so
    # there is nothing for a legacy `deprecated="auto"` fallback scheme to
    # do. `needs_rehash` still exists (and is still exercised — a future
    # tightening of `argon2_*` parameters is exactly what it's for), it
    # just never has bcrypt as a possible input today. Parameters are
    # explicit, not left to library defaults, so a dependency upgrade
    # can't silently change the work factor.
    return CryptContext(
        schemes=["argon2"],
        deprecated="auto",
        argon2__time_cost=settings.argon2_time_cost,
        argon2__memory_cost=settings.argon2_memory_cost_kib,
        argon2__parallelism=settings.argon2_parallelism,
    )


def hash_password(plain_password: str) -> str:
    """Hash a password with Argon2id. Never log or persist `plain_password`
    itself — only the return value of this function."""
    return _password_context(get_settings()).hash(plain_password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _password_context(get_settings()).verify(plain_password, hashed_password)


def needs_rehash(hashed_password: str) -> bool:
    """True if `hashed_password` used a deprecated scheme or weaker
    parameters than the current policy. Callers (app.services.auth_service)
    re-hash and persist on the next successful login when this is True —
    a transparent, gradual upgrade path with no forced reset."""
    return _password_context(get_settings()).needs_update(hashed_password)


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails the configured policy. Carries
    the list of violated rules; never carries the password itself."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("; ".join(violations))


def validate_password_policy(password: str, settings: Settings | None = None) -> None:
    """Raise `PasswordPolicyError` if `password` doesn't meet the
    configured policy (length + configurable character-class rules)."""
    settings = settings or get_settings()
    violations: list[str] = []

    if len(password) < settings.password_min_length:
        violations.append(f"must be at least {settings.password_min_length} characters long")
    if settings.password_require_uppercase and not re.search(r"[A-Z]", password):
        violations.append("must contain an uppercase letter")
    if settings.password_require_lowercase and not re.search(r"[a-z]", password):
        violations.append("must contain a lowercase letter")
    if settings.password_require_digit and not re.search(r"\d", password):
        violations.append("must contain a digit")
    if settings.password_require_symbol and not re.search(r"[^\w\s]", password):
        violations.append("must contain a symbol")

    if violations:
        raise PasswordPolicyError(violations)


class BreachedPasswordChecker(Protocol):
    """Hook for a future breached-password check (e.g. an HaveIBeenPwned
    k-anonymity range lookup). Not integrated with any external service in
    this phase; `NullBreachedPasswordChecker` is the default used
    everywhere, so the call site already exists and a real implementation
    is a drop-in replacement, not a new integration point to build later.
    """

    async def is_breached(self, password: str) -> bool: ...


class NullBreachedPasswordChecker:
    async def is_breached(self, password: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# High-entropy secrets (refresh tokens, API keys)
# ---------------------------------------------------------------------------
#
# Deliberately SHA-256, not Argon2id. Argon2id is slow *on purpose*, to
# resist brute-forcing a low-entropy, human-chosen password. Refresh
# tokens and API key secrets are 256 bits of `secrets.token_urlsafe`
# randomness, verified on every refresh/API call — running them through a
# deliberately slow KDF is a self-inflicted denial-of-service vector, not
# a security improvement. See docs/decisions/0004-refresh-token-strategy.md.


def generate_secret(num_bytes: int = 32) -> str:
    """A new high-entropy, URL-safe random secret."""
    return secrets.token_urlsafe(num_bytes)


def hash_secret(secret: str) -> str:
    """SHA-256 hex digest of a high-entropy secret, for equality-comparison
    storage (refresh tokens, API keys). Not for passwords — see
    `hash_password`."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


def _secret_and_kid_for(token_type: TokenType, settings: Settings) -> tuple[str, str]:
    """Access and refresh tokens are signed with independent secrets, so a
    refresh token can never be replayed as an access token even if a
    future code path forgot to check the `type` claim. `kid` is carried in
    the header (not used to select a key today — one key per type) so
    multi-key verification, rotation, and JWKS can be added later without
    changing the token shape."""
    if token_type == "access":
        return settings.jwt_secret_key, settings.jwt_access_key_id
    return settings.jwt_refresh_secret_key, settings.jwt_refresh_key_id


def create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Create a signed JWT for `subject` (a user ID). Returns `(token, jti)`
    — callers that need the `jti` (e.g. the logout denylist) get it back
    without decoding the token they just created."""
    settings = get_settings()
    now = datetime.now(UTC)
    jti = str(uuid.uuid4())

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
        "jti": jti,
    }
    if extra_claims:
        payload.update(extra_claims)

    secret, kid = _secret_and_kid_for(token_type, settings)
    token = jwt.encode(payload, secret, algorithm=settings.jwt_algorithm, headers={"kid": kid})
    return token, jti


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a JWT, raising `jwt.PyJWTError` (or a subclass)
    on any failure: bad signature, expired, wrong issuer, or a `type`
    claim that doesn't match `expected_type` (e.g. a refresh token
    presented where an access token is required)."""
    settings = get_settings()
    secret, _ = _secret_and_kid_for(expected_type, settings)
    payload: dict[str, Any] = jwt.decode(
        token,
        secret,
        algorithms=[settings.jwt_algorithm],
        issuer=settings.jwt_issuer,
    )
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected a {expected_type!r} token")
    return payload
