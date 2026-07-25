"""Central error-sanitization boundary.

Raw exception text must never reach a scan-facing database field, an API
response, a domain event, or an outbox row — a future scanner's exception
could carry credentials, bearer tokens, cookies, or an authenticated URL
embedded in whatever a subprocess/HTTP client happened to print. This is
the one place that decides what a caught exception becomes once it needs
to be persisted or shown to a caller.

Fully static taxonomy, deliberately: `sanitize_exception()` maps an
exception's *type* to a fixed, generic category and a fixed, generic
summary string — never anything derived from the exception's own message.
A regex-redaction approach (see `app.core.logging`'s log-line redaction,
which exists for a different surface — operator-only structured logs)
can always miss a secret shape it wasn't written to recognize; a
type-driven static mapping cannot leak content it never reads in the
first place. No real scanner exists yet to build a redact-but-keep-
some-content corpus against, so "safe by construction" is the only
option that doesn't rest on an untested assumption.

See docs/decisions/0008-transaction-and-concurrency-model.md.
"""

from dataclasses import dataclass
from enum import StrEnum


class ErrorCategory(StrEnum):
    NETWORK = "network"
    TIMEOUT = "timeout"
    CONFIGURATION = "configuration"
    TARGET_UNREACHABLE = "target_unreachable"
    RESOURCE_LIMIT = "resource_limit"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class SanitizedError:
    """`code` is a class name (e.g. `"TransientScanError"`) — safe as-is,
    never exception message content. `summary` is one of a fixed set of
    generic, category-level sentences — see `_SUMMARY_BY_CATEGORY`."""

    code: str
    category: ErrorCategory
    summary: str


# Keyed by `type(exc).__name__`, not the exception class itself, so this
# stays a pure lookup table with no import dependency on
# `app.scan_engine.exceptions` (avoiding a circular import, since that
# module is imported by code that also needs this one) or on any future
# scanner-adapter-specific exception module.
_CATEGORY_BY_EXCEPTION_TYPE: dict[str, ErrorCategory] = {
    "TransientScanError": ErrorCategory.NETWORK,
    "PermanentScanError": ErrorCategory.INTERNAL,
    "ScanConfigError": ErrorCategory.CONFIGURATION,
    "ScanTimeoutError": ErrorCategory.TIMEOUT,
    "TimeoutError": ErrorCategory.TIMEOUT,
    "ConnectionError": ErrorCategory.NETWORK,
    "ConnectionRefusedError": ErrorCategory.NETWORK,
    "ConnectionResetError": ErrorCategory.NETWORK,
    "OSError": ErrorCategory.NETWORK,
    "MemoryError": ErrorCategory.RESOURCE_LIMIT,
}

_SUMMARY_BY_CATEGORY: dict[ErrorCategory, str] = {
    ErrorCategory.NETWORK: (
        "A network error occurred while communicating with the scan target or tool."
    ),
    ErrorCategory.TIMEOUT: "The operation did not complete within its configured timeout.",
    ErrorCategory.CONFIGURATION: "The scan configuration was invalid or incomplete.",
    ErrorCategory.TARGET_UNREACHABLE: "The scan target could not be reached.",
    ErrorCategory.RESOURCE_LIMIT: (
        "A resource limit (memory, disk, or execution time) was exceeded."
    ),
    ErrorCategory.INTERNAL: "An internal error occurred while executing the scan.",
}


def sanitize_exception(exc: Exception) -> SanitizedError:
    """Never inspects `str(exc)` or `exc.args` — only `type(exc).__name__`,
    which is safe by construction (a Python class name, not
    caller-controlled data)."""
    category = _CATEGORY_BY_EXCEPTION_TYPE.get(type(exc).__name__, ErrorCategory.INTERNAL)
    return SanitizedError(
        code=type(exc).__name__,
        category=category,
        summary=_SUMMARY_BY_CATEGORY[category],
    )
