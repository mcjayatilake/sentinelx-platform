"""Structured-log secret redaction — the operator-only log surface's
counterpart to `app.core.error_sanitization` (which covers the
persisted/API-facing surface).

Deliberately the opposite design choice from that module: logs are meant
to carry full diagnostic detail for operators, so this redacts *known
secret shapes* out of otherwise-unrestricted text via regex, rather than
replacing the whole message with a fixed generic string. A regex
approach can miss a secret shape it wasn't written to recognize — that
tradeoff is acceptable here specifically because it's not the only line
of defense: `app.core.error_sanitization` already keeps raw exception
text out of every *other* surface (DB fields, API responses, domain
events) before it would ever reach a log call in the first place, and
security-sensitive code paths (`app.services.auth_service`,
`app.services.api_key_service`) never log full secrets to begin with —
this processor is a defense-in-depth backstop across every log line, not
the primary control.

Wired into `app.core.logging`'s `shared_processors`, after
`format_exc_info` (so it also covers rendered stack traces) and before
the renderer, so it applies uniformly to every log line — structlog-
native and stdlib-originated (uvicorn, SQLAlchemy, Celery) alike, via
`foreign_pre_chain`.
"""

import re
from typing import Any

_REDACTED = "[REDACTED]"

# Order matters: more specific patterns first, so a generic key=value
# match doesn't fire on text a more specific pattern already redacted.
_SIMPLE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_.+/=]+"), f"Bearer {_REDACTED}"),
    (re.compile(r"(?i)\bAuthorization\s*:\s*\S+"), f"Authorization: {_REDACTED}"),
    (re.compile(r"(?i)\bCookie\s*:\s*[^\r\n]+"), f"Cookie: {_REDACTED}"),
    # SentinelX API keys: "sx_<prefix>.<secret>" (see
    # app.services.api_key_service._PREFIX_LABEL / generate_secret).
    (re.compile(r"\bsx_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{4,}\b"), _REDACTED),
    # JWT-shaped strings (three base64url segments; a real JWT header
    # always starts "eyJ" — base64url("{\"")).
    (
        re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        _REDACTED,
    ),
    # Credentials embedded in a URL: scheme://user:pass@host
    (re.compile(r"://[^/\s:@]+:[^/\s:@]+@"), f"://{_REDACTED}@"),
]

_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|refresh[_-]?token)"
    r"(\s*[=:]\s*)([^\s&,;\"']+)"
)

# Structured-field names (structlog kwargs, e.g. `logger.info("x",
# password=raw_password)`) redacted by key regardless of their value's
# shape — the content-regex patterns above only catch a secret
# embedded *inside* a string, not a field whose entire value simply
# *is* one with nothing to pattern-match against.
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "token",
        "secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "hashed_secret",
        "hashed_password",
        "client_secret",
    }
)


def _is_sensitive_key(key: object) -> bool:
    return str(key).lower().replace("-", "_") in _SENSITIVE_FIELD_NAMES


def _redact_key_value(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{_REDACTED}"


def redact_text(value: str) -> str:
    for pattern, replacement in _SIMPLE_PATTERNS:
        value = pattern.sub(replacement, value)
    return _KEY_VALUE_PATTERN.sub(_redact_key_value, value)


def _redact_mapping(mapping: dict[Any, Any]) -> dict[Any, Any]:
    return {
        key: _REDACTED if _is_sensitive_key(key) else _redact_value(value)
        for key, value in mapping.items()
    }


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(v) for v in value)
    return value


def redact_log_secrets(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """A structlog processor: redacts known secret shapes from every
    string value in `event_dict` (including nested dicts/lists —
    `event_dict["exception"]`'s rendered traceback text, in particular),
    and any field whose *name* (top-level or nested) is a known
    sensitive one, regardless of its value's shape."""
    return _redact_mapping(event_dict)
