"""`app.core.error_sanitization` — the central boundary that keeps a raw
exception's message out of every scan-facing surface: DB fields, API
responses, `ScanEvent` rows, and (via `app.core.log_redaction`, tested
here as a backstop) structured logs. A future scanner's exception could
carry credentials, tokens, or an authenticated URL embedded in whatever a
subprocess/HTTP client happened to print — this file proves none of that
ever survives past `ScanOrchestrator`.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core.config import get_settings
from app.core.error_sanitization import ErrorCategory, sanitize_exception
from app.core.log_redaction import redact_log_secrets
from app.models.enums import AssetType, ScanStatus
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_event_repository import ScanEventRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.bootstrap import build_orchestrator
from app.scan_engine.interfaces import ScannerCapabilities
from app.scan_engine.registry import ScannerRegistry
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.common import PaginationParams
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate, ScanRead
from app.schemas.tenant import TenantCreate
from tests.scan_engine_fakes import FakeJobQueue, FakePlugin

_SECRET = "sk_live_hunter2-supersecret-do-not-leak"
_SECRET_BEARING_MESSAGE = (
    f"connection to https://user:{_SECRET}@internal.example.com failed: "
    f"Authorization: Bearer {_SECRET}"
)


class _CustomScannerFailure(Exception):
    """Stands in for a real scanner adapter's own exception type — not in
    `_CATEGORY_BY_EXCEPTION_TYPE`, so it must still fall back to a safe,
    generic INTERNAL summary rather than ever touching its own message."""


# ---------------------------------------------------------------------------
# Unit: sanitize_exception() itself
# ---------------------------------------------------------------------------


def test_sanitize_exception_never_includes_the_original_message() -> None:
    exc = RuntimeError(_SECRET_BEARING_MESSAGE)
    result = sanitize_exception(exc)
    assert _SECRET not in result.summary
    assert _SECRET not in result.code
    # RuntimeError isn't in the type->category map, so this also proves
    # the unmapped fallback path is itself secret-free.
    assert result.category == ErrorCategory.INTERNAL


def test_sanitize_exception_unmapped_type_falls_back_to_internal_and_generic_summary() -> None:
    exc = _CustomScannerFailure(_SECRET_BEARING_MESSAGE)
    result = sanitize_exception(exc)
    assert result.category == ErrorCategory.INTERNAL
    assert result.code == "_CustomScannerFailure"
    assert _SECRET not in result.summary
    assert result.summary == "An internal error occurred while executing the scan."


def test_sanitize_exception_known_types_map_to_their_fixed_category() -> None:
    assert sanitize_exception(TimeoutError(_SECRET_BEARING_MESSAGE)).category == (
        ErrorCategory.TIMEOUT
    )
    assert sanitize_exception(ConnectionError(_SECRET_BEARING_MESSAGE)).category == (
        ErrorCategory.NETWORK
    )
    assert sanitize_exception(MemoryError(_SECRET_BEARING_MESSAGE)).category == (
        ErrorCategory.RESOURCE_LIMIT
    )


def test_sanitize_exception_code_is_the_class_name_only() -> None:
    exc = ConnectionRefusedError(_SECRET_BEARING_MESSAGE)
    result = sanitize_exception(exc)
    assert result.code == "ConnectionRefusedError"
    assert _SECRET not in result.code


def test_sanitize_exception_same_type_always_yields_the_identical_summary() -> None:
    """Two different secret-bearing messages of the same exception type
    must sanitize to byte-identical output — proof the summary is a
    fixed lookup, not derived from the message at all."""
    first = sanitize_exception(RuntimeError("secret-one: sk_aaa"))
    second = sanitize_exception(RuntimeError("totally different secret: sk_bbb"))
    assert first == second


# ---------------------------------------------------------------------------
# Integration: a secret-bearing scanner exception, driven through the real
# orchestrator, checked absent from every downstream surface.
# ---------------------------------------------------------------------------


def _registry(plugin_factory) -> ScannerRegistry:
    registry = ScannerRegistry()
    registry.register(
        ScannerCapabilities(
            scanner_type="fake",
            display_name="Fake Scanner",
            supported_asset_types=frozenset({AssetType.WEBSITE}),
            default_timeout_seconds=60,
        ),
        plugin_factory,
    )
    return registry


async def test_secret_bearing_scan_failure_is_sanitized_everywhere(
    db_session: AsyncSession, tmp_path
) -> None:
    tenant = await TenantRepository(db_session).create(TenantCreate(name="secleak", slug="secleak"))
    project = await ProjectRepository(db_session).create(
        ProjectCreate(tenant_id=tenant.id, name="secleak", slug="secleak")
    )
    asset = await AssetRepository(db_session).create(
        AssetCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_type=AssetType.WEBSITE,
            name="secleak",
            locator="https://example.com",
        )
    )
    scan_repo = ScanRepository(db_session)
    scan = await scan_repo.create(
        ScanCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scanner_type="fake",
        )
    )
    scan = await ScanStateMachine(db_session).transition(scan, ScanStatus.QUEUED)

    plugin = FakePlugin(
        raise_in="execute", error_to_raise=_CustomScannerFailure(_SECRET_BEARING_MESSAGE)
    )
    job_queue = FakeJobQueue()
    settings = get_settings().model_copy(update={"artifact_storage_local_path": str(tmp_path)})
    orchestrator = build_orchestrator(
        db_session, registry=_registry(lambda: plugin), job_queue=job_queue, settings=settings
    )

    with capture_logs(processors=[redact_log_secrets]) as captured_logs:
        # Never raises: ScanOrchestrator.run() contains every scan-
        # execution failure mode internally (see its module docstring)
        # — nothing ever reaches Celery's own exception/result metadata
        # for a scan-level failure, which is itself part of the
        # containment: there is no Celery task metadata surface for this
        # secret to leak into in the first place.
        await orchestrator.run(tenant.id, scan.id, worker_id="worker-1")

    # (a) DB field
    updated = await scan_repo.get_by_id(tenant.id, scan.id)
    assert updated is not None
    assert updated.status == ScanStatus.FAILED
    assert updated.error_code == "_CustomScannerFailure"
    assert updated.error_summary is not None
    assert _SECRET not in updated.error_summary
    assert _SECRET_BEARING_MESSAGE not in updated.error_summary

    # (b) API response shape (ScanRead is exactly what the endpoint
    # returns — see app.services.scan_service.get_scan)
    api_dump = ScanRead.model_validate(updated).model_dump_json()
    assert _SECRET not in api_dump
    assert _SECRET_BEARING_MESSAGE not in api_dump

    # (c) ScanEvent rows (scan.failed's message is ScanFailed.error_summary
    # — see app.scan_engine.bootstrap._to_scan_event_create)
    events_page = await ScanEventRepository(db_session).list_by_scan(
        tenant.id, scan.id, PaginationParams(limit=50, offset=0)
    )
    assert any(e.event_type == "scan.failed" for e in events_page.items)
    for event in events_page.items:
        haystack = " ".join(str(v) for v in (event.message, event.event_metadata) if v is not None)
        assert _SECRET not in haystack
        assert _SECRET_BEARING_MESSAGE not in haystack

    # (d) Logs — every structured log entry emitted during the run,
    # through the real redaction processor.
    for entry in captured_logs:
        assert _SECRET not in str(entry)
        assert _SECRET_BEARING_MESSAGE not in str(entry)
