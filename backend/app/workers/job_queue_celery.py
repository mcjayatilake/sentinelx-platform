"""`CeleryJobQueue` — the concrete `app.scan_engine.job_queue.JobQueue`
adapter for today.

`send_task`/`control.revoke` are synchronous Celery-client calls against
Redis, wrapped in `async def` only to satisfy the `JobQueue` Protocol
shape — honestly documented as fast, non-blocking-in-practice sync I/O,
not real async I/O. Not routed through a thread pool this phase: this
runs from inside a Celery task's own synchronous execution context (see
`app.workers.tasks.scan_tasks`), not a concurrently-serving FastAPI
request path, where blocking briefly would matter more.
"""

from celery import Celery

from app.core.config import get_settings
from app.scan_engine.job_queue import ScanJobPayload

_TASK_NAME = "app.workers.tasks.scan_tasks.execute_scan"


class CeleryJobQueue:
    def __init__(self, celery_app: Celery) -> None:
        self._celery_app = celery_app

    async def enqueue_scan(self, payload: ScanJobPayload, *, countdown_seconds: float = 0) -> str:
        settings = get_settings()
        result = self._celery_app.send_task(
            _TASK_NAME,
            kwargs={
                "scan_id": str(payload.scan_id),
                "tenant_id": str(payload.tenant_id),
                "correlation_id": payload.correlation_id,
            },
            queue=settings.scan_queue_name,
            countdown=countdown_seconds if countdown_seconds > 0 else None,
        )
        return str(result.id)

    async def cancel(self, job_id: str) -> None:
        self._celery_app.control.revoke(job_id, terminate=False)

    async def dead_letter_scan(self, payload: ScanJobPayload, *, reason: str) -> None:
        """Routes to `settings.scan_dead_letter_queue_name`, which no
        worker consumes this phase — purely a durable, inspectable marker
        for later manual review/replay tooling. `reason` is not passed as
        a task kwarg (nothing ever executes this message as a real task
        invocation); it's logged by the caller
        (`app.scan_engine.orchestrator`) instead."""
        settings = get_settings()
        self._celery_app.send_task(
            _TASK_NAME,
            kwargs={
                "scan_id": str(payload.scan_id),
                "tenant_id": str(payload.tenant_id),
                "correlation_id": payload.correlation_id,
            },
            queue=settings.scan_dead_letter_queue_name,
        )
