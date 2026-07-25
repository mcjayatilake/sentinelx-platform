"""TenantMembership schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import MembershipRole, MembershipStatus
from app.schemas.common import normalize_email


class TenantMembershipCreate(BaseModel):
    tenant_id: UUID
    user_id: UUID
    role: MembershipRole


class TenantMembershipAddByEmail(BaseModel):
    """Adds an *existing* user to the caller's tenant. Inviting a
    brand-new email address (one with no `User` account yet) is a
    separate, future flow — see docs/rbac.md."""

    email: str
    role: MembershipRole

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class TenantMembershipUpdate(BaseModel):
    role: MembershipRole | None = None
    status: MembershipStatus | None = None


class TenantMembershipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    user_id: UUID
    role: MembershipRole
    status: MembershipStatus
    created_at: datetime
    updated_at: datetime
