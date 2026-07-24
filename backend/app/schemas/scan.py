"""Scan schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import ScanStatus


class ScanCreate(BaseModel):
    tenant_id: UUID
    project_id: UUID
    asset_id: UUID
    scanner_type: str
    requested_by_user_id: UUID | None = None


class ScanUpdate(BaseModel):
    status: ScanStatus | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    error_code: str | None = None
    error_summary: str | None = None


class ScanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    project_id: UUID
    asset_id: UUID
    scanner_type: str
    status: ScanStatus
    requested_by_user_id: UUID | None
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    error_code: str | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime
