"""Asset repository — tenant-scoped."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.asset import Asset
from app.repositories.base import Page
from app.schemas.asset import AssetCreate, AssetUpdate
from app.schemas.common import PaginationParams


class AssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: AssetCreate) -> Asset:
        asset = Asset(
            tenant_id=data.tenant_id,
            project_id=data.project_id,
            asset_type=data.asset_type,
            name=data.name,
            locator=data.locator,
            environment=data.environment,
        )
        self._session.add(asset)
        await self._session.flush()
        return asset

    async def get_by_id(self, tenant_id: uuid.UUID, asset_id: uuid.UUID) -> Asset | None:
        stmt = select(Asset).where(Asset.id == asset_id, Asset.tenant_id == tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_project(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[Asset]:
        base_filter = (Asset.tenant_id == tenant_id, Asset.project_id == project_id)
        total = (
            await self._session.execute(select(func.count()).select_from(Asset).where(*base_filter))
        ).scalar_one()
        stmt = (
            select(Asset)
            .where(*base_filter)
            .order_by(Asset.created_at)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)

    async def update(self, asset: Asset, data: AssetUpdate) -> Asset:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(asset, field, value)
        await self._session.flush()
        return asset
