"""UserVerificationToken — single-use tokens for email verification and
password reset.

One shared table, not two: both flows need exactly the same shape (a
hashed, expiring, single-use token tied to a user); `purpose` is the only
thing that differs. Only a SHA-256 hash of the token is ever stored — see
`docs/decisions/0004-refresh-token-strategy.md` for the same reasoning
applied here (high-entropy random secret, not a user-chosen password).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import VerificationTokenPurpose
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class UserVerificationToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_verification_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[VerificationTokenPurpose] = mapped_column(
        SAEnum(
            VerificationTokenPurpose,
            name="verification_token_purpose",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
    )
    hashed_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"UserVerificationToken(id={self.id!r}, user_id={self.user_id!r}, "
            f"purpose={self.purpose!r})"
        )
