"""FindingReference — a structured external reference (CVE, CWE, OWASP, ...)
attached to a Finding.

The only `CASCADE` delete in this schema: a reference has no meaning or
lifecycle independent of the finding it annotates.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import FindingReferenceType
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.finding import Finding


class FindingReference(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "finding_references"
    __table_args__ = (
        UniqueConstraint(
            "finding_id",
            "reference_type",
            "value",
            name="uq_finding_references_finding_id_reference_type_value",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "finding_id"],
            ["findings.tenant_id", "findings.id"],
            name="fk_finding_references_tenant_id_finding_id_findings",
            ondelete="CASCADE",
        ),
    )

    # Denormalized from the parent finding solely to support the composite
    # FK above; not an independent ownership relationship.
    tenant_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    finding_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    reference_type: Mapped[FindingReferenceType] = mapped_column(
        SAEnum(
            FindingReferenceType,
            name="finding_reference_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(String(500), nullable=False)

    finding: Mapped[Finding] = relationship(back_populates="references")

    def __repr__(self) -> str:
        return (
            f"FindingReference(finding_id={self.finding_id!r}, "
            f"reference_type={self.reference_type!r})"
        )
