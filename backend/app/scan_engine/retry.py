"""Retry/backoff policy.

Queue-agnostic by design: `RetryPolicy` decides *whether* and *when* to
retry; the orchestrator records that decision as a `ScanJobOutbox` row
(`countdown_seconds` carried through to the dispatcher's eventual
`JobQueue.enqueue_scan(payload, countdown_seconds=...)` call — see
`app.workers.tasks.outbox_dispatcher`). This is deliberately not
Celery's own `self.retry()`/`autoretry_for`/`max_retries` — keeping
retry logic above the `JobQueue` Protocol is what lets a future
`RQJobQueue` (or any other adapter) swap in with zero orchestrator
changes.

`max_attempts` is deliberately not a field on `RetryPolicy` itself: it is
per-scan configuration (`Scan.max_attempts`, settable at
`POST /scans` — see `app.schemas.scan.ScanCreateRequest`), so
`should_retry()` takes it as a call-time argument instead. A `RetryPolicy`
instance only owns the backoff *shape* (base/max delay, jitter), which is
process-wide configuration, not per-scan.
"""

import random
from dataclasses import dataclass
from enum import StrEnum

from app.scan_engine.exceptions import TransientScanError


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_delay_seconds: float
    max_delay_seconds: float
    jitter: bool = True

    def should_retry(self, attempt: int, max_attempts: int) -> bool:
        return attempt < max_attempts

    def next_delay_seconds(self, attempt: int) -> float:
        """`attempt` is the attempt number that just failed (1-indexed).
        Exponential backoff capped at `max_delay_seconds`, with optional
        full jitter (uniform in `[0, delay]`) to avoid a retry stampede
        when many scans fail around the same time."""
        delay: float = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        if self.jitter:
            return random.uniform(0, delay)
        return delay


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    PERMANENT = "permanent"


def classify_error(exc: Exception) -> ErrorClass:
    """`TransientScanError` (and subclasses) -> TRANSIENT; everything
    else, including an unclassified/unexpected exception, -> PERMANENT.
    Fail-safe: an unrecognized error retrying forever is worse than an
    unrecognized error surfacing as a single, investigable failure."""
    if isinstance(exc, TransientScanError):
        return ErrorClass.TRANSIENT
    return ErrorClass.PERMANENT
