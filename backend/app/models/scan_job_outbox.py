"""ScanJobOutbox — the transactional outbox for scan dispatch.

`ScanService._enqueue()` (initial creation) and
`ScanOrchestrator._handle_failure()` (transient-error retry) both write a
row here in the *same* transaction as the `Scan` row's own `QUEUED`
transition, instead of calling `JobQueue.enqueue_scan()` (which ultimately
calls Celery's `send_task()`) directly from inside a not-yet-committed
transaction. A separate, periodic dispatcher (`app.workers.tasks.
outbox_dispatcher`) claims unpublished rows with `SELECT ... FOR UPDATE
SKIP LOCKED` and publishes them to Celery only after they're durably
committed — see docs/decisions/0008-transaction-and-concurrency-model.md
for the full design and the one documented at-least-once gap (a
dispatcher crash between a successful `send_task()` and committing
`published_at`).

`(tenant_id, scan_id, scan_attempt)` is unique: at most one outbox row may
ever exist per scan-attempt, so a bug that tried to enqueue the same
attempt twice fails loudly (`IntegrityError`) at insert time rather than
producing a second, redundant Celery message. `ScanOrchestrator`'s
`claim_for_execution` idempotency guard (see `app.scan_engine.
state_machine`) is the independent, second layer that makes even a
duplicate *delivery* of one outbox row's message harmless.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import UUIDPrimaryKeyMixin

# Documented limit backing the DB-level size check below (same pattern as
# AuditEvent.event_metadata / ScanEvent.event_metadata).
SCAN_JOB_OUTBOX_PAYLOAD_MAX_BYTES = 4096


class ScanJobOutbox(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "scan_job_outbox"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "scan_id"],
            ["scans.tenant_id", "scans.id"],
            name="fk_scan_job_outbox_tenant_id_scan_id_scans",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "scan_id", "scan_attempt", name="uq_scan_job_outbox_tenant_scan_attempt"
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        CheckConstraint("scan_attempt >= 0", name="scan_attempt_non_negative"),
        CheckConstraint("countdown_seconds >= 0", name="countdown_seconds_non_negative"),
        CheckConstraint(
            f"payload IS NULL OR pg_column_size(payload) <= {SCAN_JOB_OUTBOX_PAYLOAD_MAX_BYTES}",
            name="payload_size_limit",
        ),
        # Backs the dispatcher's claim query (`WHERE published_at IS
        # NULL ORDER BY created_at ... FOR UPDATE SKIP LOCKED`) — a
        # partial index over only the (typically small) unpublished
        # subset, not the whole (unboundedly growing) table.
        Index(
            "ix_scan_job_outbox_pending",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    scan_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # The `Scan.attempt` value this dispatch corresponds to — the
    # idempotency key for "has this attempt already been enqueued",
    # independent of `attempt_count` below (which counts the
    # dispatcher's own publish *attempts* for this one row).
    scan_attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    # Only "scan.run" exists today (both initial dispatch and retry use
    # the same event type — see the module docstring); free text rather
    # than a CHECK-constrained enum for the same reason as
    # ScanEvent.event_type: it's an internal operational label, not a
    # client-facing contract.
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, default="scan.run")
    # {scan_id, tenant_id, correlation_id} — see ScanJobPayload
    # (app.scan_engine.job_queue). Deliberately minimal: no scan
    # config/findings/secrets, so a future operator inspecting pending
    # outbox rows never sees anything sensitive.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Retry backoff delay (RetryPolicy.next_delay_seconds), carried
    # through to the dispatcher's `enqueue_scan(..., countdown_seconds=)`
    # call — 0 for an initial (non-retry) dispatch.
    countdown_seconds: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set only after a successful `JobQueue.enqueue_scan()` call — never
    # before. NULL means "not yet published" (or "publish attempted but
    # failed"), the dispatcher's sole claim criterion.
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Sanitized only — see app.core.error_sanitization. Never a raw
    # exception message; a publish failure here is a queue/broker-level
    # error (e.g. Celery broker unreachable), not a scanner exception,
    # but the same "never persist raw exception text" discipline applies.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:
        return (
            f"ScanJobOutbox(id={self.id!r}, scan_id={self.scan_id!r}, "
            f"published_at={self.published_at!r})"
        )
