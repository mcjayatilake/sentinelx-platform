"""`CeleryJobQueue` — the `JobQueue` adapter over a real Celery client
against the local Redis broker (no worker needs to consume these; a
`send_task` client call only needs a reachable broker, not a live
worker)."""

import uuid

from app.scan_engine.job_queue import ScanJobPayload
from app.workers.celery_app import celery_app
from app.workers.job_queue_celery import CeleryJobQueue


def _payload() -> ScanJobPayload:
    return ScanJobPayload(scan_id=uuid.uuid4(), tenant_id=uuid.uuid4(), correlation_id="corr-1")


async def test_enqueue_scan_returns_a_job_id() -> None:
    job_queue = CeleryJobQueue(celery_app)
    job_id = await job_queue.enqueue_scan(_payload())
    assert job_id


async def test_enqueue_scan_with_countdown_does_not_raise() -> None:
    job_queue = CeleryJobQueue(celery_app)
    job_id = await job_queue.enqueue_scan(_payload(), countdown_seconds=5)
    assert job_id


async def test_cancel_does_not_raise_for_an_unknown_job_id() -> None:
    job_queue = CeleryJobQueue(celery_app)
    await job_queue.cancel("nonexistent-job-id")


async def test_dead_letter_scan_does_not_raise() -> None:
    job_queue = CeleryJobQueue(celery_app)
    await job_queue.dead_letter_scan(_payload(), reason="max_attempts_exhausted")
