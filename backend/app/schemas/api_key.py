"""APIKeyMetadata schemas.

`hashed_secret` never appears on `APIKeyMetadataRead`, and the plaintext
key itself only ever appears on `APIKeyIssueResponse` — returned exactly
once, at creation time, by `app.services.api_key_service`. There is no
endpoint or repository method that can retrieve a plaintext key after
that response.

Deliberately no dependency on `app.core.permissions.Permission` here
(scope validation lives at the endpoint layer instead — see
`app.api.v1.endpoints.api_keys.create_api_key`): `app.core.permissions`
imports `app.api.deps`, which imports this module transitively via
`app.repositories.api_key_repository`, so importing it back from here
would be circular.
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


class APIKeyIssueRequest(BaseModel):
    name: str
    scopes: list[str] = []
    expires_at: datetime | None = None


class APIKeyIssueResponse(BaseModel):
    id: UUID
    name: str
    prefix: str
    # The only time this endpoint (or any endpoint) ever returns the
    # plaintext key. Store it now — it cannot be recovered later.
    api_key: str
    scopes: list[str]
    expires_at: datetime | None
    created_at: datetime
