"""`JobQueue` — the seam between the orchestrator and whatever actually
runs a scan job.

No Celery import anywhere in this module, deliberately: `app.scan_engine`
must not depend on `app.workers` (the reverse dependency —
`app.workers.tasks.scan_tasks` calling into `app.scan_engine.orchestrator`
— is the one that's allowed; a dependency the other way would be
circular). `app.workers.job_queue_celery.CeleryJobQueue` is the concrete
adapter for today; a future `RQJobQueue` or similar implements this same
Protocol with zero orchestrator changes, since all retry/backoff
*decision-making* lives in `app.scan_engine.retry.RetryPolicy`, above
this abstraction — `JobQueue` only ever does what it's told.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ScanJobPayload:
    scan_id: uuid.UUID
    tenant_id: uuid.UUID
    correlation_id: str


def scan_job_payload_to_dict(payload: ScanJobPayload) -> dict[str, str]:
    """The single source of truth for `ScanJobPayload`'s wire shape —
    used both when `ScanService`/`ScanOrchestrator` write a
    `ScanJobOutbox.payload` row and when `app.workers.tasks.
    outbox_dispatcher` reads one back before calling `enqueue_scan()`.
    A plain `dict[str, str]`, not a Pydantic model: this is the exact
    JSONB shape stored, and the round trip only ever needs these three
    fields."""
    return {
        "scan_id": str(payload.scan_id),
        "tenant_id": str(payload.tenant_id),
        "correlation_id": payload.correlation_id,
    }


def scan_job_payload_from_dict(data: dict[str, object]) -> ScanJobPayload:
    return ScanJobPayload(
        scan_id=uuid.UUID(str(data["scan_id"])),
        tenant_id=uuid.UUID(str(data["tenant_id"])),
        correlation_id=str(data["correlation_id"]),
    )


class JobQueue(Protocol):
    async def enqueue_scan(self, payload: ScanJobPayload, *, countdown_seconds: float = 0) -> str:
        """Schedules `payload` for execution, optionally after
        `countdown_seconds` (used for retry backoff delay). Returns an
        opaque job id the queue implementation can later use to
        `cancel()`."""
        ...

    async def cancel(self, job_id: str) -> None:
        """Best-effort: asks the queue to not run (or stop running) the
        job. Not guaranteed to preempt a job already executing — see
        `docs/orchestrator.md`'s cooperative-cancellation note."""
        ...

    async def dead_letter_scan(self, payload: ScanJobPayload, *, reason: str) -> None:
        """Routes `payload` to the dead-letter queue for later manual
        inspection/replay tooling — no worker consumes that queue this
        phase (explicit non-goal; see docs/orchestrator.md). Called once
        a scan has exhausted `max_attempts` or hit a permanent error;
        the scan's `Scan.status` is already finalized `FAILED` by the
        time this is called, so a failure here must never raise past the
        orchestrator — it's best-effort observability, not the source of
        truth for the scan's outcome."""
        ...
