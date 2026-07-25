"""Scan — a requested or executed scan operation.

No raw command or shell-argument fields by design. `attempt`/`max_attempts`/
`worker_id`/`job_id`/`timeout_seconds` support the scan-engine orchestrator
(`app.scan_engine`) driving this row through its state machine; the row
itself stays a request/result record, not execution logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ScanStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.finding import Finding

# Documented limit backing the DB-level size check below (same pattern as
# AuditEvent.event_metadata).
SCAN_CONFIG_MAX_BYTES = 16384


class Scan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_scans_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_scans_tenant_id_project_id_projects",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "asset_id"],
            ["assets.tenant_id", "assets.id"],
            name="fk_scans_tenant_id_asset_id_assets",
            ondelete="RESTRICT",
        ),
        # A retry (POST /scans/{id}/retry) creates a *new* Scan row rather
        # than resetting this one — see docs/orchestrator.md — so the
        # original's terminal state/timestamps stay untouched history.
        ForeignKeyConstraint(
            ["tenant_id", "retry_of_scan_id"],
            ["scans.tenant_id", "scans.id"],
            name="fk_scans_tenant_id_retry_of_scan_id_scans",
            ondelete="SET NULL",
        ),
        # Note: the naming convention (see app/db/base.py) always renders a
        # CheckConstraint's name as `ck_<table>_<name>` from whatever `name`
        # is given here, so these are bare suffixes, not full names.
        CheckConstraint(
            "started_at IS NULL OR queued_at IS NULL OR started_at >= queued_at",
            name="started_after_queued",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_after_started",
        ),
        CheckConstraint(
            "failed_at IS NULL OR started_at IS NULL OR failed_at >= started_at",
            name="failed_after_started",
        ),
        CheckConstraint(
            "timed_out_at IS NULL OR started_at IS NULL OR timed_out_at >= started_at",
            name="timed_out_after_started",
        ),
        # Generalizes the old `completed_failed_mutually_exclusive`
        # constraint to all four terminal timestamps: at most one may be
        # set, matching `status` being the single source of truth for
        # which terminal state (if any) a scan is in.
        CheckConstraint(
            "(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN failed_at IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN cancelled_at IS NOT NULL THEN 1 ELSE 0 END"
            " + CASE WHEN timed_out_at IS NOT NULL THEN 1 ELSE 0 END) <= 1",
            name="terminal_timestamps_mutually_exclusive",
        ),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("timeout_seconds > 0", name="timeout_seconds_positive"),
        CheckConstraint(
            f"config IS NULL OR pg_column_size(config) <= {SCAN_CONFIG_MAX_BYTES}",
            name="config_size_limit",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scanner_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[ScanStatus] = mapped_column(
        SAEnum(
            ScanStatus,
            name="scan_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=ScanStatus.PENDING,
        server_default=ScanStatus.PENDING.value,
    )
    # Nullable + ON DELETE SET NULL: the scan record is immutable history
    # and must survive removal of the requesting user account.
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timed_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    # Orchestration bookkeeping — set/read by app.scan_engine, never by a
    # client directly.
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    worker_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # The Celery (or future job-queue) task id, for JobQueue.cancel(). An
    # internal operational detail — deliberately not on ScanRead.
    job_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1800, server_default="1800"
    )
    retry_of_scan_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # Scanner-specific configuration (timeouts/resource limits/parallelism/
    # safe mode/proxy — see docs/scan-engine.md). Validated for size here
    # and for shape by ScanCreateRequest at the API boundary; no scanner
    # exists yet to further validate its contents against.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    findings: Mapped[list[Finding]] = relationship(back_populates="scan")

    def __repr__(self) -> str:
        return f"Scan(id={self.id!r}, tenant_id={self.tenant_id!r}, status={self.status!r})"
