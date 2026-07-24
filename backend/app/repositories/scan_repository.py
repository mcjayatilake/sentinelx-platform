"""Scan repository — tenant-scoped."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scan import Scan
from app.repositories.base import Page
from app.schemas.common import PaginationParams
from app.schemas.scan import ScanCreate, ScanUpdate


class ScanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ScanCreate) -> Scan:
        scan = Scan(
            tenant_id=data.tenant_id,
            project_id=data.project_id,
            asset_id=data.asset_id,
            scanner_type=data.scanner_type,
            requested_by_user_id=data.requested_by_user_id,
        )
        self._session.add(scan)
        await self._session.flush()
        return scan

    async def get_by_id(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> Scan | None:
        stmt = select(Scan).where(Scan.id == scan_id, Scan.tenant_id == tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_asset(
        self, tenant_id: uuid.UUID, asset_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[Scan]:
        base_filter = (Scan.tenant_id == tenant_id, Scan.asset_id == asset_id)
        total = (
            await self._session.execute(select(func.count()).select_from(Scan).where(*base_filter))
        ).scalar_one()
        stmt = (
            select(Scan)
            .where(*base_filter)
            .order_by(Scan.created_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)

    async def update(self, scan: Scan, data: ScanUpdate) -> Scan:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(scan, field, value)
        await self._session.flush()
        return scan
