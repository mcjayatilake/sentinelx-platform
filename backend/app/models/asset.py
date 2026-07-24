"""Asset — an authorised scan target owned by a tenant and a project.

`tenant_id` is propagated and DB-enforced via a composite foreign key to
`projects(tenant_id, id)` rather than a plain `project_id -> projects.id`
FK, so it is structurally impossible for an asset's `tenant_id` to diverge
from its project's tenant — see docs/tenant-isolation.md.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import AssetEnvironment, AssetStatus, AssetType, AuthorizationStatus
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.project import Project


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "assets"
    __table_args__ = (
        # Lets child tables (Scan, Finding) reference (tenant_id, id) and
        # have Postgres enforce that their tenant_id matches this asset's.
        UniqueConstraint("tenant_id", "id", name="uq_assets_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_assets_tenant_id_project_id_projects",
            ondelete="RESTRICT",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(
        SAEnum(
            AssetType,
            name="asset_type",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # The identifying value for the asset type: URL for `website`, base URL
    # for `api`, `owner/repo` or clone URL for `repository`, image
    # reference for `container_image`, CIDR for `ip_range`, etc. Never a
    # credential — authenticated-scan credentials are out of scope for this
    # phase and will never be stored in plain text when added.
    locator: Mapped[str] = mapped_column(String(2048), nullable=False)
    environment: Mapped[AssetEnvironment] = mapped_column(
        SAEnum(
            AssetEnvironment,
            name="asset_environment",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=AssetEnvironment.UNKNOWN,
        server_default=AssetEnvironment.UNKNOWN.value,
    )
    status: Mapped[AssetStatus] = mapped_column(
        SAEnum(
            AssetStatus,
            name="asset_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=AssetStatus.ACTIVE,
        server_default=AssetStatus.ACTIVE.value,
    )
    authorization_status: Mapped[AuthorizationStatus] = mapped_column(
        SAEnum(
            AuthorizationStatus,
            name="authorization_status",
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            length=32,
        ),
        nullable=False,
        default=AuthorizationStatus.UNAUTHORIZED,
        server_default=AuthorizationStatus.UNAUTHORIZED.value,
    )

    project: Mapped[Project] = relationship(back_populates="assets")

    def __repr__(self) -> str:
        return (
            f"Asset(id={self.id!r}, tenant_id={self.tenant_id!r}, asset_type={self.asset_type!r})"
        )
