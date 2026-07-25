"""ScanJobOutbox schemas. No `*Read`/API exposure — this table is purely
an internal delivery mechanism between `ScanService`/`ScanOrchestrator`
and `app.workers.tasks.outbox_dispatcher`, never surfaced to a client."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ScanJobOutboxCreate(BaseModel):
    tenant_id: UUID
    scan_id: UUID
    scan_attempt: int
    event_type: str = "scan.run"
    payload: dict[str, Any]
    countdown_seconds: float = 0.0


class ScanJobOutboxMarkPublished(BaseModel):
    published_at: datetime
    attempt_count: int
    last_attempt_at: datetime


class ScanJobOutboxMarkFailedAttempt(BaseModel):
    attempt_count: int
    last_attempt_at: datetime
    last_error: str
