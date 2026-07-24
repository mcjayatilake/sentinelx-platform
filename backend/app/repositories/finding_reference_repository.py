"""FindingReference repository — tenant-scoped via its parent Finding.

`tenant_id` is a required argument to every method here (not read from the
input schema) precisely so a reference can never be attached to a finding
outside the caller's tenant — `create` verifies the parent finding belongs
to `tenant_id` before inserting anything.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finding import Finding
from app.models.finding_reference import FindingReference
from app.repositories.base import Page
from app.schemas.common import PaginationParams
from app.schemas.finding_reference import FindingReferenceCreate


class FindingNotFoundError(Exception):
    """Raised when the referenced finding does not exist for this tenant."""


class FindingReferenceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, tenant_id: uuid.UUID, data: FindingReferenceCreate) -> FindingReference:
        owner_stmt = select(Finding.id).where(
            Finding.id == data.finding_id, Finding.tenant_id == tenant_id
        )
        owner = (await self._session.execute(owner_stmt)).scalar_one_or_none()
        if owner is None:
            raise FindingNotFoundError(
                f"finding {data.finding_id} not found for tenant {tenant_id}"
            )

        reference = FindingReference(
            tenant_id=tenant_id,
            finding_id=data.finding_id,
            reference_type=data.reference_type,
            value=data.value,
        )
        self._session.add(reference)
        await self._session.flush()
        return reference

    async def list_by_finding(
        self, tenant_id: uuid.UUID, finding_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[FindingReference]:
        base_filter = (
            FindingReference.tenant_id == tenant_id,
            FindingReference.finding_id == finding_id,
        )
        total = (
            await self._session.execute(
                select(func.count()).select_from(FindingReference).where(*base_filter)
            )
        ).scalar_one()
        stmt = (
            select(FindingReference)
            .where(*base_filter)
            .order_by(FindingReference.created_at)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)
