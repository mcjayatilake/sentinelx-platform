"""TenantMembership schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import MembershipRole, MembershipStatus


class TenantMembershipCreate(BaseModel):
    tenant_id: UUID
    user_id: UUID
    role: MembershipRole


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
