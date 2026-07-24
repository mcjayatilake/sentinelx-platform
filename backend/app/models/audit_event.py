"""AuditEvent — an immutable, append-only security and administrative record.

No `updated_at` column (see `CreatedAtMixin`) and no repository update/delete
methods (see `app.repositories.audit_event_repository`) — append-only is
enforced by omission at both layers. `tenant_id` / `actor_user_id` are
nullable with `ON DELETE SET NULL` so the audit trail outlives the entities
it references.
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AuditOutcome
from app.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin

# Documented limit backing the DB-level size check below.
AUDIT_EVENT_METADATA_MAX_BYTES = 8192


class AuditEvent(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            # References the DB column name (`metadata`), not the Python
            # attribute name (`event_metadata`) — see the attribute above.
            f"metadata IS NULL OR pg_column_size(metadata) <= {AUDIT_EVENT_METADATA_MAX_BYTES}",
            name="metadata_size_limit",
        ),
    )

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(150), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    outcome: Mapped[AuditOutcome] = mapped_column(
        SAEnum(
            AuditOutcome,
            name="audit_outcome",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
    )
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Textual, not INET: keeps this generic over IPv4/IPv6/proxied values
    # without a network-address column type that could reject edge cases.
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Column name stays `metadata` in the database; the Python attribute is
    # renamed because `metadata` is reserved on declarative model classes
    # (it's SQLAlchemy's own `Base.metadata`).
    event_metadata: Mapped[dict[str, object] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )

    def __repr__(self) -> str:
        return f"AuditEvent(id={self.id!r}, action={self.action!r}, outcome={self.outcome!r})"
