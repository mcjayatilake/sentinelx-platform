"""`FindingPipeline` — the deduplicator -> repository -> audit ->
notifications(future) half of the finding pipeline. Reuses
`FindingRepository.record_detection()` for dedup persistence; this
pipeline's own job is fingerprinting + deciding which event/audit action
to emit."""

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.enums import AssetType, FindingConfidence, FindingSeverity
from app.repositories.asset_repository import AssetRepository
from app.repositories.audit_event_repository import AuditEventRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.context import ScanContext
from app.scan_engine.events import FindingCreated, FindingUpdated, InMemoryEventBus
from app.scan_engine.metrics import NoOpMetrics
from app.scan_engine.pipeline.pipeline import FindingPipeline
from app.scan_engine.pipeline.stages import NormalizedFinding
from app.scan_engine.storage.local import LocalFilesystemArtifactStorage
from app.schemas.asset import AssetCreate
from app.schemas.common import PaginationParams
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.tenant import TenantCreate


def _collector(sink: list[Any]) -> Callable[[Any], Any]:
    async def _handler(event: Any) -> None:
        sink.append(event)

    return _handler


async def _make_context(session: AsyncSession, tmp_path, slug: str) -> ScanContext:
    tenant = await TenantRepository(session).create(TenantCreate(name=slug, slug=slug))
    project = await ProjectRepository(session).create(
        ProjectCreate(tenant_id=tenant.id, name=slug, slug=slug)
    )
    asset = await AssetRepository(session).create(
        AssetCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_type=AssetType.WEBSITE,
            name=slug,
            locator="https://example.com",
        )
    )
    scan = await ScanRepository(session).create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="fake"
        )
    )
    return ScanContext(
        tenant_id=tenant.id,
        scan_id=scan.id,
        project_id=project.id,
        asset_id=asset.id,
        scanner_type="fake",
        requested_by_user_id=None,
        correlation_id="corr-1",
        config={},
        timeout_seconds=60,
        artifact_storage=LocalFilesystemArtifactStorage(tmp_path),
        metrics=NoOpMetrics(),
        logger=get_logger("test"),
    )


def _finding(
    rule_id: str = "rule-1", locator: str = "https://example.com/login"
) -> NormalizedFinding:
    return NormalizedFinding(
        title="Reflected XSS",
        description="XSS in form field",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        source_tool="fake",
        external_reference=None,
        remediation=None,
        rule_id=rule_id,
        locator=locator,
    )


def _pipeline(session: AsyncSession, bus: InMemoryEventBus) -> FindingPipeline:
    return FindingPipeline(
        finding_repository=FindingRepository(session),
        audit_repository=AuditEventRepository(session),
        event_publisher=bus,
    )


async def test_new_finding_is_created_and_publishes_finding_created(
    db_session: AsyncSession, tmp_path
) -> None:
    context = await _make_context(db_session, tmp_path, "fp1")
    bus = InMemoryEventBus()
    created_events: list[FindingCreated] = []
    bus.subscribe(FindingCreated, _collector(created_events))

    findings = await _pipeline(db_session, bus).process(context, [_finding()])

    assert len(findings) == 1
    assert findings[0].title == "Reflected XSS"
    assert len(created_events) == 1
    assert created_events[0].fingerprint == findings[0].fingerprint


async def test_redetecting_the_same_finding_updates_last_seen_and_publishes_finding_updated(
    db_session: AsyncSession, tmp_path
) -> None:
    context = await _make_context(db_session, tmp_path, "fp2")
    bus = InMemoryEventBus()
    created_events: list[FindingCreated] = []
    updated_events: list[FindingUpdated] = []
    bus.subscribe(FindingCreated, _collector(created_events))
    bus.subscribe(FindingUpdated, _collector(updated_events))

    pipeline = _pipeline(db_session, bus)
    first = await pipeline.process(context, [_finding()])
    second = await pipeline.process(context, [_finding()])

    assert first[0].id == second[0].id  # same underlying Finding row
    assert len(created_events) == 1
    assert len(updated_events) == 1


async def test_findings_with_different_fingerprints_are_distinct(
    db_session: AsyncSession, tmp_path
) -> None:
    context = await _make_context(db_session, tmp_path, "fp3")
    bus = InMemoryEventBus()

    findings = await _pipeline(db_session, bus).process(
        context, [_finding(rule_id="rule-1"), _finding(rule_id="rule-2")]
    )

    assert len({f.fingerprint for f in findings}) == 2


async def test_audit_event_recorded_for_each_finding(db_session: AsyncSession, tmp_path) -> None:
    context = await _make_context(db_session, tmp_path, "fp4")
    bus = InMemoryEventBus()
    audit_repo = AuditEventRepository(db_session)

    await _pipeline(db_session, bus).process(context, [_finding()])

    page = await audit_repo.list_by_tenant(context.tenant_id, PaginationParams(limit=10, offset=0))
    actions = [event.action for event in page.items]
    assert "finding.detected" in actions


async def test_redetection_records_finding_redetected_audit_action(
    db_session: AsyncSession, tmp_path
) -> None:
    context = await _make_context(db_session, tmp_path, "fp5")
    bus = InMemoryEventBus()
    audit_repo = AuditEventRepository(db_session)
    pipeline = _pipeline(db_session, bus)

    await pipeline.process(context, [_finding()])
    await pipeline.process(context, [_finding()])

    page = await audit_repo.list_by_tenant(context.tenant_id, PaginationParams(limit=10, offset=0))
    actions = [event.action for event in page.items]
    assert actions.count("finding.detected") == 1
    assert actions.count("finding.redetected") == 1
