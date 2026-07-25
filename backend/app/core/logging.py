"""Structured logging configuration.

Configures stdlib `logging` + `structlog` to emit structured JSON (or
human-readable console output in local development), including a
per-request correlation ID. Application code should call `get_logger(__name__)`
rather than using `print()` or the stdlib logging API directly.
"""

import logging
import sys
from typing import Any, cast

import structlog

from app.core.config import get_settings
from app.core.log_redaction import redact_log_secrets

_CONFIGURED = False


def configure_logging() -> None:
    """Configure structlog + stdlib logging. Idempotent; safe to call multiple times."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings = get_settings()
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # After format_exc_info (so rendered stack traces are covered
        # too) and before the renderer, applied to every log line via
        # both `structlog.configure()` and `foreign_pre_chain` below —
        # see app.core.log_redaction for what this catches and why.
        redact_log_secrets,
    ]

    if settings.log_format == "json":
        renderer: Any = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(log_level)

    for noisy_logger in ("uvicorn.access", "uvicorn.error"):
        logging.getLogger(noisy_logger).handlers = [handler]
        logging.getLogger(noisy_logger).propagate = False

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structured logger bound to `name` (typically `__name__`)."""
    return cast(structlog.stdlib.BoundLogger, structlog.get_logger(name))
