"""Outbox dispatcher — the only code that turns a committed
`ScanJobOutbox` row into a real Celery message.

`ScanService`/`ScanOrchestrator` write outbox rows in the same
transaction as a scan's `QUEUED` transition (see `app.repositories.
scan_outbox_repository` and their own docstrings) but never call
`JobQueue.enqueue_scan()` themselves. This Celery-Beat-scheduled task
(`celery_app.conf.beat_schedule`) is what actually does — periodically,
outside any request or orchestrator run, once a row's transaction has
already committed for real.

`_dispatch_one()` claims exactly one row (`SELECT ... FOR UPDATE SKIP
LOCKED`, see `ScanOutboxRepository.claim_next_pending`), publishes it,
and commits — one row, one transaction, one commit — rather than batching
several claims into a single transaction. That's a deliberate choice:
batching would mean a crash between one row's successful `send_task()`
and the batch's eventual commit could re-publish every row in the batch
on the next tick, not just the one that was mid-flight. Looping
one-row-per-transaction bounds that blast radius to at most one
duplicate publish per crash — which is still safe, not just smaller,
because `ScanOrchestrator.run()`'s `claim_for_execution` guard makes a
duplicate *delivery* of the same logical attempt a harmless no-op at the
worker (see `app.scan_engine.state_machine`). This is the one
honestly-documented at-least-once gap in the whole design: a dispatcher
crash between a successful publish and this function's own `commit()`
recording `published_at` leaves a row that looks unpublished and will be
retried, producing a second, redundant (but harmless) Celery message.

Each task invocation builds its own async engine (see `app.workers.
tasks.scan_tasks`'s module docstring for why — the same event-loop-per-
`anyio.run()`-call pitfall applies here).
"""

from datetime import UTC, datetime

import anyio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.error_sanitization import sanitize_exception
from app.core.logging import get_logger
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.scan_engine.job_queue import JobQueue, scan_job_payload_from_dict
from app.schemas.scan import ScanUpdate
from app.workers.celery_app import celery_app
from app.workers.job_queue_celery import CeleryJobQueue

_TASK_NAME = "app.workers.tasks.outbox_dispatcher.dispatch_pending_outbox"

logger = get_logger(__name__)


async def _dispatch_one(session: AsyncSession, job_queue: JobQueue) -> bool:
    """Returns `True` if a row was claimed (regardless of whether the
    publish itself succeeded or failed) — the caller loops on `True` to
    drain up to a batch limit, `False` once the outbox is empty."""
    outbox_repo = ScanOutboxRepository(session)
    outbox = await outbox_repo.claim_next_pending()
    if outbox is None:
        # Nothing claimed; release this no-op transaction cleanly rather
        # than leaving it idle-in-transaction.
        await session.rollback()
        return False

    payload = scan_job_payload_from_dict(outbox.payload)
    now = datetime.now(UTC)
    try:
        job_id = await job_queue.enqueue_scan(payload, countdown_seconds=outbox.countdown_seconds)
    except Exception as exc:
        # Never mark published on a failed send — the row stays
        # unpublished (published_at IS NULL) and is picked up again on
        # the next tick. last_error is sanitized, never str(exc)
        # directly (see app.core.error_sanitization) — a broker/network
        # exception could in principle carry connection-string
        # credentials in its message.
        await outbox_repo.mark_failed_attempt(
            outbox,
            attempt_count=outbox.attempt_count + 1,
            last_attempt_at=now,
            last_error=sanitize_exception(exc).summary,
        )
        await session.commit()
        logger.error(
            "outbox_dispatcher.publish_failed",
            outbox_id=str(outbox.id),
            scan_id=str(outbox.scan_id),
        )
        return True

    # Best-effort: Scan.job_id capture failing must never turn a
    # successful publish into a retried (and therefore duplicated) one —
    # the message is already on the queue.
    scan = await ScanRepository(session).get_by_id(outbox.tenant_id, outbox.scan_id)
    if scan is not None:
        await ScanRepository(session).update(scan, ScanUpdate(job_id=job_id))

    await outbox_repo.mark_published(
        outbox, published_at=now, attempt_count=outbox.attempt_count + 1
    )
    await session.commit()
    logger.info(
        "outbox_dispatcher.published", outbox_id=str(outbox.id), scan_id=str(outbox.scan_id)
    )
    return True


async def _run_async(batch_size: int) -> int:
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    job_queue = CeleryJobQueue(celery_app)
    dispatched = 0
    try:
        async with session_factory() as session:
            for _ in range(batch_size):
                claimed = await _dispatch_one(session, job_queue)
                if not claimed:
                    break
                dispatched += 1
    finally:
        await engine.dispose()
    return dispatched


@celery_app.task(name=_TASK_NAME)  # type: ignore[untyped-decorator]
def dispatch_pending_outbox() -> int:
    """Drains up to `settings.scan_outbox_dispatch_batch_size` pending
    rows. Returns the number actually dispatched (for Celery result
    inspection/logging, not consumed by anything in this phase)."""
    settings = get_settings()
    return anyio.run(_run_async, settings.scan_outbox_dispatch_batch_size)
