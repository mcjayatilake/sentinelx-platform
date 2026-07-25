"""`ScanOrchestrator.run()` — state-machine-driving control flow: happy
path, transient-failure retry, permanent-failure dead-lettering, exhausted
retries, cooperative cancellation, and timeout handling."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.enums import AssetType, FindingConfidence, FindingSeverity, ScanStatus
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.bootstrap import build_orchestrator
from app.scan_engine.exceptions import PermanentScanError, ScanConfigError, TransientScanError
from app.scan_engine.interfaces import ScannerCapabilities
from app.scan_engine.pipeline.stages import NormalizedFinding
from app.scan_engine.registry import ScannerRegistry
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.tenant import TenantCreate
from tests.scan_engine_fakes import FakeJobQueue, FakePlugin


async def _make_scan(
    session: AsyncSession, slug: str, *, max_attempts: int = 3, timeout_seconds: int = 60
):
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
    scan_repo = ScanRepository(session)
    scan = await scan_repo.create(
        ScanCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scanner_type="fake",
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
        )
    )
    # `ScanOrchestrator.run()` is only ever invoked (by
    # `app.workers.tasks.scan_tasks.execute_scan`) on a scan
    # `ScanService._enqueue()` has already transitioned to QUEUED — PENDING
    # has no direct edge to PREPARING. Mirror that here so `run()` sees
    # the same precondition it does in production.
    scan = await ScanStateMachine(session).transition(scan, ScanStatus.QUEUED)
    return tenant, scan


def _registry(plugin_factory) -> ScannerRegistry:
    registry = ScannerRegistry()
    capabilities = ScannerCapabilities(
        scanner_type="fake",
        display_name="Fake Scanner",
        supported_asset_types=frozenset({AssetType.WEBSITE}),
        default_timeout_seconds=60,
    )
    registry.register(capabilities, plugin_factory)
    return registry


def _settings_with_artifact_dir(tmp_path) -> Settings:
    return get_settings().model_copy(update={"artifact_storage_local_path": str(tmp_path)})


async def test_happy_path_reaches_succeeded_and_persists_findings(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant, scan = await _make_scan(db_session, "orc1")
    finding = NormalizedFinding(
        title="Reflected XSS",
        description="detail",
        severity=FindingSeverity.HIGH,
        confidence=FindingConfidence.HIGH,
        source_tool="fake",
        external_reference=None,
        remediation=None,
        rule_id="rule-1",
        locator="https://example.com/login",
    )
    plugin = FakePlugin(findings=[finding])
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.SUCCEEDED
    assert updated.completed_at is not None
    assert plugin.calls == [
        "validate_config",
        "prepare",
        "execute",
        "collect_results",
        "normalize_findings",
        "cleanup",
    ]
    assert job_queue.enqueued == []
    assert job_queue.dead_lettered == []


async def test_transient_failure_re_queues_in_place_for_retry(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant, scan = await _make_scan(db_session, "orc2", max_attempts=3)
    plugin = FakePlugin(raise_in="execute", error_to_raise=TransientScanError("network blip"))
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.QUEUED
    assert updated.attempt == 1
    assert updated.failed_at is None
    # Retry is recorded as an outbox row now, not a direct JobQueue call
    # — a separate dispatcher publishes it once this commit has landed
    # (see app.workers.tasks.outbox_dispatcher, not exercised here).
    assert job_queue.enqueued == []
    outbox_rows = await ScanOutboxRepository(db_session).list_by_scan(tenant.id, scan.id)
    assert len(outbox_rows) == 1
    assert outbox_rows[0].scan_attempt == 1
    assert outbox_rows[0].published_at is None
    assert outbox_rows[0].countdown_seconds >= 0
    assert outbox_rows[0].payload["scan_id"] == str(scan.id)
    assert job_queue.dead_lettered == []
    assert "cleanup" in plugin.calls


async def test_permanent_failure_finalizes_failed_and_dead_letters(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant, scan = await _make_scan(db_session, "orc3")
    plugin = FakePlugin(raise_in="execute", error_to_raise=PermanentScanError("bad target"))
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.FAILED
    assert updated.error_code == "PermanentScanError"
    assert updated.failed_at is not None
    assert job_queue.enqueued == []
    assert len(job_queue.dead_lettered) == 1


async def test_transient_failure_with_no_attempts_remaining_fails_permanently(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant, scan = await _make_scan(db_session, "orc4", max_attempts=1)
    plugin = FakePlugin(raise_in="execute", error_to_raise=TransientScanError("network blip"))
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.FAILED
    assert job_queue.enqueued == []
    assert len(job_queue.dead_lettered) == 1


async def test_invalid_config_fails_without_running_execute(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant, scan = await _make_scan(db_session, "orc5")
    plugin = FakePlugin(raise_in="validate_config", error_to_raise=ScanConfigError("bad config"))
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.FAILED
    assert updated.error_code == "invalid_config"
    assert "execute" not in plugin.calls


async def test_unregistered_scanner_type_fails_immediately(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant = await TenantRepository(db_session).create(TenantCreate(name="orc6", slug="orc6"))
    project = await ProjectRepository(db_session).create(
        ProjectCreate(tenant_id=tenant.id, name="orc6", slug="orc6")
    )
    asset = await AssetRepository(db_session).create(
        AssetCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_type=AssetType.WEBSITE,
            name="orc6",
            locator="https://example.com",
        )
    )
    scan_repo = ScanRepository(db_session)
    scan = await scan_repo.create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="nuclei"
        )
    )
    scan = await ScanStateMachine(db_session).transition(scan, ScanStatus.QUEUED)
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=ScannerRegistry(),  # nothing registered
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.FAILED
    assert updated.error_code == "scanner_not_registered"


async def test_already_cancelled_scan_is_a_no_op(db_session: AsyncSession, tmp_path) -> None:
    tenant, scan = await _make_scan(db_session, "orc7")
    scan = await ScanStateMachine(db_session).transition(scan, ScanStatus.CANCELLED)

    plugin = FakePlugin()
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    # Raced with a concurrent cancel before the task even started: no
    # plugin method should ever have been called.
    assert plugin.calls == []


async def test_cooperative_cancellation_mid_run_finalizes_cancelled(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant, scan = await _make_scan(db_session, "orc8")
    scan_repo = ScanRepository(db_session)
    machine = ScanStateMachine(db_session)

    async def _cancel_concurrently() -> None:
        current = await scan_repo.get_by_id(tenant.id, scan.id)
        assert current is not None
        await machine.transition(current, ScanStatus.CANCELLED)

    # Simulates a concurrent POST /scans/{id}/cancel landing while
    # prepare() is running: the orchestrator's next cooperative check
    # (right after prepare() returns) must notice and stop before
    # execute() ever runs.
    plugin = FakePlugin(hooks={"prepare": _cancel_concurrently})
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await scan_repo.get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.CANCELLED
    assert "execute" not in plugin.calls
    assert "cleanup" in plugin.calls


async def test_timeout_finalizes_timed_out(db_session: AsyncSession, tmp_path) -> None:
    tenant, scan = await _make_scan(db_session, "orc9", timeout_seconds=1)
    plugin = FakePlugin(sleep_in="execute", sleep_seconds=2)
    job_queue = FakeJobQueue()
    orchestrator = build_orchestrator(
        db_session,
        registry=_registry(lambda: plugin),
        job_queue=job_queue,
        settings=_settings_with_artifact_dir(tmp_path),
    )

    await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    updated = await ScanRepository(db_session).get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.TIMED_OUT
    assert updated.timed_out_at is not None
    assert "cleanup" in plugin.calls
