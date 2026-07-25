# ADR 0005: Scan Plugin Architecture

## Status

Accepted — scan-engine-foundation phase.

## Context

SentinelX needs to support many scanner tools over time (OWASP ZAP,
Nuclei, Trivy, Semgrep, Gitleaks, Nmap, Nikto, and eventually custom
scanners), each with wildly different execution models (long-running
daemon + API, one-shot CLI subprocess, container-based sandbox) and
output formats. This phase must build the framework every future scanner
plugs into, without implementing any scanner itself — the framework's
design is what's being reviewed here, not a specific integration.

## Decision

**Every scanner is a `Protocol`, not an `ABC` subclass** —
`app.scan_engine.interfaces.ScannerPlugin` — with six methods:
`validate_config`, `prepare`, `execute`, `collect_results`,
`normalize_findings`, `cleanup`. See
[`docs/scanner-interface.md`](../scanner-interface.md) for the full
contract.

**Composition over inheritance.** A plugin does not inherit shared
behavior from a common base class; it composes whatever it needs
internally (an HTTP client, a subprocess wrapper, a `Parser`/`Normalizer`
pair from `app.scan_engine.pipeline.stages`). `ScannerRegistry.register()`
does a cheap `@runtime_checkable` structural `isinstance()` check at
registration time (method presence only) — nothing about a plugin's
*implementation* is constrained beyond satisfying that shape.

**`ScannerRegistry` holds a factory, not a shared instance.**
`registry.register(capabilities, factory)` where `factory` is usually the
plugin class itself; `registry.get(scanner_type)` calls `factory()` and
returns a fresh object every time. This means:

- No scanner can accidentally leak state between two concurrent scans
  sharing a worker process (each execution gets its own instance).
- A plugin's `__init__` can safely do per-instance setup without a
  cross-scan contamination risk.
- Tests can register a lambda factory that captures a scripted fake
  plugin, without touching the registry's shape.

**Dependency injection via `ScanContext`, never a global singleton
import.** Every plugin method receives a `ScanContext` — tenant/scan/
project/asset ids, `requested_by_user_id`, `correlation_id`, `config`,
`timeout_seconds`, `artifact_storage`, `metrics`, a bound `logger` — built
once per execution by `ScanOrchestrator`. A plugin that needs to write an
artifact, log something, or increment a metric calls
`context.artifact_storage`/`context.logger`/`context.metrics`, never
`from app.db.session import engine` or similar. This is what makes "every
scan must inherit tenant, user, permissions, audit context" true by
construction rather than by convention a plugin author has to remember.

**"No scanner-specific logic outside adapters."** The generic
orchestration machinery (state machine, event bus, retry/backoff, storage
abstraction, finding pipeline's dedup/persist/audit stages) has zero
knowledge of any specific scanner. The two stages that are inherently
scanner-specific — turning raw tool output into `RawFinding`s
(`collect_results`), and mapping those into SentinelX's common
`NormalizedFinding` vocabulary (`normalize_findings`) — are plugin
methods, not orchestrator code, precisely because they cannot be made
generic without losing scanner-specific meaning.

## Why this phase implements zero scanners

Building the framework and a real scanner in the same phase risks the
framework being accidentally shaped around that one scanner's quirks. The
explicit two-step "implement `ScannerPlugin`, then
`registry.register(...)`" integration path
(`docs/scan-engine.md#future-scanner-integration-guide`) is the
deliverable this phase is actually judged against: adding a new scanner
should require touching exactly those two things, nothing in
`app.scan_engine`'s orchestration core.

## Consequences

- A future scanner with an execution model this design didn't anticipate
  (e.g. one needing bidirectional streaming during `execute()`, not a
  single request/response) may need `ScannerPlugin`'s shape extended —
  acceptable; the Protocol is expected to grow method signatures (not
  reduce them) as real scanners are built against it.
- `ScannerRegistry.get()`'s `isinstance()` check only verifies method
  *presence*, not signature correctness — a plugin with the right method
  names but wrong parameter types passes registration and fails at
  `mypy --strict` time instead, or (if mypy wasn't run) at first call.
  This is an accepted tradeoff of `Protocol`'s structural typing; a
  runtime signature check would be far more invasive to implement.
- Every plugin method for one scan execution must funnel through one
  `ScanContext` instance — a plugin cannot access data belonging to a
  different tenant/scan without breaking the DI contract deliberately
  (e.g. importing a global singleton anyway), which is exactly the
  failure mode this ADR closes off, not one it makes convenient.
