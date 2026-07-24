"""AuditEvent repository — append-only.

Deliberately offers only `create` and `list_by_tenant`. There is no
`update`/`delete` method anywhere in this class — append-only is enforced
by omission here, matching the model having no `updated_at` column.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.repositories.base import Page
from app.schemas.audit_event import AuditEventCreate
from app.schemas.common import PaginationParams


class AuditEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: AuditEventCreate) -> AuditEvent:
        event = AuditEvent(
            tenant_id=data.tenant_id,
            actor_user_id=data.actor_user_id,
            action=data.action,
            resource_type=data.resource_type,
            resource_id=data.resource_id,
            outcome=data.outcome,
            correlation_id=data.correlation_id,
            source_ip=data.source_ip,
            user_agent=data.user_agent,
            event_metadata=data.event_metadata,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_by_tenant(
        self, tenant_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[AuditEvent]:
        base_filter = AuditEvent.tenant_id == tenant_id
        total = (
            await self._session.execute(
                select(func.count()).select_from(AuditEvent).where(base_filter)
            )
        ).scalar_one()
        stmt = (
            select(AuditEvent)
            .where(base_filter)
            .order_by(AuditEvent.created_at.desc())
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)
