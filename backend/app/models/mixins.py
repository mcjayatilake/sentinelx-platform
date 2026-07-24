"""Reusable declarative mixins.

Kept deliberately minimal — only universally-applicable primitives (primary
key, timestamps). No business fields belong here.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """Client-generated UUIDv4 primary key.

    Generated in Python (not `gen_random_uuid()`) so table creation has no
    dependency on a PostgreSQL extension or version.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """`created_at` / `updated_at`, both timezone-aware and server-defaulted."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CreatedAtMixin:
    """`created_at` only — for append-only tables that must never have an
    `updated_at` column (e.g. audit events)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
