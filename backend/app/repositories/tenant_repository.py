"""Tenant repository.

Tenant is the root of tenant-scoped data — it is not itself owned by a
tenant, so (unlike every other repository here) a platform-wide list method
is legitimate.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.repositories.base import Page
from app.schemas.common import PaginationParams
from app.schemas.tenant import TenantCreate, TenantUpdate


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: TenantCreate) -> Tenant:
        tenant = Tenant(name=data.name, slug=data.slug)
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def get_by_id(self, tenant_id: uuid.UUID) -> Tenant | None:
        return await self._session.get(Tenant, tenant_id)

    async def get_by_slug(self, slug: str) -> Tenant | None:
        stmt = select(Tenant).where(Tenant.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update(self, tenant: Tenant, data: TenantUpdate) -> Tenant:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(tenant, field, value)
        await self._session.flush()
        return tenant

    async def list_all(self, pagination: PaginationParams) -> Page[Tenant]:
        total = (await self._session.execute(select(func.count()).select_from(Tenant))).scalar_one()
        stmt = (
            select(Tenant)
            .order_by(Tenant.created_at)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)
