"""ScanEvent repository — append-only, tenant-scoped.

Deliberately offers only `create`, `list_by_scan`, and `get_latest_progress`
— no `update`/`delete` anywhere in this class, matching the model having
no `updated_at` column (same pattern as `AuditEventRepository`).
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scan_event import ScanEvent
from app.repositories.base import Page
from app.schemas.common import PaginationParams
from app.schemas.scan_event import ScanEventCreate


class ScanEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ScanEventCreate) -> ScanEvent:
        event = ScanEvent(
            tenant_id=data.tenant_id,
            scan_id=data.scan_id,
            event_type=data.event_type,
            stage=data.stage,
            progress_percent=data.progress_percent,
            worker_id=data.worker_id,
            correlation_id=data.correlation_id,
            message=data.message,
            event_metadata=data.event_metadata,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_by_scan(
        self, tenant_id: uuid.UUID, scan_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[ScanEvent]:
        base_filter = (ScanEvent.tenant_id == tenant_id, ScanEvent.scan_id == scan_id)
        total = (
            await self._session.execute(
                select(func.count()).select_from(ScanEvent).where(*base_filter)
            )
        ).scalar_one()
        stmt = (
            select(ScanEvent)
            .where(*base_filter)
            .order_by(ScanEvent.created_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)

    async def get_latest_progress(
        self, tenant_id: uuid.UUID, scan_id: uuid.UUID
    ) -> ScanEvent | None:
        stmt = (
            select(ScanEvent)
            .where(
                ScanEvent.tenant_id == tenant_id,
                ScanEvent.scan_id == scan_id,
                ScanEvent.event_type == "scan.progress",
            )
            .order_by(ScanEvent.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
