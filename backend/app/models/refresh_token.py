"""RefreshToken — DB-backed refresh token with rotation and reuse detection.

Tokens are grouped into a `family_id`: every rotation of a token creates a
new row in the same family and marks the old row `ROTATED` with
`replaced_by_id` pointing at the new one. If an already-`ROTATED` token is
ever presented again, that's a stolen/replayed token — the entire family
must be revoked immediately (see `app.services.auth_service`).

Only a SHA-256 hash of the bearer secret is ever stored — see
`docs/decisions/0004-refresh-token-strategy.md` for why this is SHA-256
and not Argon2id (unlike user passwords).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import RefreshTokenStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "refresh_tokens"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Groups every token produced by rotating the same original login.
    family_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    hashed_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[RefreshTokenStatus] = mapped_column(
        SAEnum(
            RefreshTokenStatus,
            name="refresh_token_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=RefreshTokenStatus.ACTIVE,
        server_default=RefreshTokenStatus.ACTIVE.value,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The token that replaced this one via rotation, if any.
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("refresh_tokens.id", ondelete="SET NULL"), nullable=True
    )
    created_by_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:
        return f"RefreshToken(id={self.id!r}, user_id={self.user_id!r}, status={self.status!r})"
