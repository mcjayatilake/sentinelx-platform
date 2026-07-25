"""TenantMembership repository — tenant-scoped.

Every tenant-facing read method requires `tenant_id` and filters on it
explicitly, so a membership belonging to a different tenant is
structurally unreachable through `get_by_id` even when the caller has a
valid membership UUID. `list_by_user` is the one deliberate exception —
it's how login resolves *which* tenants a user may authenticate into, so
it is necessarily keyed by user, not tenant.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models.enums import MembershipStatus
from app.models.membership import TenantMembership
from app.repositories.base import Page
from app.schemas.common import PaginationParams
from app.schemas.membership import TenantMembershipCreate, TenantMembershipUpdate


class TenantMembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: TenantMembershipCreate) -> TenantMembership:
        membership = TenantMembership(
            tenant_id=data.tenant_id, user_id=data.user_id, role=data.role
        )
        self._session.add(membership)
        await self._session.flush()
        return membership

    async def get_by_id(
        self, tenant_id: uuid.UUID, membership_id: uuid.UUID
    ) -> TenantMembership | None:
        stmt = select(TenantMembership).where(
            TenantMembership.id == membership_id, TenantMembership.tenant_id == tenant_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_tenant_and_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> TenantMembership | None:
        stmt = select(TenantMembership).where(
            TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_tenant(
        self, tenant_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[TenantMembership]:
        total_stmt = (
            select(func.count())
            .select_from(TenantMembership)
            .where(TenantMembership.tenant_id == tenant_id)
        )
        total = (await self._session.execute(total_stmt)).scalar_one()
        stmt = (
            select(TenantMembership)
            .where(TenantMembership.tenant_id == tenant_id)
            .order_by(TenantMembership.created_at)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)

    async def list_by_user(self, user_id: uuid.UUID) -> list[TenantMembership]:
        """Every active membership for a user, tenant eagerly loaded — used
        by login to resolve which tenant to sign into (or to list the
        choices when there's more than one)."""
        stmt = (
            select(TenantMembership)
            .where(
                TenantMembership.user_id == user_id,
                TenantMembership.status == MembershipStatus.ACTIVE,
            )
            .options(joinedload(TenantMembership.tenant))
            .order_by(TenantMembership.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().unique().all())

    async def update(
        self, membership: TenantMembership, data: TenantMembershipUpdate
    ) -> TenantMembership:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(membership, field, value)
        await self._session.flush()
        return membership
