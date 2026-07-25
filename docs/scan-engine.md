# Scan Engine

## Status

Phase 4A — **orchestration foundation only**. No individual scanner
(OWASP ZAP, Nuclei, Trivy, Semgrep, Gitleaks, Nmap, Nikto, ...) is
implemented yet. This phase builds the plugin contract, orchestrator,
event system, storage abstraction, finding pipeline, retry/backoff, and
the API surface every future scanner plugs into — see
[`docs/decisions/0005-scan-plugin-architecture.md`](decisions/0005-scan-plugin-architecture.md)
for why "no scanners yet" is the deliberate scope of this phase, not a
gap.

## Module map

```
backend/app/scan_engine/
├── interfaces.py       ScannerPlugin Protocol, ScannerCapabilities, RawScanOutput
├── registry.py          ScannerRegistry — factory-per-scanner-type
├── context.py            ScanContext — the DI bundle every plugin method receives
├── exceptions.py          ScanEngineError hierarchy (worker-side only),
│                            including StaleScanStateError (see ADR 0008)
├── state_machine.py        ScanStateMachine — sole ScanStatus mutation
│                              point; commits at every transition (ADR 0008)
├── events.py                 DomainEvent hierarchy + InMemoryEventBus
├── progress.py                 ScanProgressSnapshot, estimate_completion()
├── retry.py                     RetryPolicy, ErrorClass, classify_error()
├── job_queue.py                  JobQueue Protocol (no Celery import);
│                                   scan_job_payload_to_dict/from_dict —
│                                   the outbox payload wire shape
├── orchestrator.py                ScanOrchestrator — the control-flow engine
├── bootstrap.py                    Composition root (build_orchestrator, ...)
└── metrics.py                      Metrics Protocol + NoOpMetrics
    storage/
    ├── interfaces.py               ArtifactStorage Protocol
    └── local.py                     LocalFilesystemArtifactStorage
    pipeline/
    ├── stages.py                     RawFinding, NormalizedFinding, Parser/Normalizer
    ├── fingerprint.py                 compute_fingerprint()
    ├── identity.py                     PassthroughParser, IdentityNormalizer
    └── pipeline.py                     FindingPipeline (dedup -> repository -> audit)

backend/app/core/
├── error_sanitization.py    sanitize_exception() — the persisted/API-facing
│                               boundary (ADR 0008 §7)
└── log_redaction.py           redact_log_secrets() — the structured-log
                                  backstop (ADR 0008 §7)

backend/app/models/scan_job_outbox.py            ScanJobOutbox
backend/app/repositories/scan_outbox_repository.py  claim_next_pending() etc.
backend/app/schemas/scan_outbox.py

backend/app/workers/
├── celery_app.py            Celery app factory — beat_schedule includes
│                               outbox_dispatcher's periodic task
├── job_queue_celery.py       CeleryJobQueue — the concrete JobQueue adapter
└── tasks/
    ├── scan_tasks.py          execute_scan Celery task (per-invocation engine)
    └── outbox_dispatcher.py    dispatch_pending_outbox — the only code that
                                  turns a committed outbox row into a real
                                  Celery message (ADR 0008 §5)

backend/app/services/scan_service.py   Request-scoped scan lifecycle API
backend/app/api/v1/endpoints/scans.py  POST/GET/cancel/retry/progress endpoints
```

See [`docs/orchestrator.md`](orchestrator.md) for the state machine and
control flow, and [`docs/scanner-interface.md`](scanner-interface.md) for
the plugin contract new scanners implement.

## Architecture summary

Every scanner is a plugin implementing the `ScannerPlugin` Protocol
(`app.scan_engine.interfaces`) — composition over inheritance, per
[ADR 0005](decisions/0005-scan-plugin-architecture.md). The
`ScannerRegistry` holds a *factory* per `scanner_type`, not a shared
instance, so every scan execution gets a fresh, stateless plugin object;
no scanner can leak state across concurrent scans sharing a worker
process.

`ScanOrchestrator.run()` is the one place that: resolves a plugin from
the registry, drives `Scan.status` through `ScanStateMachine`, builds the
`ScanContext` dependency-injection bundle every plugin method receives,
enforces the configured per-scan timeout, checks for cooperative
cancellation between stages, and decides retry vs. permanent failure on
error (`app.scan_engine.retry`). It is invoked by exactly one caller:
`app.workers.tasks.scan_tasks.execute_scan`, a Celery task.

