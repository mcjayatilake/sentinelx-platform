"""`RetryPolicy` backoff/should_retry decisions and `classify_error`."""

from app.scan_engine.exceptions import (
    PermanentScanError,
    ScanConfigError,
    TransientScanError,
)
from app.scan_engine.retry import ErrorClass, RetryPolicy, classify_error


def test_should_retry_true_while_attempts_remain() -> None:
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=60)
    assert policy.should_retry(1, max_attempts=3) is True
    assert policy.should_retry(2, max_attempts=3) is True


def test_should_retry_false_once_max_attempts_reached() -> None:
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=60)
    assert policy.should_retry(3, max_attempts=3) is False
    assert policy.should_retry(4, max_attempts=3) is False


def test_should_retry_respects_a_scan_specific_max_attempts() -> None:
    # max_attempts is per-scan configuration (Scan.max_attempts), not a
    # property of the policy itself — the same policy instance must honor
    # whatever value a given scan was created with.
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=60)
    assert policy.should_retry(1, max_attempts=1) is False
    assert policy.should_retry(1, max_attempts=5) is True


def test_next_delay_seconds_grows_exponentially_without_jitter() -> None:
    policy = RetryPolicy(base_delay_seconds=2, max_delay_seconds=1000, jitter=False)
    assert policy.next_delay_seconds(1) == 2
    assert policy.next_delay_seconds(2) == 4
    assert policy.next_delay_seconds(3) == 8


def test_next_delay_seconds_capped_at_max() -> None:
    policy = RetryPolicy(base_delay_seconds=100, max_delay_seconds=150, jitter=False)
    assert policy.next_delay_seconds(5) == 150


def test_next_delay_seconds_jitter_stays_within_bounds() -> None:
    policy = RetryPolicy(base_delay_seconds=10, max_delay_seconds=1000, jitter=True)
    for attempt in range(1, 5):
        delay = policy.next_delay_seconds(attempt)
        upper_bound = min(10 * (2 ** (attempt - 1)), 1000)
        assert 0 <= delay <= upper_bound


def test_classify_error_transient() -> None:
    assert classify_error(TransientScanError("network blip")) == ErrorClass.TRANSIENT


def test_classify_error_permanent_for_explicit_permanent_error() -> None:
    assert classify_error(PermanentScanError("bad target")) == ErrorClass.PERMANENT


def test_classify_error_permanent_for_unrecognized_error() -> None:
    # Fail-safe: an error type the scan engine doesn't know about is
    # treated as PERMANENT, not retried forever.
    assert classify_error(ScanConfigError("bad config")) == ErrorClass.PERMANENT
    assert classify_error(ValueError("unexpected")) == ErrorClass.PERMANENT
