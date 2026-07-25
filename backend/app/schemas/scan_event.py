"""ScanEvent schemas. No update schema — scan events are append-only."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.scan_event import SCAN_EVENT_METADATA_MAX_BYTES


class ScanEventCreate(BaseModel):
    tenant_id: UUID
    scan_id: UUID
    event_type: str
    stage: str | None = None
    progress_percent: int | None = None
    worker_id: str | None = None
    correlation_id: str | None = None
    message: str | None = None
    event_metadata: dict[str, Any] | None = None

    @field_validator("progress_percent")
    @classmethod
    def _check_progress_range(cls, value: int | None) -> int | None:
        if value is not None and not (0 <= value <= 100):
            raise ValueError("progress_percent must be between 0 and 100")
        return value

    @field_validator("event_metadata")
    @classmethod
    def _check_metadata_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        size = len(json.dumps(value).encode()) if value is not None else 0
        if size > SCAN_EVENT_METADATA_MAX_BYTES:
            raise ValueError(
                f"event_metadata must not exceed {SCAN_EVENT_METADATA_MAX_BYTES} bytes"
            )
        return value


class ScanEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    scan_id: UUID
    event_type: str
    stage: str | None
    progress_percent: int | None
    worker_id: str | None
    correlation_id: str | None
    message: str | None
    event_metadata: dict[str, Any] | None
    created_at: datetime
