"""Scan repository — tenant-scoped."""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ScanStatus
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
            config=data.config,
            retry_of_scan_id=data.retry_of_scan_id,
            correlation_id=data.correlation_id or str(uuid.uuid4()),
        )
        # Only overridden when the caller specified one — otherwise the
        # model's own default (see app/models/scan.py) applies, so it
        # stays the single source of truth for the default value.
        if data.timeout_seconds is not None:
            scan.timeout_seconds = data.timeout_seconds
        if data.max_attempts is not None:
            scan.max_attempts = data.max_attempts
        self._session.add(scan)
        await self._session.flush()
        return scan

    async def get_by_id(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> Scan | None:
        # `populate_existing=True`: without it, a `Scan` already present in
        # this session's identity map (e.g. loaded earlier in the same
        # long-lived orchestrator session — see app.scan_engine.orchestrator)
        # would be returned as-is, ignoring the freshly queried row.
        # `ScanOrchestrator._assert_not_cancelled` depends on this method
        # observing a concurrent cancellation committed by a *different*
        # session mid-run; without `populate_existing`, cooperative
        # cancellation would silently never trigger for any scan whose row
        # this session already has loaded.
        stmt = (
            select(Scan)
            .where(Scan.id == scan_id, Scan.tenant_id == tenant_id)
            .execution_options(populate_existing=True)
        )
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

    async def list_by_tenant(
        self,
        tenant_id: uuid.UUID,
        pagination: PaginationParams,
        *,
        asset_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        status: ScanStatus | None = None,
    ) -> Page[Scan]:
        filters = [Scan.tenant_id == tenant_id]
        if asset_id is not None:
            filters.append(Scan.asset_id == asset_id)
        if project_id is not None:
            filters.append(Scan.project_id == project_id)
        if status is not None:
            filters.append(Scan.status == status)
        total = (
            await self._session.execute(select(func.count()).select_from(Scan).where(*filters))
        ).scalar_one()
        stmt = (
            select(Scan)
            .where(*filters)
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

    async def conditional_update(
        self,
        tenant_id: uuid.UUID,
        scan_id: uuid.UUID,
        *,
        expected_status: ScanStatus,
        data: ScanUpdate,
    ) -> Scan | None:
        """Atomic `UPDATE ... WHERE status = :expected_status RETURNING *`
        — the SQL-level guard `ScanStateMachine.transition()` layers under
        its in-memory `assert_valid()` check. Returns `None` (never
        raises) if `scan_id` is no longer `expected_status` — some other
        transaction already moved it first; the caller (`ScanStateMachine`)
        turns that into `StaleScanStateError`. `populate_existing=True` so
        an already-identity-mapped `Scan` object is refreshed from the
        `RETURNING` row rather than left stale — see `get_by_id`'s
        docstring for why that matters for a long-lived session.
        """
        stmt = (
            update(Scan)
            .where(
                Scan.tenant_id == tenant_id,
                Scan.id == scan_id,
                Scan.status == expected_status,
            )
            .values(**data.model_dump(exclude_unset=True))
            .returning(Scan)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().one_or_none()

    async def claim_for_execution(
        self,
        tenant_id: uuid.UUID,
        scan_id: uuid.UUID,
        *,
        expected_attempt: int,
        worker_id: str,
    ) -> Scan | None:
        """Atomically claims `scan_id` for execution: `QUEUED -> PREPARING`
        plus `attempt += 1`, guarded by `WHERE status='QUEUED' AND
        attempt=:expected_attempt`. This is the sole "only one worker may
        acquire execution ownership for a scan attempt" enforcement point
        — a duplicate/late Celery delivery of the same logical attempt
        finds `attempt` already incremented by the delivery that won the
        race, matches zero rows, and gets `None` back (a clean, expected
        no-op the caller should log and return from, not an error).
        """
        stmt = (
            update(Scan)
            .where(
                Scan.tenant_id == tenant_id,
                Scan.id == scan_id,
                Scan.status == ScanStatus.QUEUED,
                Scan.attempt == expected_attempt,
            )
            .values(
                status=ScanStatus.PREPARING,
                attempt=expected_attempt + 1,
                worker_id=worker_id,
            )
            .returning(Scan)
            .execution_options(populate_existing=True)
        )
        result = await self._session.execute(stmt)
        return result.scalars().one_or_none()
