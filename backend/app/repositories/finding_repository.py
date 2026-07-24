"""Finding repository — tenant-scoped.

`record_detection` implements the deduplication policy described in
app.models.finding: a fresh fingerprint creates a new row; a fingerprint
already seen on this asset updates `last_seen_at`/`scan_id` on the existing
row instead of inserting a duplicate. `first_seen_at` is never modified
after creation.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finding import Finding
from app.repositories.base import Page
from app.schemas.common import PaginationParams
from app.schemas.finding import FindingCreate, FindingUpdate


class FindingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, tenant_id: uuid.UUID, finding_id: uuid.UUID) -> Finding | None:
        stmt = select(Finding).where(Finding.id == finding_id, Finding.tenant_id == tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_fingerprint(
        self, tenant_id: uuid.UUID, asset_id: uuid.UUID, fingerprint: str
    ) -> Finding | None:
        stmt = select(Finding).where(
            Finding.tenant_id == tenant_id,
            Finding.asset_id == asset_id,
            Finding.fingerprint == fingerprint,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def record_detection(self, data: FindingCreate) -> Finding:
        existing = await self.get_by_fingerprint(data.tenant_id, data.asset_id, data.fingerprint)
        if existing is not None:
            existing.scan_id = data.scan_id
            existing.last_seen_at = datetime.now(UTC)
            await self._session.flush()
            return existing

        finding = Finding(
            tenant_id=data.tenant_id,
            project_id=data.project_id,
            asset_id=data.asset_id,
            scan_id=data.scan_id,
            title=data.title,
            description=data.description,
            severity=data.severity,
            confidence=data.confidence,
            source_tool=data.source_tool,
            external_reference=data.external_reference,
            fingerprint=data.fingerprint,
            remediation=data.remediation,
        )
        self._session.add(finding)
        await self._session.flush()
        return finding

    async def list_by_asset(
        self, tenant_id: uuid.UUID, asset_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[Finding]:
        base_filter = (Finding.tenant_id == tenant_id, Finding.asset_id == asset_id)
        total = (
            await self._session.execute(
                select(func.count()).select_from(Finding).where(*base_filter)
            )
        ).scalar_one()
        stmt = (
            select(Finding)
            .where(*base_filter)
            .order_by(Finding.first_seen_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)

    async def update(self, finding: Finding, data: FindingUpdate) -> Finding:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(finding, field, value)
        await self._session.flush()
        return finding
