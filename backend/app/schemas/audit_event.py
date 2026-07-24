"""AuditEvent schemas. No update schema — audit events are append-only."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.audit_event import AUDIT_EVENT_METADATA_MAX_BYTES
from app.models.enums import AuditOutcome


class AuditEventCreate(BaseModel):
    tenant_id: UUID | None = None
    actor_user_id: UUID | None = None
    action: str
    resource_type: str
    resource_id: str | None = None
    outcome: AuditOutcome
    correlation_id: str | None = None
    source_ip: str | None = None
    user_agent: str | None = None
    event_metadata: dict[str, Any] | None = None

    @field_validator("event_metadata")
    @classmethod
    def _check_metadata_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        size = len(json.dumps(value).encode()) if value is not None else 0
        if size > AUDIT_EVENT_METADATA_MAX_BYTES:
            raise ValueError(
                f"event_metadata must not exceed {AUDIT_EVENT_METADATA_MAX_BYTES} bytes"
            )
        return value


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID | None
    actor_user_id: UUID | None
    action: str
    resource_type: str
    resource_id: str | None
    outcome: AuditOutcome
    correlation_id: str | None
    source_ip: str | None
    user_agent: str | None
    event_metadata: dict[str, Any] | None
    created_at: datetime
