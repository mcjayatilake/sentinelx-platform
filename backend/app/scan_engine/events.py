"""Internal domain events, publishable without tight coupling.

`ScanOrchestrator` depends only on `EventPublisher.publish()` — never on
a concrete subscriber. `app.scan_engine.bootstrap.build_orchestrator`
wires an `InMemoryEventBus` with three built-in subscribers (durable
persistence to `ScanEvent`, metrics, structured logging); a future
notification system subscribes the same way, to `FindingCreated`/
`FindingUpdated`, without any orchestrator change.

Scoped per task execution (constructed fresh for each `execute_scan`
Celery task run), not a process-wide singleton — Celery workers are
separate processes, so there is no cross-process pub/sub need. Durability
comes from the DB-persistence subscriber, not from the bus itself.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.logging import get_logger
from app.models.enums import ScanStatus

logger = get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DomainEvent:
    scan_id: uuid.UUID
    tenant_id: uuid.UUID
    correlation_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class ScanQueued(DomainEvent):
    pass


@dataclass(frozen=True, slots=True)
class ScanStarted(DomainEvent):
    worker_id: str = ""


@dataclass(frozen=True, slots=True)
class ScanProgress(DomainEvent):
    stage: ScanStatus = ScanStatus.RUNNING
    percent: int = 0
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ScanCompleted(DomainEvent):
    pass


@dataclass(frozen=True, slots=True)
class ScanFailed(DomainEvent):
    error_code: str = ""
    error_summary: str = ""
    dead_letter: bool = False


@dataclass(frozen=True, slots=True)
class ScanCancelled(DomainEvent):
    pass


@dataclass(frozen=True, slots=True)
class ScanTimedOut(DomainEvent):
    pass


@dataclass(frozen=True, slots=True)
class FindingCreated(DomainEvent):
    finding_id: uuid.UUID = field(default_factory=uuid.uuid4)
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class FindingUpdated(DomainEvent):
    finding_id: uuid.UUID = field(default_factory=uuid.uuid4)
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class WorkerStarted:
    worker_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class WorkerStopped:
    worker_id: str
    occurred_at: datetime = field(default_factory=_now)


class EventPublisher(Protocol):
    async def publish(self, event: Any) -> None: ...


class InMemoryEventBus:
    """A process-local pub/sub bus. Each event type may have any number
    of subscribers; one handler raising never breaks `publish()` for the
    other subscribers, or for the event's producer."""

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable[[Any], Awaitable[None]]]] = {}

    def subscribe(self, event_type: type, handler: Callable[[Any], Awaitable[None]]) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    async def publish(self, event: Any) -> None:
        for handler in self._subscribers.get(type(event), []):
            try:
                await handler(event)
            except Exception:
                logger.error(
                    "scan_engine.event_handler_failed",
                    event_type=type(event).__name__,
                    handler=getattr(handler, "__qualname__", repr(handler)),
                    exc_info=True,
                )
