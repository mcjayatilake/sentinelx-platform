"""Shared FastAPI dependencies.

`get_current_user` and `get_current_principal` are the only places in the
codebase that turn a bearer token into an identity. Every tenant-scoped
endpoint depends on `CurrentPrincipalDep` (directly, or via
`require_permission` in `app.core.permissions`) rather than reading a
tenant ID from anywhere client-supplied.
"""

import uuid
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.core.token_denylist import is_token_denied
from app.db.session import get_db
from app.models.enums import MembershipRole, MembershipStatus, TenantStatus, UserStatus
from app.models.user import User
from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.services.api_key_service import APIKeyService

SettingsDep = Annotated[Settings, Depends(get_settings)]

# A plain alias, not a wrapper generator: `get_db()` already implements the
# full commit/rollback transaction policy (see its docstring in
# app/db/session.py) — a wrapper that does `async for session in get_db():
# yield session` looks like safe delegation but isn't, since FastAPI
# throws an escaped exception into *this* function's suspended `yield`,
# which never reaches `get_db()`'s separate generator frame. Tests
# override this exact name (`app.dependency_overrides[get_db_session]`),
# so it must stay a distinct importable symbol, not an inline `Depends(get_db)`.
get_db_session = get_db

DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]

# auto_error=False on both schemes: `get_current_principal` needs to
# inspect *which* credential (if either) was actually sent before
# deciding which authentication path to take, which FastAPI's built-in
# auto-401 (fired before our code ever runs) would preempt. Endpoints
# that only ever want the JWT session concept (e.g. `get_current_user`,
# used by /auth/switch-tenant, /auth/password/change) still end up
# requiring a real Bearer token — `get_access_token_payload` raises the
# same 401 explicitly now instead of relying on HTTPBearer's auto_error.
_bearer_scheme = HTTPBearer(auto_error=False, description="SentinelX access token")
_api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="SentinelX API key (prefix.secret)"
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_access_token_payload(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Decodes and validates the bearer token as an access token (signature,
    expiry, issuer, `type` claim) and checks the logout denylist. FastAPI
    caches this per request, so `get_current_user` and
    `get_current_principal` (both of which depend on it) never decode the
    same token twice or double-check Redis.
    """
    if credentials is None:
        raise _unauthorized("Not authenticated")

    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid or expired access token") from exc

    jti = payload.get("jti")
    if jti and await is_token_denied(jti):
        raise _unauthorized("Token has been revoked")

    return payload


AccessTokenPayloadDep = Annotated[dict[str, Any], Depends(get_access_token_payload)]


async def get_current_user(session: DbSessionDep, payload: AccessTokenPayloadDep) -> User:
    """Loads the `User` for a validated access token. Also enforces
    `token_version`: bumping a user's `token_version` (password change,
    "log out everywhere", suspected compromise) invalidates every
    previously-issued access token at once, checked here on every request.
    """
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise _unauthorized("Invalid token subject") from exc

    user = await UserRepository(session).get_by_id(user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise _unauthorized("User not found or inactive")

    if payload.get("ver") != user.token_version:
        raise _unauthorized("Token has been invalidated")

    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


@dataclass(frozen=True, slots=True)
class Principal:
    """The fully-resolved identity of an authenticated, tenant-scoped
    request: who (user, if any), which tenant, and what it's allowed to
    do. `tenant_id` always comes from a server-verified source — the
    JWT's own claim (set at login/switch-tenant, when membership was
    verified server-side) or the API key's own stored `tenant_id` — never
    from a client-supplied header, query parameter, or path parameter.

    `role` and `scopes` are mutually exclusive by construction: a JWT
    (user-session) principal sets `role` and leaves `scopes` `None`; an
    API-key principal sets `scopes` and leaves `role` `None`. See
    `app.core.permissions.principal_has_permission` — an API-key
    principal's `scopes` are checked on their own and never fall back to
    a role-based grant, so it structurally cannot exceed what its key was
    actually issued. `user` is `None` for a tenant-level service-account
    API key not tied to any specific user (`APIKeyMetadata.user_id` is
    nullable) — callers that only make sense for a real user session
    (e.g. `/auth/me`, `/auth/logout`) are JWT-only in practice and are not
    reachable via an API key in normal use.
    """

    user: User | None
    tenant_id: uuid.UUID
    role: MembershipRole | None = None
    scopes: frozenset[str] | None = None


async def _ensure_tenant_active(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Applied identically to both the JWT and API-key principal paths —
    a suspended/archived tenant must lose access through either door.
    Previously only `TenantMembership.status` was checked, never
    `Tenant.status` itself."""
    tenant = await TenantRepository(session).get_by_id(tenant_id)
    if tenant is None or tenant.status != TenantStatus.ACTIVE:
        raise _unauthorized("Tenant is not active")


async def _principal_from_api_key(session: AsyncSession, plaintext_key: str) -> Principal:
    record = await APIKeyService(APIKeyMetadataRepository(session)).authenticate(plaintext_key)
    if record is None:
        raise _unauthorized("Invalid API key")

    # authenticate() already flushed the last_used_at bump (repositories
    # never call session.commit() — see CLAUDE.md rule 9); commit it now,
    # in its own short transaction, rather than holding that row's write
    # lock for the rest of the request. A single automation key can
    # receive many concurrent requests, and get_db()'s own end-of-request
    # commit would otherwise serialize every one of them behind whichever
    # request's UPDATE landed first.
    await session.commit()

    await _ensure_tenant_active(session, record.tenant_id)

    user: User | None = None
    if record.user_id is not None:
        user = await UserRepository(session).get_by_id(record.user_id)

    return Principal(
        user=user, tenant_id=record.tenant_id, role=None, scopes=frozenset(record.scopes)
    )


async def get_current_principal(
    session: DbSessionDep,
    api_key: Annotated[str | None, Depends(_api_key_header)] = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> Principal:
    """Resolves the caller's identity via `X-API-Key` if present —
    authenticated exclusively through `APIKeyService.authenticate()`, any
    Bearer token sent alongside is ignored, a simple deterministic rule —
    otherwise via the existing JWT bearer-token flow. Re-checks the
    tenant membership (JWT path) or the key's own status (API-key path)
    is still active on every request, not just at issuance time, so a
    role change, removal, revocation, or tenant suspension takes effect
    immediately rather than waiting for a token to expire.
    """
    if api_key is not None:
        return await _principal_from_api_key(session, api_key)

    payload = await get_access_token_payload(credentials)
    user = await get_current_user(session, payload)

    tenant_id_claim = payload.get("tenant_id")
    if not tenant_id_claim:
        raise _unauthorized("Token is not scoped to a tenant")

    try:
        tenant_id = uuid.UUID(tenant_id_claim)
    except ValueError as exc:
        raise _unauthorized("Invalid tenant claim") from exc

    membership = await TenantMembershipRepository(session).get_by_tenant_and_user(
        tenant_id, user.id
    )
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        raise _unauthorized("Tenant membership is no longer active")

    await _ensure_tenant_active(session, tenant_id)

    return Principal(user=user, tenant_id=tenant_id, role=membership.role, scopes=None)


CurrentPrincipalDep = Annotated[Principal, Depends(get_current_principal)]
