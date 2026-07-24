"""Finding — a normalised, deduplicated security finding.

Deduplication: unique on `(tenant_id, asset_id, fingerprint)`. Re-detection
of the same fingerprint updates the existing row (`last_seen_at`, `scan_id`)
rather than inserting a duplicate; `first_seen_at` is set once and never
modified. See docs/data-model.md for the full policy.
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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import FindingConfidence, FindingSeverity, FindingStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.finding_reference import FindingReference
    from app.models.scan import Scan


class Finding(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_findings_tenant_id_id"),
        UniqueConstraint(
            "tenant_id",
            "asset_id",
            "fingerprint",
            name="uq_findings_tenant_id_asset_id_fingerprint",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_findings_tenant_id_project_id_projects",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "asset_id"],
            ["assets.tenant_id", "assets.id"],
            name="fk_findings_tenant_id_asset_id_assets",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "scan_id"],
            ["scans.tenant_id", "scans.id"],
            name="fk_findings_tenant_id_scan_id_scans",
            ondelete="RESTRICT",
        ),
        # Bare suffixes: the naming convention renders these as
        # `ck_findings_<name>` (see app/models/scan.py for why).
        CheckConstraint("last_seen_at >= first_seen_at", name="last_seen_after_first_seen"),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= first_seen_at",
            name="resolved_after_first_seen",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scan_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    severity: Mapped[FindingSeverity] = mapped_column(
        SAEnum(
            FindingSeverity,
            name="finding_severity",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
    )
    status: Mapped[FindingStatus] = mapped_column(
        SAEnum(
            FindingStatus,
            name="finding_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=FindingStatus.OPEN,
        server_default=FindingStatus.OPEN.value,
    )
    confidence: Mapped[FindingConfidence] = mapped_column(
        SAEnum(
            FindingConfidence,
            name="finding_confidence",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=FindingConfidence.MEDIUM,
        server_default=FindingConfidence.MEDIUM.value,
    )
    source_tool: Mapped[str] = mapped_column(String(100), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Deterministic hash of the tool's stable finding identity (e.g. rule ID
    # + locator + normalized detail) used for deduplication across scans.
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    remediation: Mapped[str | None] = mapped_column(Text, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    scan: Mapped[Scan] = relationship(back_populates="findings")
    references: Mapped[list[FindingReference]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"Finding(id={self.id!r}, tenant_id={self.tenant_id!r}, severity={self.severity!r})"
