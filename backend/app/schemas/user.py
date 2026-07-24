"""User schemas. No password/credential fields — see app.models.user."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import UserStatus
from app.schemas.common import normalize_email


class UserCreate(BaseModel):
    email: str
    display_name: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class UserUpdate(BaseModel):
    display_name: str | None = None
    status: UserStatus | None = None


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str
    status: UserStatus
    created_at: datetime
    updated_at: datetime
