"""Scan — a requested or executed scan operation.

No worker/execution logic and no raw command or shell-argument fields by
design — this phase only persists the *request/result* record.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ScanStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.finding import Finding


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
        # Note: the naming convention (see app/db/base.py) always renders a
        # CheckConstraint's name as `ck_<table>_<name>` from whatever `name`
        # is given here, so these are bare suffixes, not full names.
        CheckConstraint(
            "completed_at IS NULL OR failed_at IS NULL",
            name="completed_failed_mutually_exclusive",
        ),
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
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    findings: Mapped[list[Finding]] = relationship(back_populates="scan")

    def __repr__(self) -> str:
        return f"Scan(id={self.id!r}, tenant_id={self.tenant_id!r}, status={self.status!r})"
