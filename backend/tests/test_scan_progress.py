"""`estimate_completion` — best-effort linear extrapolation."""

from datetime import UTC, datetime, timedelta

from app.scan_engine.progress import estimate_completion


def test_estimate_completion_none_without_started_at() -> None:
    assert estimate_completion(started_at=None, percent=50, now=datetime.now(UTC)) is None


def test_estimate_completion_none_at_zero_percent() -> None:
    started_at = datetime.now(UTC) - timedelta(seconds=10)
    assert estimate_completion(started_at=started_at, percent=0, now=datetime.now(UTC)) is None


def test_estimate_completion_extrapolates_linearly() -> None:
    now = datetime.now(UTC)
    started_at = now - timedelta(seconds=50)  # 50s elapsed at 50% -> ~50s remaining
    estimated = estimate_completion(started_at=started_at, percent=50, now=now)
    assert estimated is not None
    remaining = (estimated - now).total_seconds()
    assert 45 <= remaining <= 55


def test_estimate_completion_returns_now_when_effectively_complete() -> None:
    now = datetime.now(UTC)
    started_at = now - timedelta(seconds=100)
    estimated = estimate_completion(started_at=started_at, percent=100, now=now)
    assert estimated == now


def test_estimate_completion_none_when_started_in_future() -> None:
    now = datetime.now(UTC)
    started_at = now + timedelta(seconds=10)
    assert estimate_completion(started_at=started_at, percent=50, now=now) is None
