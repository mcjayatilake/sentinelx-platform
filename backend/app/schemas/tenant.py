"""Tenant schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import TenantStatus
from app.schemas.common import normalize_slug


class TenantCreate(BaseModel):
    name: str
    slug: str

    @field_validator("slug")
    @classmethod
    def _normalize_slug(cls, value: str) -> str:
        return normalize_slug(value)


class TenantUpdate(BaseModel):
    name: str | None = None
    status: TenantStatus | None = None


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    slug: str
    status: TenantStatus
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
