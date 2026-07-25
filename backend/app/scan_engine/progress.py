"""`ScanProgressSnapshot` — the read model behind `GET /scans/{id}/progress`.

Built from the latest `scan.progress` `ScanEvent` row
(`ScanEventRepository.get_latest_progress`), not a duplicated mutable
column on `Scan` — `Scan` stays closer to an immutable history record,
and there is one source of truth for "where is this scan right now."
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.models.enums import ScanStatus


@dataclass(frozen=True, slots=True)
class ScanProgressSnapshot:
    scan_id: uuid.UUID
    status: ScanStatus
    stage: str
    percent: int
    started_at: datetime | None
    estimated_completion_at: datetime | None
    worker_id: str | None
    updated_at: datetime


def estimate_completion(
    *, started_at: datetime | None, percent: int, now: datetime
) -> datetime | None:
    """Best-effort linear extrapolation from elapsed time and percent
    complete — not a promise, just a UI hint. `None` when there isn't
    enough information yet (`started_at` unknown, or `percent` is 0)."""
    if started_at is None or percent <= 0:
        return None
    elapsed = (now - started_at).total_seconds()
    if elapsed <= 0:
        return None
    total_estimated = elapsed / percent * 100
    remaining = total_estimated - elapsed
    if remaining <= 0:
        return now
    return now + timedelta(seconds=remaining)
