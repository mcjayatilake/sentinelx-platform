"""FastAPI exception handlers mapping domain-service errors to HTTP
responses.

Endpoints raise (or let propagate) typed exceptions from
`app.services.auth_service` / `app.core.security` instead of constructing
an `HTTPException` at each call site — one mapping, defined once, instead
of the same status-code decision duplicated across every endpoint that
can fail the same way.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.security import PasswordPolicyError
from app.services.auth_service import (
    AccountInactiveError,
    AuthServiceError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidVerificationTokenError,
    RefreshTokenReuseDetectedError,
    TenantAccessDeniedError,
)
from app.services.scan_service import (
    AssetNotFoundError,
    AssetProjectMismatchError,
    ProjectNotFoundError,
    ScanNotCancellableError,
    ScanNotFoundError,
    ScanNotRetryableError,
    ScanServiceError,
)

_STATUS_BY_ERROR: dict[type[AuthServiceError], int] = {
    InvalidCredentialsError: status.HTTP_401_UNAUTHORIZED,
    AccountInactiveError: status.HTTP_401_UNAUTHORIZED,
    EmailAlreadyRegisteredError: status.HTTP_409_CONFLICT,
    TenantAccessDeniedError: status.HTTP_403_FORBIDDEN,
    InvalidRefreshTokenError: status.HTTP_401_UNAUTHORIZED,
    RefreshTokenReuseDetectedError: status.HTTP_401_UNAUTHORIZED,
    InvalidVerificationTokenError: status.HTTP_400_BAD_REQUEST,
}

_DETAIL_BY_ERROR: dict[type[AuthServiceError], str] = {
    InvalidCredentialsError: "Invalid email or password.",
    AccountInactiveError: "This account is not active.",
    EmailAlreadyRegisteredError: "That email address is already registered.",
    TenantAccessDeniedError: "You do not have access to that tenant.",
    InvalidRefreshTokenError: "Invalid or expired refresh token.",
    RefreshTokenReuseDetectedError: "This refresh token has already been used and was revoked.",
    InvalidVerificationTokenError: "Invalid or expired token.",
}

_SCAN_STATUS_BY_ERROR: dict[type[ScanServiceError], int] = {
    ScanNotFoundError: status.HTTP_404_NOT_FOUND,
    ProjectNotFoundError: status.HTTP_404_NOT_FOUND,
    AssetNotFoundError: status.HTTP_404_NOT_FOUND,
    AssetProjectMismatchError: status.HTTP_400_BAD_REQUEST,
    ScanNotCancellableError: status.HTTP_409_CONFLICT,
    ScanNotRetryableError: status.HTTP_409_CONFLICT,
}

_SCAN_DETAIL_BY_ERROR: dict[type[ScanServiceError], str] = {
    ScanNotFoundError: "Scan not found.",
    ProjectNotFoundError: "Project not found.",
    AssetNotFoundError: "Asset not found.",
    AssetProjectMismatchError: "The asset does not belong to the given project.",
}


async def auth_service_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AuthServiceError)
    status_code = _STATUS_BY_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
    detail = _DETAIL_BY_ERROR.get(type(exc), "The request could not be processed.")
    headers = (
        {"WWW-Authenticate": "Bearer"} if status_code == status.HTTP_401_UNAUTHORIZED else None
    )
    return JSONResponse(status_code=status_code, content={"detail": detail}, headers=headers)


async def password_policy_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, PasswordPolicyError)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Password does not meet policy requirements.",
            "violations": exc.violations,
        },
    )


async def scan_service_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ScanServiceError)
    status_code = _SCAN_STATUS_BY_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
    detail = str(exc) or _SCAN_DETAIL_BY_ERROR.get(type(exc), "The request could not be processed.")
    return JSONResponse(status_code=status_code, content={"detail": detail})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthServiceError, auth_service_error_handler)
    app.add_exception_handler(PasswordPolicyError, password_policy_error_handler)
    app.add_exception_handler(ScanServiceError, scan_service_error_handler)
