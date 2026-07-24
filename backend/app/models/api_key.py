"""APIKeyMetadata — metadata only for a future API key.

Only ever stores a hash (`hashed_secret`) plus a short public `prefix` used
for lookup; issuing or authenticating keys is out of scope for this phase.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import APIKeyStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class APIKeyMetadata(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "api_key_metadata"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    # Nullable: a key may be a tenant-level service-account key not tied to
    # a specific user; SET NULL preserves the key's audit metadata if the
    # issuing user is later removed.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Short, non-secret public identifier (e.g. "sx_live_ab12cd34") used to
    # locate a key record without comparing the hash against every row.
    prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    hashed_secret: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(100)), nullable=False, default=list)
    status: Mapped[APIKeyStatus] = mapped_column(
        SAEnum(
            APIKeyStatus,
            name="api_key_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=APIKeyStatus.ACTIVE,
        server_default=APIKeyStatus.ACTIVE.value,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"APIKeyMetadata(id={self.id!r}, tenant_id={self.tenant_id!r}, prefix={self.prefix!r})"
        )
