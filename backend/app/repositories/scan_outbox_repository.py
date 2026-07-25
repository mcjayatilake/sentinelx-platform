"""ScanJobOutbox repository.

Deliberately not tenant-scoped for `claim_next_pending()` — the outbox
dispatcher is a platform-wide background process that has no tenant
context of its own to filter by (it drains pending rows across every
tenant), the same reasoning `TenantRepository.list_all()` already
documents for the one other legitimately-platform-wide repository method
in this codebase. Every other method here still requires a `tenant_id`,
since a scan's own outbox history is otherwise tenant-owned data.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scan_job_outbox import ScanJobOutbox
from app.schemas.scan_outbox import ScanJobOutboxCreate


class ScanOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ScanJobOutboxCreate) -> ScanJobOutbox:
        outbox = ScanJobOutbox(
            tenant_id=data.tenant_id,
            scan_id=data.scan_id,
            scan_attempt=data.scan_attempt,
            event_type=data.event_type,
            payload=data.payload,
            countdown_seconds=data.countdown_seconds,
        )
        self._session.add(outbox)
        await self._session.flush()
        return outbox

    async def claim_next_pending(self) -> ScanJobOutbox | None:
        """`SELECT ... WHERE published_at IS NULL ORDER BY created_at
        LIMIT 1 FOR UPDATE SKIP LOCKED` — the row lock itself is the
        coordination primitive multiple concurrent dispatcher
        processes/tasks need: each one skips any row another dispatcher
        already has locked instead of blocking on it, so two dispatchers
        can run this query at the same moment and never claim the same
        row. No distributed lock (Redis or otherwise) is needed — see
        docs/decisions/0008-transaction-and-concurrency-model.md.

        Flush-only, matching every other repository method here: the
        caller (the dispatcher task) commits after successfully
        publishing (or after recording a failed attempt), releasing this
        row's lock at that point — never before.
        """
        stmt = (
            select(ScanJobOutbox)
            .where(ScanJobOutbox.published_at.is_(None))
            .order_by(ScanJobOutbox.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_published(
        self, outbox: ScanJobOutbox, *, published_at: datetime, attempt_count: int
    ) -> ScanJobOutbox:
        outbox.published_at = published_at
        outbox.attempt_count = attempt_count
        outbox.last_attempt_at = published_at
        await self._session.flush()
        return outbox

    async def mark_failed_attempt(
        self,
        outbox: ScanJobOutbox,
        *,
        attempt_count: int,
        last_attempt_at: datetime,
        last_error: str,
    ) -> ScanJobOutbox:
        outbox.attempt_count = attempt_count
        outbox.last_attempt_at = last_attempt_at
        outbox.last_error = last_error
        await self._session.flush()
        return outbox

    async def list_by_scan(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> list[ScanJobOutbox]:
        stmt = (
            select(ScanJobOutbox)
            .where(ScanJobOutbox.tenant_id == tenant_id, ScanJobOutbox.scan_id == scan_id)
            .order_by(ScanJobOutbox.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())
