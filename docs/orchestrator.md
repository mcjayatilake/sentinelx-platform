# Scan Orchestrator

`app.scan_engine.orchestrator.ScanOrchestrator` is the state-machine-
driving engine — the one place that resolves a scanner plugin, drives
`Scan.status`, builds the per-execution `ScanContext`, enforces the
configured timeout, checks for cooperative cancellation, and decides
retry vs. permanent failure. Its single caller is
`app.workers.tasks.scan_tasks.execute_scan`, a Celery task. See
[`docs/scan-engine.md`](scan-engine.md) for the surrounding architecture
and [`docs/scanner-interface.md`](scanner-interface.md) for what a plugin
implements.

## State machine

`app.scan_engine.state_machine.ScanStateMachine` is the **sole**
sanctioned way to mutate `Scan.status` anywhere in this codebase — see
[ADR 0007](decisions/0007-scan-state-machine.md). Ten states:

```
PENDING → QUEUED → PREPARING → RUNNING → COLLECTING → PROCESSING → SUCCEEDED
             ↑          |          |           |
             └──────────┴──────────┴───────────┘   (in-place retry: transient
                                                      failure re-queues the
                                                      SAME Scan row)

Any of {QUEUED, PREPARING, RUNNING, COLLECTING} → CANCELLED
Any of {PREPARING, RUNNING, COLLECTING, PROCESSING} → FAILED
Any of {PREPARING, RUNNING, COLLECTING} → TIMED_OUT
PENDING → CANCELLED   (never queued)
QUEUED → FAILED        (e.g. rejected before a worker ever picks it up)
PROCESSING → CANCELLED (cooperative cancellation checked one last time
                         before finalizing findings)

SUCCEEDED, FAILED, CANCELLED, TIMED_OUT — terminal, no outbound edges.
```

`PROCESSING` deliberately has **no** edge back to `QUEUED`: a failure
while persisting findings goes straight to `FAILED`, never retried
in-place — see ADR 0007 for why.

`ScanStateMachine.transition()` validates the edge, applies `fields`, and
automatically stamps the matching terminal timestamp
(`completed_at`/`failed_at`/`cancelled_at`/`timed_out_at`) and
`queued_at`/`started_at` on their first-ever transition — callers never
set these by hand.

**Every `transition()` call commits immediately** — see
[ADR 0008](decisions/0008-transaction-and-concurrency-model.md) — via a
SQL-level conditional `UPDATE ... WHERE status = :expected`, not a plain
`UPDATE`. A concurrent writer (a cancel request, a duplicate Celery
delivery) that already moved the row raises `StaleScanStateError`, which
`run()` catches once, around the whole execution — see "Concurrency"
below.

## Scan lifecycle (happy path)

1. `run(tenant_id, scan_id, worker_id)` fetches the scan and returns
   immediately (a clean, expected no-op — see "Concurrency" below) unless
   it is exactly `QUEUED`. Resolves its plugin from the
   `ScannerRegistry`; unregistered `scanner_type` → immediate `FAILED`
   (`error_code="scanner_not_registered"`), no plugin method ever
   called.
2. Atomically claims the attempt (`ScanRepository.claim_for_execution`:
   `QUEUED → PREPARING` plus `attempt += 1`, guarded by the attempt
   number — see "Concurrency" below) and commits. A duplicate/late
   delivery of the same logical attempt loses this race and returns
   cleanly. Builds `ScanContext`, calls `plugin.validate_config()`. A
   `ScanConfigError` here → `FAILED` (`error_code="invalid_config"`),
   plugin's `cleanup()` still called.
3. Runs the rest inside `asyncio.timeout(scan.timeout_seconds)`:
   `plugin.prepare()` → `RUNNING` → `plugin.execute()` → `COLLECTING` →
   `plugin.collect_results()` → `PROCESSING` →
   `plugin.normalize_findings()` + `FindingPipeline.process()` →
   `SUCCEEDED`.
4. `plugin.cleanup()` always runs in a `finally`, including on
   failure/timeout/cancellation — a `cleanup()` failure is logged and
   counted, never allowed to mask the scan's real outcome.

## Job execution

