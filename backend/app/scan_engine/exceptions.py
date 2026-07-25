"""Scan-engine-internal exception hierarchy.

These are raised and caught within `app.scan_engine`/`app.workers.tasks`
(worker-side). They are distinct from `app.services.scan_service`'s
smaller `ScanServiceError` family, which is the only exception hierarchy
API endpoints ever see — scan execution happens in a Celery task, not
inline in a request, so there is no HTTP response for these to become.
"""


class ScanEngineError(Exception):
    """Base class for every scan-engine-internal error."""


class ScannerNotRegisteredError(ScanEngineError):
    """No plugin is registered for a scan's `scanner_type`."""


class ScanConfigError(ScanEngineError):
    """A scan's `config` failed a plugin's `validate_config`."""


class InvalidScanTransitionError(ScanEngineError):
    """An attempted `ScanStatus` transition is not in the valid-edges table.

    Detected in-memory, from `ScanStateMachine.assert_valid()`, before any
    SQL is issued — a cheap fail-fast against an edge that is illegal
    regardless of concurrent activity. See `StaleScanStateError` for the
    matching SQL-level guard against a race with a *concurrent* writer."""


class StaleScanStateError(ScanEngineError):
    """A `ScanStatus` transition's conditional `UPDATE ... WHERE status =
    <expected>` (or `claim_for_execution`'s `WHERE status='QUEUED' AND
    attempt=<expected>`) matched zero rows — some other transaction
    already moved this scan (or claimed this attempt) first.

    This is the actual source-of-truth enforcement against races; the
    in-memory `assert_valid()` check alone cannot detect a transition
    that raced ahead between that check and this statement executing.
    Always means "the scan is no longer in the state the caller believed
    it was" — never a data-corruption signal. See
    docs/decisions/0008-transaction-and-concurrency-model.md."""


class ScanCancelledError(ScanEngineError):
    """Raised internally when a cooperative cancellation check detects the
    scan was cancelled by a concurrent API call."""


class ScanTimeoutError(ScanEngineError):
    """Raised internally when a scan's configured timeout elapses."""


class TransientScanError(ScanEngineError):
    """A retryable failure (e.g. a network blip talking to a scan target
    or tool). `ScanOrchestrator` re-queues the scan (see
    `app.scan_engine.retry.RetryPolicy`) up to `Scan.max_attempts`."""


class PermanentScanError(ScanEngineError):
    """A non-retryable failure. `ScanOrchestrator` finalizes the scan as
    `FAILED` immediately, regardless of remaining attempts."""


class ArtifactPathError(ScanEngineError):
    """An artifact storage operation was given a `name` that would escape
    its tenant/scan namespace (path traversal)."""
