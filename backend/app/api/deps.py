"""Shared FastAPI dependencies.

`get_current_user` and `get_current_principal` are the only places in the
codebase that turn a bearer token into an identity. Every tenant-scoped
endpoint depends on `CurrentPrincipalDep` (directly, or via
`require_permission` in `app.core.permissions`) rather than reading a
tenant ID from anywhere client-supplied.
"""

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.core.token_denylist import is_token_denied
from app.db.session import get_db
from app.models.enums import MembershipRole, MembershipStatus, UserStatus
from app.models.user import User
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.user_repository import UserRepository

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_db_session() -> AsyncGenerator[AsyncSession]:
    async for session in get_db():
        yield session


DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]

_bearer_scheme = HTTPBearer(auto_error=True, description="SentinelX access token")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_access_token_payload(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer_scheme)],
) -> dict[str, Any]:
    """Decodes and validates the bearer token as an access token (signature,
    expiry, issuer, `type` claim) and checks the logout denylist. FastAPI
    caches this per request, so `get_current_user` and
    `get_current_principal` (both of which depend on it) never decode the
    same token twice or double-check Redis.
    """
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
    request: who (user), which tenant, and what role. `tenant_id`/`role`
    come from the token's own claims (set at login/switch-tenant, when
    membership was verified server-side) — never from a client-supplied
    header, query parameter, or path parameter.
    """

    user: User
    tenant_id: uuid.UUID
    role: MembershipRole


async def get_current_principal(
    session: DbSessionDep, user: CurrentUserDep, payload: AccessTokenPayloadDep
) -> Principal:
    """Extends `get_current_user` with tenant/role resolution for
    tenant-scoped endpoints. Re-checks the membership is still active on
    every request (not just at token-issuance time), so a role change or
    removal takes effect immediately rather than waiting for the token to
    expire.
    """
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

    return Principal(user=user, tenant_id=tenant_id, role=membership.role)


CurrentPrincipalDep = Annotated[Principal, Depends(get_current_principal)]
