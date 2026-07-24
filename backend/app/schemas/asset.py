"""Asset schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import AssetEnvironment, AssetStatus, AssetType, AuthorizationStatus


class AssetCreate(BaseModel):
    tenant_id: UUID
    project_id: UUID
    asset_type: AssetType
    name: str
    locator: str
    environment: AssetEnvironment = AssetEnvironment.UNKNOWN


class AssetUpdate(BaseModel):
    name: str | None = None
    locator: str | None = None
    environment: AssetEnvironment | None = None
    status: AssetStatus | None = None
    authorization_status: AuthorizationStatus | None = None


class AssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    project_id: UUID
    asset_type: AssetType
    name: str
    locator: str
    environment: AssetEnvironment
    status: AssetStatus
    authorization_status: AuthorizationStatus
    created_at: datetime
    updated_at: datetime
