"""`FindingPipeline` — the deduplicator -> repository -> audit ->
notifications(future) half of "scanner output -> parser -> normaliser ->
deduplicator -> repository -> audit -> notifications".

The first half (parser -> normaliser) is `ScannerPlugin.collect_results()`
+ `normalize_findings()` (see `app.scan_engine.interfaces`) — those are
inherently scanner-specific ("no scanner-specific logic outside
adapters"), so they're plugin responsibilities, not this pipeline's.
`app.scan_engine.pipeline.stages.Parser`/`Normalizer` and
`app.scan_engine.pipeline.identity`'s reference implementations are the
building blocks a plugin's `collect_results`/`normalize_findings` compose
with internally; this pipeline consumes their *output*
(`list[NormalizedFinding]`), not scanner-specific raw output.

Calls the *existing* `FindingRepository.record_detection()` for the
deduplicator+repository stages rather than reimplementing dedup
persistence — this pipeline's only new responsibility is computing the
fingerprint (`app.scan_engine.pipeline.fingerprint.compute_fingerprint`)
and deciding which event/audit action to emit based on whether the
fingerprint was already known.

"notifications (future)" has no stub class here: `FindingCreated`/
`FindingUpdated` (published on `EventPublisher`) already *are* the
extension point a future notification subscriber attaches to — nothing
else to build until one exists.
"""

from app.models.enums import AuditOutcome
from app.models.finding import Finding
from app.repositories.audit_event_repository import AuditEventRepository
from app.repositories.finding_repository import FindingRepository
from app.scan_engine.context import ScanContext
from app.scan_engine.events import EventPublisher, FindingCreated, FindingUpdated
from app.scan_engine.pipeline.fingerprint import compute_fingerprint
from app.scan_engine.pipeline.stages import NormalizedFinding
from app.schemas.audit_event import AuditEventCreate
from app.schemas.finding import FindingCreate


class FindingPipeline:
    def __init__(
        self,
        finding_repository: FindingRepository,
        audit_repository: AuditEventRepository,
        event_publisher: EventPublisher,
    ) -> None:
        self._finding_repository = finding_repository
        self._audit_repository = audit_repository
        self._event_publisher = event_publisher

    async def process(
        self, context: ScanContext, normalized_findings: list[NormalizedFinding]
    ) -> list[Finding]:
        findings: list[Finding] = []

        for normalized in normalized_findings:
            fingerprint = compute_fingerprint(
                context.scanner_type,
                normalized.rule_id,
                normalized.locator,
                normalized.description or "",
            )

            existing = await self._finding_repository.get_by_fingerprint(
                context.tenant_id, context.asset_id, fingerprint
            )
            is_redetection = existing is not None

            finding = await self._finding_repository.record_detection(
                FindingCreate(
                    tenant_id=context.tenant_id,
                    project_id=context.project_id,
                    asset_id=context.asset_id,
                    scan_id=context.scan_id,
                    title=normalized.title,
                    description=normalized.description,
                    severity=normalized.severity,
                    confidence=normalized.confidence,
                    source_tool=normalized.source_tool,
                    external_reference=normalized.external_reference,
                    fingerprint=fingerprint,
                    remediation=normalized.remediation,
                )
            )
            findings.append(finding)

            await self._audit_repository.create(
                AuditEventCreate(
                    tenant_id=context.tenant_id,
                    actor_user_id=context.requested_by_user_id,
                    action="finding.redetected" if is_redetection else "finding.detected",
                    resource_type="finding",
                    resource_id=str(finding.id),
                    outcome=AuditOutcome.SUCCESS,
                    correlation_id=context.correlation_id,
                )
            )

            event_cls = FindingUpdated if is_redetection else FindingCreated
            await self._event_publisher.publish(
                event_cls(
                    scan_id=context.scan_id,
                    tenant_id=context.tenant_id,
                    correlation_id=context.correlation_id,
                    finding_id=finding.id,
                    fingerprint=fingerprint,
                )
            )

        return findings
