"""User — a platform identity. Not tenant-owned; tenancy is via TenantMembership.

`hashed_password` is nullable: an OAuth-only user (future SSO integration)
may never have a local password. Never store, log, or return the plaintext
password anywhere — see `app.core.security` for hashing.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import UserStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.membership import TenantMembership


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # Always stored normalized (lowercased + trimmed) by the schema layer;
    # the DB unique constraint is only correct because of that guarantee.
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(
            UserStatus,
            name="user_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )
    # Argon2id hash (see app.core.security.hash_password). Nullable for
    # future OAuth-only accounts.
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Bumped to invalidate every previously-issued access token at once
    # (password change, "log out everywhere", suspected compromise) —
    # embedded as the `ver` claim and checked on every request.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    memberships: Mapped[list[TenantMembership]] = relationship(back_populates="user")

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r})"
