"""Scan lifecycle service: the request-scoped operations exposed at
`/scans` — create, get, list, cancel, retry, get-progress. Actual scan
*execution* happens in `app.workers.tasks.scan_tasks.execute_scan` (a
Celery task, driven by `app.scan_engine.orchestrator.ScanOrchestrator`);
this service only ever creates/reads `Scan` rows and writes a
`ScanJobOutbox` row in the same transaction as its `QUEUED` transition
(see `_enqueue`) — it never calls `JobQueue.enqueue_scan()` directly;
`app.workers.tasks.outbox_dispatcher` does, once that transaction has
actually committed. `JobQueue` is still used directly here for
`cancel()` (best-effort, not outbox-routed — see `cancel_scan`). No
scanner is invoked from here, no scan runs inline in a request.

`ScanServiceError` is the only exception hierarchy `app/api/v1/endpoints/
scans.py` needs to handle (see `app.core.error_handlers`). It is
deliberately narrower than `app.scan_engine.exceptions.ScanEngineError`,
which lives entirely within `app.scan_engine`/`app.workers.tasks`
(worker-side, never surfaces as an HTTP response — see that module's
docstring).

Every method takes a caller-verified `tenant_id` (from
`CurrentPrincipalDep`, never a client-supplied value) and every query is
scoped by it via the tenant-owned repositories, satisfying "no scan may
execute outside tenant boundaries" at the same boundary the rest of the
platform already enforces it.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import ScanStatus
from app.models.scan import Scan
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_event_repository import ScanEventRepository
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.scan_engine.bootstrap import build_event_bus
from app.scan_engine.events import ScanCancelled, ScanQueued
from app.scan_engine.exceptions import InvalidScanTransitionError, StaleScanStateError
from app.scan_engine.job_queue import JobQueue, ScanJobPayload, scan_job_payload_to_dict
from app.scan_engine.progress import estimate_completion
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.common import Page, PaginationParams
from app.schemas.scan import ScanCreate, ScanCreateRequest, ScanProgressRead, ScanRead
from app.schemas.scan_outbox import ScanJobOutboxCreate

logger = get_logger(__name__)

# A retry re-runs a scan that did not reach a successful terminal state.
# Retrying a SUCCEEDED scan isn't "retry" semantics (nothing failed to
# retry) and isn't offered by this endpoint — running a scan again on
# purpose is a new `POST /scans`, not this action.
_RETRYABLE_STATUSES = frozenset({ScanStatus.FAILED, ScanStatus.CANCELLED, ScanStatus.TIMED_OUT})


class ScanServiceError(Exception):
    """Base class for scan-service failures the API layer translates to
    HTTP responses (see `app.core.error_handlers`)."""


class ScanNotFoundError(ScanServiceError):
    pass


class ProjectNotFoundError(ScanServiceError):
    pass


class AssetNotFoundError(ScanServiceError):
    pass


class AssetProjectMismatchError(ScanServiceError):
    """The requested `asset_id` does not belong to the requested `project_id`."""


class ScanNotCancellableError(ScanServiceError):
    """The scan is already in a terminal state."""


class ScanNotRetryableError(ScanServiceError):
    """Only a scan in `FAILED`/`CANCELLED`/`TIMED_OUT` may be retried."""


def _correlation_id(scan: Scan) -> str:
    # ScanRepository.create() always sets one; this is a defensive
    # fallback for the type checker, not an expected runtime path.
    return scan.correlation_id or str(scan.id)


class ScanService:
    def __init__(self, session: AsyncSession, job_queue: JobQueue) -> None:
        self._session = session
        self._scans = ScanRepository(session)
        self._projects = ProjectRepository(session)
        self._assets = AssetRepository(session)
        self._scan_events = ScanEventRepository(session)
        self._outbox = ScanOutboxRepository(session)
        self._job_queue = job_queue
        self._state_machine = ScanStateMachine(session)
        self._events = build_event_bus(session)

    async def create_scan(
        self, tenant_id: uuid.UUID, requested_by_user_id: uuid.UUID | None, data: ScanCreateRequest
    ) -> ScanRead:
        project = await self._projects.get_by_id(tenant_id, data.project_id)
        if project is None:
            raise ProjectNotFoundError()

        asset = await self._assets.get_by_id(tenant_id, data.asset_id)
        if asset is None:
            raise AssetNotFoundError()
        if asset.project_id != data.project_id:
            raise AssetProjectMismatchError()

        scan = await self._scans.create(
            ScanCreate(
                tenant_id=tenant_id,
                project_id=data.project_id,
                asset_id=data.asset_id,
                scanner_type=data.scanner_type,
                requested_by_user_id=requested_by_user_id,
                config=data.config,
                timeout_seconds=data.timeout_seconds,
                max_attempts=data.max_attempts,
            )
        )
        scan = await self._enqueue(scan)
        return ScanRead.model_validate(scan)

    async def get_scan(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> ScanRead:
        scan = await self._get_or_404(tenant_id, scan_id)
        return ScanRead.model_validate(scan)

    async def list_scans(
        self,
        tenant_id: uuid.UUID,
        pagination: PaginationParams,
        *,
        asset_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        status: ScanStatus | None = None,
    ) -> Page[ScanRead]:
        page = await self._scans.list_by_tenant(
            tenant_id, pagination, asset_id=asset_id, project_id=project_id, status=status
        )
        return Page[ScanRead](
            items=[ScanRead.model_validate(scan) for scan in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )

    async def cancel_scan(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> ScanRead:
        scan = await self._get_or_404(tenant_id, scan_id)
        job_id = scan.job_id

        try:
            scan = await self._state_machine.transition(scan, ScanStatus.CANCELLED)
        except (InvalidScanTransitionError, StaleScanStateError) as exc:
            # StaleScanStateError here means a concurrent writer (most
            # likely the scan's own worker finishing/failing/timing out)
            # already moved it out of the state this request believed it
            # was in — same "already not cancellable" outcome as an
            # in-memory-detected invalid edge, just caught one layer
            # later (at the SQL level, where it's the actual source of
            # truth for a race like this).
            raise ScanNotCancellableError(str(exc)) from exc

        await self._events.publish(
            ScanCancelled(
                scan_id=scan.id, tenant_id=scan.tenant_id, correlation_id=_correlation_id(scan)
            )
        )

        if job_id is not None:
            try:
                await self._job_queue.cancel(job_id)
            except Exception:
                # Best-effort: `scan.status` is already CANCELLED, the
                # source of truth for the outcome; cancellation of an
                # in-flight job is cooperative anyway (see
                # docs/orchestrator.md) so a queue-side failure here
                # doesn't change the scan's recorded state.
                logger.error("scan_service.job_cancel_failed", scan_id=str(scan.id))

        return ScanRead.model_validate(scan)

    async def retry_scan(
        self, tenant_id: uuid.UUID, scan_id: uuid.UUID, requested_by_user_id: uuid.UUID | None
    ) -> ScanRead:
        original = await self._get_or_404(tenant_id, scan_id)
        if original.status not in _RETRYABLE_STATUSES:
            raise ScanNotRetryableError(
                f"Scan {scan_id} is {original.status.value!r}; only "
                f"{sorted(s.value for s in _RETRYABLE_STATUSES)} scans may be retried."
            )

        scan = await self._scans.create(
            ScanCreate(
                tenant_id=tenant_id,
                project_id=original.project_id,
                asset_id=original.asset_id,
                scanner_type=original.scanner_type,
                requested_by_user_id=requested_by_user_id,
                config=original.config,
                timeout_seconds=original.timeout_seconds,
                max_attempts=original.max_attempts,
                retry_of_scan_id=original.id,
            )
        )
        scan = await self._enqueue(scan)
        return ScanRead.model_validate(scan)

    async def get_progress(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> ScanProgressRead:
        scan = await self._get_or_404(tenant_id, scan_id)
        latest = await self._scan_events.get_latest_progress(tenant_id, scan_id)
        now = datetime.now(UTC)

        if latest is not None:
            stage = latest.stage or scan.status.value
            percent = latest.progress_percent if latest.progress_percent is not None else 0
            worker_id = latest.worker_id or scan.worker_id
            updated_at = latest.created_at
        else:
            stage = scan.status.value
            percent = 100 if scan.status == ScanStatus.SUCCEEDED else 0
            worker_id = scan.worker_id
            updated_at = scan.updated_at

        return ScanProgressRead(
            scan_id=scan.id,
            status=scan.status,
            stage=stage,
            percent=percent,
            started_at=scan.started_at,
            estimated_completion_at=estimate_completion(
                started_at=scan.started_at, percent=percent, now=now
            ),
            worker_id=worker_id,
            updated_at=updated_at,
        )

    async def _get_or_404(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> Scan:
        scan = await self._scans.get_by_id(tenant_id, scan_id)
        if scan is None:
            raise ScanNotFoundError()
        return scan

    async def _enqueue(self, scan: Scan) -> Scan:
        """Writes the outbox row *before* the QUEUED transition, both on
        this session — the transition's own commit (see
        `ScanStateMachine.transition`) is what makes them durable
        together, atomically, without any new transactional machinery.
        No `JobQueue.enqueue_scan()` call happens here: a separate,
        periodic dispatcher (`app.workers.tasks.outbox_dispatcher`)
        publishes to Celery only after this commit has actually landed —
        see docs/decisions/0008-transaction-and-concurrency-model.md.
        `Scan.job_id` is likewise no longer set here; the dispatcher
        captures it, best-effort, after a successful publish."""
        correlation_id = _correlation_id(scan)
        await self._outbox.create(
            ScanJobOutboxCreate(
                tenant_id=scan.tenant_id,
                scan_id=scan.id,
                scan_attempt=scan.attempt,
                payload=scan_job_payload_to_dict(
                    ScanJobPayload(
                        scan_id=scan.id, tenant_id=scan.tenant_id, correlation_id=correlation_id
                    )
                ),
            )
        )
        scan = await self._state_machine.transition(scan, ScanStatus.QUEUED)
        await self._events.publish(
            ScanQueued(scan_id=scan.id, tenant_id=scan.tenant_id, correlation_id=correlation_id)
        )
        return scan