## Scan lifecycle

1. `POST /scans` (`app.api.v1.endpoints.scans.create_scan`) validates the
   target project/asset belong to the caller's tenant, creates a `Scan`
   row (`PENDING`), writes a `ScanJobOutbox` row, and transitions it to
   `QUEUED` (`ScanService._enqueue`) — the outbox row and the `QUEUED`
   transition commit together, atomically, in one transaction (see
   [ADR 0008](decisions/0008-transaction-and-concurrency-model.md#5-transactional-outbox-for-queue-dispatch)).
   Nothing is handed to `JobQueue` directly from this request.
2. A separate, periodic dispatcher
   (`app.workers.tasks.outbox_dispatcher`, Celery-Beat-scheduled) claims
   the outbox row once that commit has landed and calls
   `JobQueue.enqueue_scan()` (today: `CeleryJobQueue`, which calls
   `celery_app.send_task(...)`, routing to the `scans` queue).
3. A Celery worker picks up `execute_scan(scan_id, tenant_id,
   correlation_id)`. Each invocation builds its **own** async SQLAlchemy
   engine (see `scan_tasks.py`'s docstring on the event-loop-per-
   invocation pitfall) and calls `ScanOrchestrator.run()`, which first
   atomically claims execution ownership of this attempt
   (`claim_for_execution`) — a duplicate/late delivery of the same
   attempt is a clean no-op here.
4. `run()` drives the scan through `PREPARING -> RUNNING -> COLLECTING ->
   PROCESSING -> SUCCEEDED` (see [`docs/orchestrator.md`](orchestrator.md)
   for the full state diagram and every off-ramp: `FAILED`, `CANCELLED`,
   `TIMED_OUT`, and the in-place `QUEUED` retry edge), **committing at
   every stage transition** rather than once at the end (ADR 0008 §2) —
   this is what makes mid-run progress visible to a separate session and
   what keeps a cancellation request from ever blocking behind a run's
   own lock.
5. `execute_scan`'s own `session.commit()`/`rollback()` around the
   `run()` call is a defensive boundary for infrastructure failures that
   happen *outside* `run()`'s own internal handling (`run()` never lets
   a scan-execution failure escape); on an escaped exception, it rolls
   back and re-raises so Celery surfaces the task as failed.
6. Progress is durably logged to `ScanEvent` rows throughout (see
   "Event architecture" below) and readable via
   `GET /scans/{id}/progress` at any point, including mid-run.

## Event architecture

Three layers, each solving a different requirement from the spec:

1. **Typed domain events** (`app.scan_engine.events`) — `DomainEvent` plus
   nine required subclasses (`ScanQueued`, `ScanStarted`, `ScanProgress`,
   `ScanCompleted`, `ScanFailed`, `ScanCancelled`, `ScanTimedOut`,
   `FindingCreated`, `FindingUpdated`) and two operational ones
   (`WorkerStarted`, `WorkerStopped`, not yet published by anything this
   phase). Frozen dataclasses — value objects, not persisted directly.
2. **`InMemoryEventBus`** — process-local pub/sub. `publish()` fans an
   event out to every subscriber for its type; one handler raising never
   breaks `publish()` for the others or for the producer (see
   [ADR 0006](decisions/0006-scan-event-system.md)).
3. **Durable persistence** — `app.scan_engine.bootstrap._wire_subscribers`
   attaches a persistence subscriber for all nine event types, writing a
   `ScanEvent` row per event (`app.repositories.scan_event_repository`).
   This is the backing store `GET /scans/{id}/progress` reads from
   (`get_latest_progress`), and the durable event/audit trail for a scan's
   execution.

`event_type` on `ScanEvent` is free text, not an enum-backed column: the
nine formal events always populate it with one of a fixed, documented set
of strings (`scan.queued`, `scan.started`, `scan.progress`,
`scan.completed`, `scan.failed`, `scan.cancelled`, `scan.timed_out`,
`finding.created`, `finding.updated`), but the orchestrator also logs an
incidental operational sub-event this way (`scan.progress` with a
`message` describing a scheduled retry) rather than inventing a tenth
`DomainEvent` subclass or a bespoke `ScanEvent.status` CHECK constraint
for one message.

## Progress reporting

`GET /scans/{id}/progress` returns `ScanProgressRead`: `status`, `stage`,
`percent`, `started_at`, `estimated_completion_at`, `worker_id`,
`updated_at`. Built from the latest `scan.progress` `ScanEvent` row when
one exists, falling back to the `Scan` row's own status/timestamps
otherwise (`app.services.scan_service.ScanService.get_progress`).
`estimated_completion_at` (`app.scan_engine.progress.estimate_completion`)
is a best-effort linear extrapolation from elapsed time and percent
complete — an explicit UI hint, not a guarantee.

The shape is already websocket-ready: nothing about `ScanProgressRead` or
the durable `ScanEvent` log assumes a polling client — a future websocket
endpoint tails the same `ScanEvent` table (polling or `LISTEN/NOTIFY`),
publishing the same payload shape, without any change to how progress is
computed or persisted.

## Storage abstraction

`ArtifactStorage` (`app.scan_engine.storage.interfaces`) is the seam for
raw scanner output, logs, screenshots, and future evidence files.
`LocalFilesystemArtifactStorage` is the only implementation this phase
ships (`settings.artifact_storage_backend: Literal["local"]`), laid out
as `{base_dir}/{tenant_id}/{scan_id}/{name}` — every method takes
`tenant_id` even though `scan_id` alone would resolve uniquely,
deliberately mirroring the tenant-owned-repository convention so a future
S3-compatible backend (`boto3` is already a dependency; see
`settings.s3_*`) maps directly onto a `{tenant_id}/{scan_id}/{key}`
object-key layout with no interface change. `name` is treated as
untrusted input and checked against path-traversal (`..` segments,
absolute-path overrides, symlink escapes) — see
`LocalFilesystemArtifactStorage._resolve_path` and
`tests/test_scan_artifact_storage.py`.

## Result normalisation and the finding pipeline

`scanner output -> parser -> normaliser -> deduplicator -> repository ->
audit -> notifications (future)`:

- **Parser / normaliser** are `ScannerPlugin.collect_results()` +
  `normalize_findings()` — inherently scanner-specific ("no
  scanner-specific logic outside adapters"), so they are plugin
  responsibilities, not the pipeline's. `app.scan_engine.pipeline.stages`
  defines the `Parser`/`Normalizer` Protocols a plugin's own
  `collect_results`/`normalize_findings` may compose internally;
  `app.scan_engine.pipeline.identity` ships `PassthroughParser` +
  `IdentityNormalizer` as real (not test-only) reference implementations
  — the minimal, honest starting point every scanner-specific
  parser/normalizer adapts from.
- **Deduplicator / repository / audit / notifications** are
  `FindingPipeline.process()` (`app.scan_engine.pipeline.pipeline`):
  computes a fingerprint per finding
  (`app.scan_engine.pipeline.fingerprint.compute_fingerprint` — a SHA-256
  digest of `(scanner_type, rule_id, locator, detail)`), then calls the
  **existing** `FindingRepository.record_detection()` (built in the
  database-foundation phase) for dedup persistence — new-fingerprint
  inserts a `Finding`, seen-fingerprint updates its `last_seen_at`. This
  pipeline's only new responsibility is the fingerprint and deciding
  `finding.detected` vs. `finding.redetected` for the audit event and
  `FindingCreated` vs. `FindingUpdated` for the domain event.
  "Notifications (future)" has no stub class: `FindingCreated`/
  `FindingUpdated` on the event bus already *are* the extension point a
  future notification subscriber attaches to.

## Configuration

`Scan.config` (JSONB, size-capped — see `SCAN_CONFIG_MAX_BYTES`) carries
scanner-specific configuration: timeouts, resource limits, parallelism,
safe mode, future proxy support — validated for size at the API boundary
(`ScanCreateRequest`) and the DB level (a CHECK constraint), and for shape
by each scanner plugin's own `validate_config()` once one exists. Process-
wide scan-engine defaults live in `Settings`
(`scan_default_timeout_seconds`, `scan_default_max_attempts`,
`scan_retry_backoff_base_seconds`, `scan_retry_backoff_max_seconds`,
`scan_queue_name`, `scan_dead_letter_queue_name`,
`scan_outbox_dispatch_interval_seconds`,
`scan_outbox_dispatch_batch_size`, `artifact_storage_backend`,
`artifact_storage_local_path`) — see `.env.example`.

## Security

Every `ScanContext` (built once per execution by the orchestrator) carries
`tenant_id`, `requested_by_user_id`, and `correlation_id` — a plugin never
imports a global session/settings singleton to discover who it's acting
for. `ScanRepository`'s tenant-scoped methods (composite FK to `tenants`)
mean a scan can never be created, read, or transitioned outside its
tenant. `POST /scans`, `GET /scans`, `GET /scans/{id}`,
`POST /scans/{id}/cancel`, `POST /scans/{id}/retry`, and
`GET /scans/{id}/progress` are all `require_permission`-gated
(`Permission.SCANS_VIEW`/`SCANS_MANAGE` — see `docs/rbac.md`), never a
hand-rolled role check. That gate now also accepts an API-key principal
(scopes checked, never a role fallback — see `docs/api-keys.md` and
[ADR 0008](decisions/0008-transaction-and-concurrency-model.md#8-inactive-tenant-enforcement-and-api-key-authentication)),
and rejects a suspended/archived tenant on every request regardless of
which authentication path was used.

A caught scanner exception's raw message never reaches `Scan.error_summary`,
the `ScanFailed` event, or the API response — `app.core.error_sanitization`
maps it to a fixed, generic category summary instead (ADR 0008 §7). The
same discipline applies to the outbox's `last_error` field on a failed
publish attempt.

## Observability

Structured logging via `app.core.logging.get_logger`, bound with
`scan_id`/`tenant_id`/`correlation_id` at context-construction time so
every log line from a plugin or the orchestrator during one execution
shares the same correlation id. `Metrics` (`app.scan_engine.metrics`) is a
`Protocol` with a `NoOpMetrics` default — the orchestrator calls
`increment()`/`observe_duration()` at every stage transition and outcome,
ready for a real Prometheus/OpenTelemetry backend to be a drop-in
replacement (`build_orchestrator(..., metrics=RealMetrics())`) with zero
call-site changes. `correlation_id` is generated at scan creation if the
caller didn't supply one, threaded through every event, log line, and
`ScanEvent` row for that execution.

## Future scanner integration guide

To add a new scanner (e.g. OWASP ZAP):

1. Implement `ScannerPlugin` (`app.scan_engine.interfaces`) — see
   [`docs/scanner-interface.md`](scanner-interface.md) for the full
   method-by-method contract.
2. Register it in `app.scan_engine.bootstrap.build_default_registry()`:
   `registry.register(capabilities, ZapPlugin)` — one line, a
   `ScannerCapabilities` instance, and a factory (usually the class
   itself, since `ScannerRegistry.get()` calls `factory()` to get a fresh
   instance per execution).
3. Nothing else changes: the orchestrator, state machine, event system,
   retry/backoff, storage abstraction, finding pipeline, and API
   endpoints are all scanner-agnostic already.

## Remaining work

- No individual scanner is implemented (explicit non-goal this phase).
- No stuck-scan reaper (a Celery Beat job for rows past
  `started_at + timeout_seconds` with a stale `worker_id` and a crashed
  worker) — see [`docs/orchestrator.md`](orchestrator.md)'s "Known
  limitations" section. `outbox_dispatcher` now occupies one entry of
  the same `beat_schedule` seam a future reaper would use.
- The outbox dispatcher's at-least-once delivery gap (a crash between
  publish and recording `published_at`) is accepted and documented, not
  eliminated — see [ADR 0008](decisions/0008-transaction-and-concurrency-model.md#5-transactional-outbox-for-queue-dispatch).
- No operational metrics/alerts on outbox backlog depth or dispatcher
  publish-failure rate yet — the schema (`published_at`, `attempt_count`,
  `last_error`) already carries what a future alert would need.
- `WorkerStarted`/`WorkerStopped` events are defined but nothing
  publishes them yet (no worker-lifecycle hook exists to call from).
- No S3-compatible `ArtifactStorage` implementation yet (interface is
  ready; `boto3` is already a dependency).
- No websocket progress endpoint yet (the `ScanEvent` durable log is
  ready for one to tail).
