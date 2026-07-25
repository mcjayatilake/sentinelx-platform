"""ScanEvent — an immutable, append-only log of a scan's state transitions
and progress updates.

The durable backing store for progress reporting (`GET /scans/{id}/progress`
reads the latest `scan.progress` row) and for the domain-event history —
see `app.scan_engine.events` for the in-process pub/sub layer this
persists alongside. Ready for a future websocket endpoint to tail
(polling or `LISTEN/NOTIFY`), not built this phase.

Unlike `AuditEvent` (`ON DELETE SET NULL` — a compliance record that must
outlive the entities it references), this is CASCADE on its scan: a scan
event has no meaning independent of the scan it logs, the same reasoning
already used for `FindingReference.finding_id` → `findings`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

# Documented limit backing the DB-level size check below (same pattern as
# AuditEvent.event_metadata).
SCAN_EVENT_METADATA_MAX_BYTES = 8192


class ScanEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "scan_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "scan_id"],
            ["scans.tenant_id", "scans.id"],
            name="fk_scan_events_tenant_id_scan_id_scans",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            # References the DB column name (`metadata`), not the Python
            # attribute name (`event_metadata`) — see AuditEvent for why.
            f"metadata IS NULL OR pg_column_size(metadata) <= {SCAN_EVENT_METADATA_MAX_BYTES}",
            name="metadata_size_limit",
        ),
        CheckConstraint(
            "progress_percent IS NULL OR (progress_percent >= 0 AND progress_percent <= 100)",
            name="progress_percent_range",
        ),
    )

    # Overrides `CreatedAtMixin`'s `func.now()` (fixed for the whole
    # transaction it's called in) with `clock_timestamp()` (the actual
    # wall-clock time at each INSERT). Each `ScanEvent` insert commits in
    # its own short transaction now (see `app.scan_engine.bootstrap.
    # _wire_subscribers` and ADR 0008), so `now()` would technically be
    # correct today too — but `clock_timestamp()` is the robust choice
    # regardless of whether inserts end up batched into one transaction
    # or not, which is what `ScanEventRepository.get_latest_progress`'s
    # `ORDER BY created_at DESC` actually needs.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    scan_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # Free text, not an enum-CHECK column: the 9 formal domain events (see
    # app.scan_engine.events) always populate this with one of a fixed,
    # documented set of strings ("scan.queued", "finding.created", ...),
    # but the orchestrator also logs incidental operational sub-events
    # here (e.g. "scan.retry_scheduled") that don't warrant a bespoke
    # domain-event class or a CHECK-constraint migration each time one is
    # added. See docs/scan-engine.md for the full event_type reference.
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(50), nullable=True)
    progress_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    event_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)

    def __repr__(self) -> str:
        return (
            f"ScanEvent(id={self.id!r}, scan_id={self.scan_id!r}, event_type={self.event_type!r})"
        )
