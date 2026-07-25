"""Authentication and session-management schemas.

`password`/`current_password`/`new_password` fields are `str`, not a
custom secret-redacting type — Pydantic v2 has no built-in "never repr
this" wrapper cheap enough to justify here, so redaction discipline is
enforced at the logging layer instead (see `docs/security.md`: request
bodies for auth endpoints are never logged). None of these fields ever
appear on a response schema.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import MembershipRole
from app.schemas.common import normalize_email
from app.schemas.user import UserRead


class RegisterRequest(BaseModel):
    email: str
    display_name: str
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class RegisterResponse(BaseModel):
    user: UserRead
    email_verification_sent: bool


class LoginRequest(BaseModel):
    email: str
    password: str
    # Only required when the account has more than one active
    # `TenantMembership`; see `LoginResponse.available_tenants`.
    tenant_id: UUID | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class TenantOption(BaseModel):
    tenant_id: UUID
    tenant_name: str
    role: MembershipRole


class LoginResponse(BaseModel):
    """`token` is populated when the tenant could be resolved (the caller
    passed `tenant_id`, or the account has exactly one active
    membership). Otherwise `token` is `None` and `available_tenants`
    lists the memberships the caller must pick between via a follow-up
    `POST /auth/login` with `tenant_id` set.
    """

    token: TokenPair | None = None
    available_tenants: list[TenantOption] = []


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    # Revokes this session's refresh-token family. The bearer access
    # token itself is denylisted from the `Authorization` header, not
    # from this body.
    refresh_token: str


class SwitchTenantRequest(BaseModel):
    tenant_id: UUID


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class PasswordResetRequestRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class PasswordResetConfirmRequest(BaseModel):
    token: str
    new_password: str


class EmailVerifyConfirmRequest(BaseModel):
    token: str


class MeResponse(BaseModel):
    user: UserRead
    tenant_id: UUID
    role: MembershipRole


class SessionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    created_at: datetime
    expires_at: datetime
    user_agent: str | None
    created_by_ip: str | None


class MessageResponse(BaseModel):
    detail: str
