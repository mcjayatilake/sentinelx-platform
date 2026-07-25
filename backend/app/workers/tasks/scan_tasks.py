"""Celery task entrypoint for scan execution.

Each invocation builds its own async SQLAlchemy engine rather than
reusing `app.db.session`'s process-wide singleton: `anyio.run()` binds a
fresh event loop per call, and asyncpg connections are bound to the loop
that created them, so a shared engine would break on any invocation after
the first (the same event-loop-per-invocation pitfall already solved for
the Redis client in `tests/conftest.py` during the auth phase). The
engine is disposed in a `finally` block regardless of outcome.

`ScanOrchestrator.run()` already contains its own internal
try/except/finally covering every scan-execution failure mode (timeout,
cancellation, transient/permanent errors) and never lets those propagate
here — see `app/scan_engine/orchestrator.py`. An exception escaping
`orchestrator.run()` therefore means an infrastructure failure (e.g. the
database connection dropped) that happened outside the scan's own
try block, which is why it is rolled back and re-raised rather than
swallowed: Celery must see the task as failed so it shows up in
monitoring, even though `ScanOrchestrator` has no Celery-level
`self.retry()`/`autoretry_for` of its own (all retry *decisions* live in
`app.scan_engine.retry.RetryPolicy`, which records the decision as a
`ScanJobOutbox` row for `app.workers.tasks.outbox_dispatcher` to publish
once committed — `JobQueue` only ever does what it's told, and never
directly from here).
"""

import uuid

import anyio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.logging import get_logger
from app.scan_engine.bootstrap import build_orchestrator
from app.workers.celery_app import celery_app
from app.workers.job_queue_celery import CeleryJobQueue

_TASK_NAME = "app.workers.tasks.scan_tasks.execute_scan"

logger = get_logger(__name__)


async def _run_async(
    scan_id: uuid.UUID, tenant_id: uuid.UUID, correlation_id: str, worker_id: str
) -> None:
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
    try:
        async with session_factory() as session:
            orchestrator = build_orchestrator(
                session,
                job_queue=CeleryJobQueue(celery_app),
                settings=settings,
            )
            try:
                await orchestrator.run(tenant_id, scan_id, worker_id)
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()
    finally:
        await engine.dispose()


@celery_app.task(name=_TASK_NAME, bind=True)  # type: ignore[untyped-decorator]
def execute_scan(self, scan_id: str, tenant_id: str, correlation_id: str) -> None:  # type: ignore[no-untyped-def]
    worker_id = self.request.hostname or self.request.id or str(uuid.uuid4())
    logger.info(
        "scan_engine.task_received",
        scan_id=scan_id,
        tenant_id=tenant_id,
        correlation_id=correlation_id,
        worker_id=worker_id,
    )
    anyio.run(
        _run_async,
        uuid.UUID(scan_id),
        uuid.UUID(tenant_id),
        correlation_id,
        worker_id,
    )