Background execution is a Celery task (`app.workers.tasks.scan_tasks.
execute_scan`), but the orchestrator itself has zero Celery import —
see [ADR 0005](decisions/0005-scan-plugin-architecture.md) and
[`docs/scan-engine.md`](scan-engine.md#module-map). Each task invocation:

- builds its **own** async SQLAlchemy engine (not the process-wide
  `app.db.session` singleton) — `anyio.run()` gives each invocation a
  fresh event loop, and asyncpg connections are bound to the loop that
  created them, so a shared engine breaks on any invocation after the
  first;
- uses **one session for the whole run, but many commits** — see
  [ADR 0008](decisions/0008-transaction-and-concurrency-model.md).
  `ScanStateMachine.transition()` commits at every stage boundary and
  the event-bus's persistence subscriber commits after every
  `ScanEvent` insert, so no DB lock is ever held across a plugin method
  call and mid-run progress is visible to a separate session before the
  run finishes. `scan_tasks.py`'s own `session.commit()`/`rollback()`
  around the `run()` call is a defensive boundary for infrastructure
  failures that happen *outside* `run()`'s own internal try/except
  (`run()` never lets a scan-execution failure propagate — see its
  module docstring); an exception that does escape rolls back and
  re-raises so Celery surfaces the task as failed;
- disposes the engine in a `finally` block regardless of outcome.

Concurrency (how many scans run at once) and worker count are Celery
worker-process configuration (`celery -A app.workers.celery_app worker
--concurrency=N`), not something `ScanOrchestrator` manages — the
orchestrator only knows how to run *one* scan to completion.

## Retry / backoff

Two independent mechanisms, for two different failure surfaces:

- **In-place retry** (`app.scan_engine.retry.RetryPolicy`): on a
  `TransientScanError` during `PREPARING`/`RUNNING`/`COLLECTING`, if
  `scan.attempt < scan.max_attempts` (the scan's **own** configured
  limit — see `ScanCreateRequest.max_attempts` — never a process-wide
  default), the orchestrator writes a `ScanJobOutbox` row (backoff delay
  from `policy.next_delay_seconds(attempt)` carried as
  `countdown_seconds`) and transitions the *same* `Scan` row back to
  `QUEUED` — both committed together, atomically, by sequencing (see
  [ADR 0008](decisions/0008-transaction-and-concurrency-model.md#5-transactional-outbox-for-queue-dispatch)).
  A separate, periodic dispatcher publishes the outbox row to
  `JobQueue.enqueue_scan()` once that commit has actually landed; the
  orchestrator itself never calls `JobQueue.enqueue_scan()` for a retry.
  Attempts exhausted, or a `PermanentScanError`/any other exception →
  `FAILED` and `JobQueue.dead_letter_scan()` (best-effort; a dead-letter
  routing failure is logged, never allowed to change the scan's
  already-finalized `FAILED` status).
- **User-facing retry** (`POST /scans/{id}/retry`,
  `ScanService.retry_scan`): only valid from a terminal `FAILED`/
  `CANCELLED`/`TIMED_OUT` scan — creates a **new** `Scan` row
  (`retry_of_scan_id` lineage to the original), leaving the original's
  history untouched, and enqueues it exactly like a fresh
  `POST /scans`. A retry of a `SUCCEEDED` scan is out of scope for this
  endpoint (re-running a scan that succeeded is "run again," a
  `POST /scans` call, not a retry).

`RetryPolicy` is queue-agnostic by design: it decides *whether* and
*when* to retry; `JobQueue` (today: `CeleryJobQueue`) only ever does what
it's told (`enqueue_scan`/`cancel`/`dead_letter_scan`), never makes its
own retry decisions — no Celery `self.retry()`/`autoretry_for`. A future
`RQJobQueue` or similar implements the same `JobQueue` Protocol with zero
orchestrator changes.

## Cancellation

**Cooperative, not preemptive.** `POST /scans/{id}/cancel`
(`ScanService.cancel_scan`) transitions `Scan.status` to `CANCELLED`
through the same `ScanStateMachine.transition()`/conditional-`UPDATE`
path as every other transition (valid from any non-terminal state),
committing immediately, and calls `JobQueue.cancel(job_id)` best-effort
— a no-op if the scan hasn't been dispatched to Celery yet (`job_id` is
only ever set by the outbox dispatcher, after a real publish; see
[ADR 0008](decisions/0008-transaction-and-concurrency-model.md)). This
`UPDATE` is never blocked behind a run's own long-held lock, because
that lock no longer exists — the fix this whole repair centers on (see
ADR 0008 §2). If the transition raises `StaleScanStateError` (the scan
already finished or was already cancelled by the time this request's
`UPDATE` ran), `cancel_scan` turns that into a 409
`ScanNotCancellableError` — the same observable outcome as an
in-memory-detected invalid edge, just caught one layer later, at the SQL
level, where it's the actual source of truth for a race like this.

The orchestrator notices a cancellation via `_assert_not_cancelled()`,
which re-fetches the scan fresh from the database at every stage
boundary — it **cannot** interrupt a plugin already inside `execute()`.
A scanner plugin that runs an external process for a long time will
finish that step even after cancellation is requested; the next
boundary check catches it before the *following* step starts.

`ScanRepository.get_by_id()` executes with
`execution_options(populate_existing=True)` specifically so this works
correctly across the orchestrator's long-lived session: without it,
SQLAlchemy's identity map would keep returning the same Python object
loaded earlier in the run without refreshing its columns, and a
concurrent cancellation committed by a different session/request would
never be observed. See the comment on that method for the full
reasoning — this was caught by
`tests/test_scan_orchestrator.py::test_cooperative_cancellation_mid_run_finalizes_cancelled`
during this phase, not assumed correct by inspection.

## Concurrency

See [ADR 0008](decisions/0008-transaction-and-concurrency-model.md) for
the full design; summary of the mechanisms visible from this module:

- **Execution ownership** (`ScanRepository.claim_for_execution`): the
  sole point where "only one worker may run a given scan attempt" is
  enforced — an atomic `QUEUED → PREPARING` plus `attempt += 1`, guarded
  by the attempt number. A duplicate/late Celery delivery of the same
  attempt loses this race and `run()` returns without ever calling a
  plugin method.
- **Every other transition** (`ScanStateMachine.transition()`) is
  guarded by a conditional `UPDATE ... WHERE status = :expected` —
  `StaleScanStateError` if the row already moved. `run()` catches this
  once, around the whole execution: if the scan is now `CANCELLED`, it
  publishes `ScanCancelled`; otherwise it logs
  `scan_engine.stale_scan_state_during_run` as a backstop (the primary
  cancellation-detection path is still `_assert_not_cancelled`'s
  explicit re-fetch between stages, above).
- **No distributed lock anywhere in this design** — every race here is
  resolved with a conditional `UPDATE`, which is sufficient because the
  row itself (plus Postgres's own row-level locking) is the only
  coordination primitive any of these races actually need.

## Timeout handling

`asyncio.timeout(scan.timeout_seconds)` wraps the whole
prepare→execute→collect→process sequence. On expiry, the scan
transitions to `TIMED_OUT` (not retried — a scan that ran out of time
once will likely run out of time again with the same configuration;
increasing `timeout_seconds` and using the retry endpoint is the
user-facing recovery path, not an automatic retry loop).

## Known limitations

- **No stuck-scan reaper.** If a worker process crashes mid-scan (e.g.
  the machine is killed, or the DB connection drops), the row is left at
  its **last successfully committed** status (see ADR 0008 §2 — every
  stage transition commits immediately now, so this is coherent partial
  progress, not a lost transaction) with no live worker and nothing yet
  resuming it. A Celery Beat job (periodic scan for rows past
  `started_at + timeout_seconds` with a stale `worker_id`) is the natural
  fix and is intentionally not built this phase — `outbox_dispatcher`
  (ADR 0008 §5) occupies one entry of the same `beat_schedule` seam this
  reaper would use.
- **Cooperative cancellation cannot interrupt a running plugin step** —
  see above. A scanner that hangs inside `execute()` is only bounded by
  the scan's own timeout, not by a cancel request.
- **The outbox dispatcher's own at-least-once delivery gap** (ADR 0008
  §5): a dispatcher crash between a successful publish and recording
  `published_at` produces a second, redundant (but harmless — see ADR
  0008 §6) Celery message for the same attempt.
