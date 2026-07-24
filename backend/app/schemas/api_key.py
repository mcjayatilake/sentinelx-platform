"""APIKeyMetadata schemas.

`hashed_secret` never appears on `APIKeyMetadataRead` — that is the whole
point of this table. Issuing/authenticating keys (and therefore hashing a
raw secret) is out of scope for this phase; `APIKeyMetadataCreate` takes an
already-hashed value.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import APIKeyStatus


class APIKeyMetadataCreate(BaseModel):
    tenant_id: UUID
    user_id: UUID | None = None
    name: str
    prefix: str
    hashed_secret: str
    scopes: list[str] = []
    expires_at: datetime | None = None


class APIKeyMetadataUpdate(BaseModel):
    status: APIKeyStatus | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class APIKeyMetadataRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    user_id: UUID | None
    name: str
    prefix: str
    scopes: list[str]
    status: APIKeyStatus
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    updated_at: datetime
