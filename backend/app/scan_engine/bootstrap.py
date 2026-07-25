"""Composition root for the scan engine.

`build_default_registry()` mirrors `app.workers.celery_app`'s
`include=[]` — empty, with scanners registered here as they're
implemented. `build_orchestrator()` wires everything else: repositories,
an `InMemoryEventBus` with three built-in subscribers (durable
persistence to `ScanEvent`, metrics, structured logging), artifact
storage selected from `Settings`, and the retry policy.
"""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.repositories.audit_event_repository import AuditEventRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.scan_event_repository import ScanEventRepository
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.scan_engine.events import (
    DomainEvent,
    FindingCreated,
    FindingUpdated,
    InMemoryEventBus,
    ScanCancelled,
    ScanCompleted,
    ScanFailed,
    ScanProgress,
    ScanQueued,
    ScanStarted,
    ScanTimedOut,
)
from app.scan_engine.job_queue import JobQueue
from app.scan_engine.metrics import Metrics, NoOpMetrics
from app.scan_engine.orchestrator import ScanOrchestrator
from app.scan_engine.pipeline.pipeline import FindingPipeline
from app.scan_engine.registry import ScannerRegistry
from app.scan_engine.retry import RetryPolicy
from app.scan_engine.state_machine import ScanStateMachine
from app.scan_engine.storage.interfaces import ArtifactStorage
from app.scan_engine.storage.local import LocalFilesystemArtifactStorage
from app.schemas.scan_event import ScanEventCreate

_EVENT_TYPE_BY_CLASS: dict[type, str] = {
    ScanQueued: "scan.queued",
    ScanStarted: "scan.started",
    ScanProgress: "scan.progress",
    ScanCompleted: "scan.completed",
    ScanFailed: "scan.failed",
    ScanCancelled: "scan.cancelled",
    ScanTimedOut: "scan.timed_out",
    FindingCreated: "finding.created",
    FindingUpdated: "finding.updated",
}


def build_default_registry() -> ScannerRegistry:
    """Empty — scanners are registered here as they're implemented."""
    return ScannerRegistry()


def build_artifact_storage(settings: Settings | None = None) -> ArtifactStorage:
    settings = settings or get_settings()
    if settings.artifact_storage_backend == "local":
        return LocalFilesystemArtifactStorage(Path(settings.artifact_storage_local_path))
    raise ValueError(f"Unsupported artifact_storage_backend: {settings.artifact_storage_backend!r}")


def build_retry_policy(settings: Settings | None = None) -> RetryPolicy:
    settings = settings or get_settings()
    return RetryPolicy(
        base_delay_seconds=settings.scan_retry_backoff_base_seconds,
        max_delay_seconds=settings.scan_retry_backoff_max_seconds,
    )


def _to_scan_event_create(event: DomainEvent, event_type: str) -> ScanEventCreate:
    stage: str | None = None
    percent: int | None = None
    message: str | None = None
    worker_id: str | None = None
    metadata: dict[str, object] | None = None

    if isinstance(event, ScanProgress):
        stage, percent, message = event.stage.value, event.percent, event.message
    elif isinstance(event, ScanStarted):
        worker_id = event.worker_id
    elif isinstance(event, ScanFailed):
        message = event.error_summary
        metadata = {"error_code": event.error_code, "dead_letter": event.dead_letter}
    elif isinstance(event, FindingCreated | FindingUpdated):
        metadata = {"finding_id": str(event.finding_id), "fingerprint": event.fingerprint}

    return ScanEventCreate(
        tenant_id=event.tenant_id,
        scan_id=event.scan_id,
        event_type=event_type,
        stage=stage,
        progress_percent=percent,
        worker_id=worker_id,
        correlation_id=event.correlation_id,
        message=message,
        event_metadata=metadata,
    )


def _wire_subscribers(bus: InMemoryEventBus, session: AsyncSession, metrics: Metrics) -> None:
    scan_events = ScanEventRepository(session)
    logger = get_logger("app.scan_engine.events")

    for event_cls, event_type in _EVENT_TYPE_BY_CLASS.items():

        async def _persist(event: DomainEvent, _event_type: str = event_type) -> None:
            await scan_events.create(_to_scan_event_create(event, _event_type))
            # Commits immediately, not just flushes: this is the durability
            # boundary that makes progress observable from a separate
            # session while a scan is still running (see
            # docs/decisions/0008-transaction-and-concurrency-model.md).
            # In the request-scoped context (app.services.scan_service,
            # via build_event_bus) this is a harmless extra commit inside
            # a request that already commits once at its own boundary.
            await session.commit()

        async def _observe(event: DomainEvent, _event_type: str = event_type) -> None:
            metrics.increment(f"scan_engine.event.{_event_type}")

        async def _log(event: DomainEvent, _event_type: str = event_type) -> None:
            logger.info(
                "scan_engine.event",
                event_type=_event_type,
                scan_id=str(event.scan_id),
                tenant_id=str(event.tenant_id),
                correlation_id=event.correlation_id,
            )

        bus.subscribe(event_cls, _persist)
        bus.subscribe(event_cls, _observe)
        bus.subscribe(event_cls, _log)


def build_event_bus(session: AsyncSession, metrics: Metrics | None = None) -> InMemoryEventBus:
    """A wired `InMemoryEventBus` (durable persistence + metrics +
    logging subscribers attached) for callers outside the orchestrator
    that still need to publish/persist a domain event consistently —
    e.g. `app.services.scan_service` publishing `ScanQueued` at scan
    creation, before any worker/orchestrator run exists."""
    bus = InMemoryEventBus()
    _wire_subscribers(bus, session, metrics or NoOpMetrics())
    return bus


def build_orchestrator(
    session: AsyncSession,
    *,
    registry: ScannerRegistry | None = None,
    job_queue: JobQueue,
    settings: Settings | None = None,
    metrics: Metrics | None = None,
) -> ScanOrchestrator:
    settings = settings or get_settings()
    metrics = metrics or NoOpMetrics()

    bus = build_event_bus(session, metrics)

    pipeline = FindingPipeline(
        finding_repository=FindingRepository(session),
        audit_repository=AuditEventRepository(session),
        event_publisher=bus,
    )

    return ScanOrchestrator(
        session=session,
        scan_repository=ScanRepository(session),
        outbox_repository=ScanOutboxRepository(session),
        state_machine=ScanStateMachine(session),
        registry=registry or build_default_registry(),
        finding_pipeline=pipeline,
        event_publisher=bus,
        artifact_storage=build_artifact_storage(settings),
        metrics=metrics,
        retry_policy=build_retry_policy(settings),
        job_queue=job_queue,
        logger=get_logger("app.scan_engine.orchestrator"),
    )
