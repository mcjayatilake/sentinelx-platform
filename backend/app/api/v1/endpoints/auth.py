"""Authentication and session-management endpoints.

Business logic lives in `app.services.auth_service.AuthService`; this
module is thin request/response glue plus the two concerns that only make
sense at the HTTP boundary: extracting caller IP/user-agent into a
`RequestContext`, and denylisting the bearer access token's `jti` on
logout. Domain errors from `AuthService` are handled globally — see
`app.core.error_handlers`.
"""

import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import (
    AccessTokenPayloadDep,
    CurrentPrincipalDep,
    CurrentUserDep,
    DbSessionDep,
    Principal,
)
from app.core.config import get_settings
from app.core.rate_limit import get_client_ip, rate_limit
from app.core.token_denylist import deny_token
from app.models.enums import MembershipRole
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    EmailVerifyConfirmRequest,
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    MeResponse,
    MessageResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequestRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    SessionRead,
    SwitchTenantRequest,
    TokenPair,
)
from app.schemas.user import UserRead
from app.services.auth_service import AuthService, RequestContext

router = APIRouter(prefix="/auth", tags=["auth"])

_settings = get_settings()


def _require_user_session(principal: Principal) -> tuple[User, MembershipRole]:
    """`/auth/logout` and `/auth/me` are user-session concepts with no
    meaning for an API-key-authenticated `Principal` — no user (a
    service-account key), no refresh-token session to log out, no role
    to report. Reject cleanly with a clear 401 rather than crash on a
    `None` user/role, or (worse) silently act on the wrong identity if a
    caller sent both an `X-API-Key` and an unrelated Bearer token."""
    if principal.user is None or principal.role is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This endpoint requires a user session (JWT), not an API key.",
        )
    return principal.user, principal.role


def _request_context(request: Request) -> RequestContext:
    return RequestContext(
        ip_address=get_client_ip(request), user_agent=request.headers.get("user-agent")
    )


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, session: DbSessionDep) -> RegisterResponse:
    return await AuthService(session).register(data)


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[Depends(rate_limit("login", max_requests=_settings.rate_limit_login_per_minute))],
)
async def login(data: LoginRequest, request: Request, session: DbSessionDep) -> LoginResponse:
    return await AuthService(session).login(data, _request_context(request))


@router.post(
    "/refresh",
    response_model=TokenPair,
    dependencies=[
        Depends(rate_limit("refresh", max_requests=_settings.rate_limit_refresh_per_minute))
    ],
)
async def refresh(data: RefreshRequest, request: Request, session: DbSessionDep) -> TokenPair:
    return await AuthService(session).refresh(data.refresh_token, _request_context(request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    data: LogoutRequest,
    request: Request,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    payload: AccessTokenPayloadDep,
) -> None:
    user, _role = _require_user_session(principal)
    await AuthService(session).logout(
        user, principal.tenant_id, data.refresh_token, _request_context(request)
    )

    # The refresh-token family is now DB-revoked (above); the bearer
    # access token is stateless and short-lived, so it's denylisted in
    # Redis for exactly its remaining lifetime.
    jti = payload.get("jti")
    exp = payload.get("exp")
    if jti and isinstance(exp, int | float):
        await deny_token(jti, ttl_seconds=int(exp - time.time()))


@router.post("/switch-tenant", response_model=TokenPair)
async def switch_tenant(
    data: SwitchTenantRequest, request: Request, session: DbSessionDep, user: CurrentUserDep
) -> TokenPair:
    return await AuthService(session).switch_tenant(user, data.tenant_id, _request_context(request))


@router.get("/me", response_model=MeResponse)
async def me(principal: CurrentPrincipalDep) -> MeResponse:
    user, role = _require_user_session(principal)
    return MeResponse(
        user=UserRead.model_validate(user),
        tenant_id=principal.tenant_id,
        role=role,
    )


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    data: ChangePasswordRequest, session: DbSessionDep, user: CurrentUserDep
) -> None:
    await AuthService(session).change_password(user, data.current_password, data.new_password)


@router.post(
    "/password/reset/request",
    response_model=MessageResponse,
    dependencies=[
        Depends(
            rate_limit(
                "password_reset", max_requests=_settings.rate_limit_password_reset_per_minute
            )
        )
    ],
)
async def request_password_reset(
    data: PasswordResetRequestRequest, request: Request, session: DbSessionDep
) -> MessageResponse:
    # Always the same response, whether or not the email is registered —
    # the service layer mirrors this by skipping the audit trail too, so
    # neither surface can be used to enumerate accounts.
    await AuthService(session).request_password_reset(data.email, _request_context(request))
    return MessageResponse(
        detail="If that email address is registered, a password reset link has been sent."
    )


@router.post("/password/reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_password_reset(data: PasswordResetConfirmRequest, session: DbSessionDep) -> None:
    await AuthService(session).confirm_password_reset(data.token, data.new_password)


@router.post("/email/verify/request", response_model=MessageResponse)
async def request_email_verification(
    session: DbSessionDep, user: CurrentUserDep
) -> MessageResponse:
    await AuthService(session).request_email_verification(user)
    return MessageResponse(detail="If verification is needed, an email has been sent.")


@router.post("/email/verify/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_email_verification(
    data: EmailVerifyConfirmRequest, session: DbSessionDep
) -> None:
    await AuthService(session).confirm_email_verification(data.token)


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(session: DbSessionDep, user: CurrentUserDep) -> list[SessionRead]:
    tokens = await AuthService(session).list_sessions(user.id)
    return [SessionRead.model_validate(token) for token in tokens]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, session: DbSessionDep, user: CurrentUserDep
) -> None:
    await AuthService(session).revoke_session(user, session_id)


@router.delete("/sessions", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_all_sessions(session: DbSessionDep, user: CurrentUserDep) -> None:
    await AuthService(session).revoke_all_sessions(user)
