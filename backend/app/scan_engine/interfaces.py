"""The scanner plugin contract.

`Protocol`, not `ABC` — composition over inheritance, per the scan-engine
spec. `ScannerRegistry` (`app.scan_engine.registry`) holds a *factory*
per scanner type, not a shared instance, so every scan execution gets a
fresh plugin object with no state shared across concurrent scans.

See docs/scanner-interface.md for the full method-by-method contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.models.enums import AssetType

if TYPE_CHECKING:
    from app.scan_engine.context import ScanContext
    from app.scan_engine.pipeline.stages import NormalizedFinding, RawFinding


@dataclass(frozen=True, slots=True)
class ScannerCapabilities:
    """Static metadata about a scanner plugin, independent of any single
    scan — used by the registry for discovery/listing and by the
    orchestrator to pick sensible defaults (e.g. `default_timeout_seconds`
    when a scan didn't specify one)."""

    scanner_type: str
    display_name: str
    supported_asset_types: frozenset[AssetType]
    default_timeout_seconds: int
    supports_safe_mode: bool = True


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    key: str
    size_bytes: int
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class RawScanOutput:
    """What `ScannerPlugin.execute()` returns and `collect_results()`
    consumes — a reference to whatever the scanner produced (files saved
    via `ScanContext.artifact_storage`), not the scan output itself.

    `findings`, when populated, lets a `Parser` skip re-reading artifacts
    it already has in memory — this is what
    `app.scan_engine.pipeline.identity.PassthroughParser` reads from;
    most real parsers will read `artifacts` instead and leave this unset.
    """

    artifacts: list[ArtifactRef]
    exit_code: int | None = None
    findings: list[RawFinding] | None = None


@runtime_checkable
class ScannerPlugin(Protocol):
    """Every scanner plugin implements this. `@runtime_checkable` lets
    `ScannerRegistry.register()` do a cheap structural `isinstance()`
    sanity check at registration time (method presence only — full
    signature correctness is mypy strict's job at each call site)."""

    capabilities: ScannerCapabilities

    async def validate_config(self, context: ScanContext) -> None:
        """Raise `app.scan_engine.exceptions.ScanConfigError` if
        `context.config` is invalid for this scanner. Called before any
        other method; must not have side effects."""
        ...

    async def prepare(self, context: ScanContext) -> None:
        """Set up whatever the scan needs before `execute()` (e.g.
        writing a scanner-specific config file via
        `context.artifact_storage`). Idempotent-safe to call again on
        retry."""
        ...

    async def execute(self, context: ScanContext) -> RawScanOutput:
        """Run the scan. May raise
        `app.scan_engine.exceptions.TransientScanError` (retryable) or
        `PermanentScanError` (not retryable) for scanner-specific
        failures."""
        ...

    async def collect_results(self, context: ScanContext, raw: RawScanOutput) -> list[RawFinding]:
        """Parse the raw output referenced by `raw` into `RawFinding`s —
        still in the scanner's own vocabulary."""
        ...

    def normalize_findings(
        self, context: ScanContext, raw: list[RawFinding]
    ) -> list[NormalizedFinding]:
        """Translate `RawFinding`s into SentinelX's common `Finding`
        vocabulary. Synchronous and side-effect-free — pure data mapping."""
        ...

    async def cleanup(self, context: ScanContext) -> None:
        """Release any resources acquired in `prepare()`/`execute()`.
        Always called by the orchestrator, including on failure/timeout/
        cancellation — must not raise for a scan that never got past
        `prepare()`."""
        ...
