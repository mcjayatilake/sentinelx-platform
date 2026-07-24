"""Project schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import ProjectStatus
from app.schemas.common import normalize_slug


class ProjectCreate(BaseModel):
    tenant_id: UUID
    name: str
    slug: str
    description: str | None = None

    @field_validator("slug")
    @classmethod
    def _normalize_slug(cls, value: str) -> str:
        return normalize_slug(value)


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    name: str
    slug: str
    description: str | None
    status: ProjectStatus
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime
