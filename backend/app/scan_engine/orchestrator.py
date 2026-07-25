"""`ScanOrchestrator` — the state-machine-driving engine.

`run()` is the entrypoint `app.workers.tasks.scan_tasks.execute_scan`
calls for every scan execution attempt. It is the one place that:
resolves a scanner plugin, drives `Scan.status` through
`ScanStateMachine`, builds the `ScanContext` every plugin method receives,
enforces the configured timeout, checks for cooperative cancellation
between stages, and decides retry vs. permanent failure on error.

Cancellation is cooperative, not preemptive: a concurrent
`POST /scans/{id}/cancel` call sets `status=CANCELLED` in the database,
and `run()` notices it at the next stage boundary (`_assert_not_cancelled`)
— it cannot interrupt a plugin already inside `execute()`. See
docs/orchestrator.md.

Every `ScanStateMachine.transition()` call commits immediately (see that
class) and is guarded by a SQL-level conditional `UPDATE`, not just an
in-memory check — a concurrent writer (a cancel request, a duplicate
Celery delivery of the same attempt) can make any of them raise
`StaleScanStateError`. `run()` catches that once, around the whole
execution, rather than at every call site — see `_handle_stale_state`.

Known limitation, explicit future work: if the worker process crashes
between steps, the row can be left in a non-terminal state with no live
worker and nothing left to resume it. A Celery Beat "stuck-scan reaper"
(periodic scan for rows past `started_at + timeout_seconds` with a stale
`worker_id`) is the natural fix and is intentionally not built this
phase — see docs/orchestrator.md.
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from structlog.stdlib import BoundLogger

from app.core.error_sanitization import sanitize_exception
from app.models.enums import ScanStatus
from app.models.scan import Scan
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.scan_engine.context import ScanContext
from app.scan_engine.events import (
    EventPublisher,
    ScanCancelled,
    ScanCompleted,
    ScanFailed,
    ScanProgress,
    ScanStarted,
    ScanTimedOut,
)
from app.scan_engine.exceptions import ScanCancelledError, ScanConfigError, StaleScanStateError
from app.scan_engine.interfaces import ScannerPlugin
from app.scan_engine.job_queue import JobQueue, ScanJobPayload, scan_job_payload_to_dict
from app.scan_engine.metrics import Metrics
from app.scan_engine.pipeline.pipeline import FindingPipeline
from app.scan_engine.registry import ScannerRegistry
from app.scan_engine.retry import ErrorClass, RetryPolicy, classify_error
from app.scan_engine.state_machine import ScanStateMachine
from app.scan_engine.storage.interfaces import ArtifactStorage
from app.schemas.scan_outbox import ScanJobOutboxCreate


class ScanOrchestrator:
    def __init__(
        self,
        session: AsyncSession,
        scan_repository: ScanRepository,
        outbox_repository: ScanOutboxRepository,
        state_machine: ScanStateMachine,
        registry: ScannerRegistry,
        finding_pipeline: FindingPipeline,
        event_publisher: EventPublisher,
        artifact_storage: ArtifactStorage,
        metrics: Metrics,
        retry_policy: RetryPolicy,
        job_queue: JobQueue,
        logger: BoundLogger,
    ) -> None:
        self._session = session
        self._scans = scan_repository
        self._outbox = outbox_repository
        self._state_machine = state_machine
        self._registry = registry
        self._pipeline = finding_pipeline
        self._events = event_publisher
        self._artifact_storage = artifact_storage
        self._metrics = metrics
        self._retry_policy = retry_policy
        self._job_queue = job_queue
        self._logger = logger

    async def run(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, worker_id: str) -> None:
        scan = await self._scans.get_by_id(tenant_id, scan_id)
        if scan is None:
            self._logger.warning("scan_engine.scan_not_found", scan_id=str(scan_id))
            return
        if scan.status != ScanStatus.QUEUED:
            # Duplicate/late delivery of an already-claimed, already-
            # terminal, or already-cancelled scan — a clean, expected
            # no-op under at-least-once queue delivery, not a task
            # failure. Subsumes the old CANCELLED-only check: any status
            # other than QUEUED means some other invocation already owns
            # (or finished) this attempt.
            self._logger.info(
                "scan_engine.duplicate_or_stale_delivery_ignored",
                scan_id=str(scan_id),
                tenant_id=str(tenant_id),
                status=scan.status.value,
            )
            return

        correlation_id = scan.correlation_id or str(uuid.uuid4())

        if not self._registry.is_registered(scan.scanner_type):
            await self._finalize_failed(
                scan,
                error_code="scanner_not_registered",
                exc=None,
                error_summary=f"No scanner plugin registered for {scan.scanner_type!r}",
                correlation_id=correlation_id,
            )
            return

        plugin = self._registry.get(scan.scanner_type)

        # The sole "only one worker may acquire execution ownership for a
        # scan attempt" enforcement point: atomically QUEUED -> PREPARING
        # plus attempt += 1, guarded by `WHERE attempt = scan.attempt`. A
        # duplicate/late delivery of this same logical attempt loses this
        # race and gets None back.
        claimed = await self._scans.claim_for_execution(
            tenant_id, scan.id, expected_attempt=scan.attempt, worker_id=worker_id
        )
        if claimed is None:
            self._logger.info(
                "scan_engine.duplicate_delivery_ignored",
                scan_id=str(scan.id),
                tenant_id=str(tenant_id),
            )
            return
        await self._session.commit()
        scan = claimed

        await self._events.publish(
            ScanStarted(
                scan_id=scan.id,
                tenant_id=tenant_id,
                correlation_id=correlation_id,
                worker_id=worker_id,
            )
        )

        context = ScanContext(
            tenant_id=tenant_id,
            scan_id=scan.id,
            project_id=scan.project_id,
            asset_id=scan.asset_id,
            scanner_type=scan.scanner_type,
            requested_by_user_id=scan.requested_by_user_id,
            correlation_id=correlation_id,
            config=scan.config or {},
            timeout_seconds=scan.timeout_seconds,
            artifact_storage=self._artifact_storage,
            metrics=self._metrics,
            logger=self._logger.bind(
                scan_id=str(scan.id), tenant_id=str(tenant_id), correlation_id=correlation_id
            ),
        )

        try:
            await plugin.validate_config(context)
        except ScanConfigError as exc:
            await self._finalize_failed(
                scan,
                error_code="invalid_config",
                exc=exc,
                error_summary=None,
                correlation_id=correlation_id,
            )
            await self._cleanup(plugin, context)
            return

        try:
            try:
                async with asyncio.timeout(scan.timeout_seconds):
                    await self._run_stages(scan, plugin, context, worker_id, correlation_id)
            except TimeoutError:
                scan = await self._state_machine.transition(scan, ScanStatus.TIMED_OUT)
                await self._events.publish(
                    ScanTimedOut(
                        scan_id=scan.id, tenant_id=tenant_id, correlation_id=correlation_id
                    )
                )
                self._metrics.increment("scan.timed_out")
            except ScanCancelledError:
                # `_assert_not_cancelled` re-fetches `scan` from the DB via
                # `ScanRepository.get_by_id` (`populate_existing=True`)
                # before raising this — since that fetch refreshes the
                # same identity-mapped `Scan` object `scan` already refers
                # to, `scan.status` here is *already* CANCELLED (set, with
                # its `cancelled_at`, by whichever request/session
                # cancelled it). Transitioning CANCELLED -> CANCELLED
                # again would be an invalid self-edge, so only transition
                # if that hasn't happened yet.
                if scan.status != ScanStatus.CANCELLED:
                    scan = await self._state_machine.transition(scan, ScanStatus.CANCELLED)
                await self._events.publish(
                    ScanCancelled(
                        scan_id=scan.id, tenant_id=tenant_id, correlation_id=correlation_id
                    )
                )
            except Exception as exc:  # noqa: BLE001 - classify_error() decides retry vs. permanent
                await self._handle_failure(scan, exc, correlation_id)
        except StaleScanStateError:
            # A concurrent writer (cancellation racing completion, or a
            # genuinely unexpected second claimant) moved this scan out
            # from under one of the transitions above. Never overwrite a
            # terminal row: reconcile against the DB's current truth
            # instead of blindly retrying the transition.
            await self._handle_stale_state(scan, tenant_id, correlation_id)
        finally:
            await self._cleanup(plugin, context)

    async def _run_stages(
        self,
        scan: Scan,
        plugin: ScannerPlugin,
        context: ScanContext,
        worker_id: str,
        correlation_id: str,
    ) -> None:
        await self._assert_not_cancelled(context.tenant_id, scan.id)
        await plugin.prepare(context)

        await self._assert_not_cancelled(context.tenant_id, scan.id)
        scan = await self._state_machine.transition(scan, ScanStatus.RUNNING, worker_id=worker_id)
        await self._publish_progress(scan, context, ScanStatus.RUNNING, 10, correlation_id)
        raw_output = await plugin.execute(context)

        await self._assert_not_cancelled(context.tenant_id, scan.id)
        scan = await self._state_machine.transition(
            scan, ScanStatus.COLLECTING, worker_id=worker_id
        )
        await self._publish_progress(scan, context, ScanStatus.COLLECTING, 60, correlation_id)
        raw_findings = await plugin.collect_results(context, raw_output)

        await self._assert_not_cancelled(context.tenant_id, scan.id)
        scan = await self._state_machine.transition(
            scan, ScanStatus.PROCESSING, worker_id=worker_id
        )
        await self._publish_progress(scan, context, ScanStatus.PROCESSING, 85, correlation_id)
        normalized_findings = plugin.normalize_findings(context, raw_findings)
        await self._pipeline.process(context, normalized_findings)

        scan = await self._state_machine.transition(scan, ScanStatus.SUCCEEDED)
        await self._events.publish(
            ScanCompleted(
                scan_id=scan.id, tenant_id=context.tenant_id, correlation_id=correlation_id
            )
        )
        self._metrics.increment("scan.succeeded")

    async def _publish_progress(
        self, scan: Scan, context: ScanContext, stage: ScanStatus, percent: int, correlation_id: str
    ) -> None:
        await self._events.publish(
            ScanProgress(
                scan_id=scan.id,
                tenant_id=context.tenant_id,
                correlation_id=correlation_id,
                stage=stage,
                percent=percent,
            )
        )

    async def _assert_not_cancelled(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> None:
        current = await self._scans.get_by_id(tenant_id, scan_id)
        if current is not None and current.status == ScanStatus.CANCELLED:
            raise ScanCancelledError(f"Scan {scan_id} was cancelled")

    async def _handle_stale_state(
        self, scan: Scan, tenant_id: uuid.UUID, correlation_id: str
    ) -> None:
        """Reconciles against the DB's current truth after a
        `StaleScanStateError` — never blindly retries the transition that
        raised it, since that would risk overwriting whatever the
        concurrent writer already committed."""
        current = await self._scans.get_by_id(tenant_id, scan.id)
        if current is not None and current.status == ScanStatus.CANCELLED:
            await self._events.publish(
                ScanCancelled(scan_id=scan.id, tenant_id=tenant_id, correlation_id=correlation_id)
            )
            return
        self._logger.error(
            "scan_engine.stale_scan_state_during_run",
            scan_id=str(scan.id),
            tenant_id=str(tenant_id),
            current_status=current.status.value if current is not None else "not_found",
        )

    async def _handle_failure(self, scan: Scan, exc: Exception, correlation_id: str) -> None:
        error_class = classify_error(exc)
        self._metrics.increment("scan.error", tags={"error_class": error_class.value})

        if error_class == ErrorClass.TRANSIENT and self._retry_policy.should_retry(
            scan.attempt, scan.max_attempts
        ):
            delay = self._retry_policy.next_delay_seconds(scan.attempt)
            # Outbox row written *before* the QUEUED transition, on this
            # same session — its commit (ScanStateMachine.transition)
            # durably lands both together. No direct
            # JobQueue.enqueue_scan() call here: the dispatcher publishes
            # once this has actually committed — see _enqueue's
            # docstring counterpart in app.services.scan_service and
            # docs/decisions/0008-transaction-and-concurrency-model.md.
            await self._outbox.create(
                ScanJobOutboxCreate(
                    tenant_id=scan.tenant_id,
                    scan_id=scan.id,
                    scan_attempt=scan.attempt,
                    payload=scan_job_payload_to_dict(
                        ScanJobPayload(
                            scan_id=scan.id,
                            tenant_id=scan.tenant_id,
                            correlation_id=correlation_id,
                        )
                    ),
                    countdown_seconds=delay,
                )
            )
            scan = await self._state_machine.transition(scan, ScanStatus.QUEUED)
            await self._events.publish(
                ScanProgress(
                    scan_id=scan.id,
                    tenant_id=scan.tenant_id,
                    correlation_id=correlation_id,
                    stage=ScanStatus.QUEUED,
                    percent=0,
                    message=(
                        f"Retry scheduled (attempt {scan.attempt}/{scan.max_attempts}) "
                        f"after a transient error."
                    ),
                )
            )
            return

        await self._finalize_failed(
            scan,
            error_code=type(exc).__name__,
            exc=exc,
            error_summary=None,
            correlation_id=correlation_id,
        )

    async def _finalize_failed(
        self,
        scan: Scan,
        *,
        error_code: str,
        exc: Exception | None,
        error_summary: str | None,
        correlation_id: str,
    ) -> None:
        """`error_summary`, when not explicitly given, is derived from
        `exc` through `sanitize_exception()` — never `str(exc)` directly.
        A future scanner's exception may carry credentials, tokens, or
        authenticated URLs; this is the one and only place that decides
        what of a scan failure becomes durable/API-visible text, see
        `app.core.error_sanitization` and
        docs/decisions/0008-transaction-and-concurrency-model.md."""
        if error_summary is None:
            error_summary = sanitize_exception(exc).summary if exc is not None else error_code

        scan = await self._state_machine.transition(
            scan, ScanStatus.FAILED, error_code=error_code, error_summary=error_summary
        )
        await self._events.publish(
            ScanFailed(
                scan_id=scan.id,
                tenant_id=scan.tenant_id,
                correlation_id=correlation_id,
                error_code=error_code,
                error_summary=error_summary,
                dead_letter=True,
            )
        )
        self._metrics.increment("scan.failed")

        try:
            await self._job_queue.dead_letter_scan(
                ScanJobPayload(
                    scan_id=scan.id, tenant_id=scan.tenant_id, correlation_id=correlation_id
                ),
                reason=error_code,
            )
        except Exception:
            # Best-effort observability, not the source of truth for the
            # scan's outcome — Scan.status is already finalized FAILED.
            self._logger.error("scan_engine.dead_letter_routing_failed", scan_id=str(scan.id))

    async def _cleanup(self, plugin: ScannerPlugin, context: ScanContext) -> None:
        try:
            await plugin.cleanup(context)
        except Exception:
            self._metrics.increment("scan.cleanup_failed")
            self._logger.error("scan_engine.cleanup_failed", scan_id=str(context.scan_id))
