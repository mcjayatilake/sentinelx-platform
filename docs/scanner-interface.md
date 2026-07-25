# Scanner Plugin Interface

Every scanner (OWASP ZAP, Nuclei, Trivy, Semgrep, Gitleaks, Nmap, Nikto,
or a custom scanner) implements `app.scan_engine.interfaces.ScannerPlugin`
— a `Protocol`, not an `ABC`. See
[ADR 0005](decisions/0005-scan-plugin-architecture.md) for why composition
over inheritance was chosen, and [`docs/scan-engine.md`](scan-engine.md#future-scanner-integration-guide)
for the two-step process to register a new one.

## Why `Protocol`, not `ABC`

A `Protocol` describes shape, not a shared base class to inherit
behavior from. `ScannerRegistry.get()` does a cheap, `@runtime_checkable`
structural `isinstance()` check at registration time (method presence
only — signature correctness is `mypy --strict`'s job at each call site,
not a runtime concern). Nothing about a scanner's implementation is
forced to extend a common base; a plugin composes whatever helpers it
needs (a `Parser`, a `Normalizer`, an HTTP client, a subprocess wrapper)
rather than inheriting them.

## `ScannerCapabilities`

Static metadata, independent of any single scan — used for
discovery/listing and by the registry:

```python
@dataclass(frozen=True, slots=True)
class ScannerCapabilities:
    scanner_type: str
    display_name: str
    supported_asset_types: frozenset[AssetType]
    default_timeout_seconds: int
    supports_safe_mode: bool = True
```

## The six methods

Every method receives a `ScanContext` — the dependency-injection bundle
built once per execution by `ScanOrchestrator` (tenant/scan/project/asset
ids, `requested_by_user_id`, `correlation_id`, `config`,
`timeout_seconds`, `artifact_storage`, `metrics`, a bound `logger`). A
plugin never imports a global settings/session/storage singleton —
everything it needs to act on behalf of a specific tenant is passed in.

1. **`validate_config(context) -> None`** — validate configuration.
   Raise `app.scan_engine.exceptions.ScanConfigError` if
   `context.config` is invalid for this scanner. Called before any other
   method; must not have side effects. A failure here fails the scan
   immediately (`error_code="invalid_config"`) without ever calling
   `prepare()`/`execute()`.

2. **`prepare(context) -> None`** — prepare scan. Set up whatever the
   scan needs before `execute()` (e.g. writing a scanner-specific config
   file via `context.artifact_storage`). Must be safe to call again on
   an in-place retry (the same `Scan` row, a fresh plugin instance).

3. **`execute(context) -> RawScanOutput`** — execute scan. Run the actual
   scan. May raise `TransientScanError` (retryable, subject to
   `Scan.max_attempts` and backoff — see
   [`docs/orchestrator.md`](orchestrator.md#retry--backoff)) or
   `PermanentScanError` (not retryable) for scanner-specific failures.
   Returns a reference to whatever was produced
   (`RawScanOutput.artifacts`, saved via `context.artifact_storage`), not
   the scan output inline — `RawScanOutput.findings` is an optional
   escape hatch for a plugin that already has `RawFinding`s in memory and
   wants `collect_results()` to be a trivial passthrough (see
   `app.scan_engine.pipeline.identity.PassthroughParser`).

4. **`collect_results(context, raw) -> list[RawFinding]`** — collect
   results / parse. Parse the raw output referenced by `raw` into
   `RawFinding`s, still in the scanner's own vocabulary (raw severity/
   confidence strings, tool-specific field names). This *is* the
   "parser" stage of `scanner output -> parser -> normaliser -> ...` —
   scanner-specific by nature, so it lives here, not in the shared
   pipeline (see [`docs/scan-engine.md`](scan-engine.md#result-normalisation-and-the-finding-pipeline)).

5. **`normalize_findings(context, raw) -> list[NormalizedFinding]`** —
   normalise findings. Synchronous and side-effect-free: translate
   `RawFinding`s into SentinelX's common `NormalizedFinding` vocabulary
   (`title`, `description`, `severity: FindingSeverity`,
   `confidence: FindingConfidence`, `source_tool`, `rule_id`, `locator`,
   ...). This is the "normaliser" stage — also scanner-specific ("no
   scanner-specific logic outside adapters"). A plugin with a
   straightforward raw→common mapping can compose
   `app.scan_engine.pipeline.identity.IdentityNormalizer` rather than
   writing this from scratch.

6. **`cleanup(context) -> None`** — release any resources acquired in
   `prepare()`/`execute()` (temp files, subprocess handles, sandbox
   containers). **Always** called by the orchestrator — including on
   failure, timeout, and cancellation — so it must not raise for a scan
   that never got past `prepare()` (e.g. nothing to clean up yet).

## What happens after `normalize_findings()`

The orchestrator hands `list[NormalizedFinding]` to
`app.scan_engine.pipeline.pipeline.FindingPipeline.process()` —
fingerprinting, dedup, persistence, audit, and event publication, all
scanner-agnostic. A plugin never calls `FindingPipeline` itself and never
touches `FindingRepository` directly.

## Configuration

`context.config` is the scan's `config` JSONB column (size-capped at
`SCAN_CONFIG_MAX_BYTES`, validated for size at both the API boundary and
the DB level) — the vehicle for timeouts, resource limits, parallelism,
safe mode, and future proxy support. Shape validation beyond size is
entirely `validate_config()`'s responsibility; no scanner exists yet to
define a concrete config schema against.

## Example skeleton

```python
from app.scan_engine.context import ScanContext
from app.scan_engine.exceptions import ScanConfigError, TransientScanError
from app.scan_engine.interfaces import RawScanOutput, ScannerCapabilities
from app.scan_engine.pipeline.identity import IdentityNormalizer
from app.scan_engine.pipeline.stages import NormalizedFinding, RawFinding


class ZapPlugin:
    capabilities = ScannerCapabilities(
        scanner_type="zap",
        display_name="OWASP ZAP",
        supported_asset_types=frozenset({AssetType.WEBSITE}),
        default_timeout_seconds=1800,
    )

    def __init__(self) -> None:
        self._normalizer = IdentityNormalizer(source_tool="zap")

    async def validate_config(self, context: ScanContext) -> None:
        if "target_url" not in context.config:
            raise ScanConfigError("config.target_url is required")

    async def prepare(self, context: ScanContext) -> None:
        ...  # write a ZAP context file via context.artifact_storage

    async def execute(self, context: ScanContext) -> RawScanOutput:
        ...  # run zap-cli / API against context.config["target_url"]

    async def collect_results(
        self, context: ScanContext, raw: RawScanOutput
    ) -> list[RawFinding]:
        ...  # parse ZAP's own report format into RawFinding

    def normalize_findings(
        self, context: ScanContext, raw: list[RawFinding]
    ) -> list[NormalizedFinding]:
        return [self._normalizer.normalize(f) for f in raw]

    async def cleanup(self, context: ScanContext) -> None:
        ...  # remove the ZAP context file / stop the daemon
```

Registration (`app.scan_engine.bootstrap.build_default_registry`):

```python
registry.register(ZapPlugin.capabilities, ZapPlugin)
```
