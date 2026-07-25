# ADR 0008: Transaction and Concurrency Model (Phase 4A Repair)

## Status

Accepted — repair of the scan-engine-foundation phase, on the same
`feature/scan-engine-foundation` branch, not yet merged.

## Context

A technical review of the just-implemented Phase 4A found eight
production-blocking issues, all confirmed by direct code reading against
the running implementation:

1. **`get_db()` never committed.** `AsyncSession.__aexit__` only closes a
   session; nothing in the request path ever called `commit()`. Every
   mutating request's writes were silently discarded when the connection
   returned to the pool. This bug was invisible to the existing test
   suite because `auth_client`'s `get_db_session` override shared one
   `db_session` (a SAVEPOINT-joined session, rolled back only at test
   teardown) across every request in a test — no commit ever needed to
   happen for those tests to pass. A second bug compounded it:
   `app.api.deps.get_db_session` was a wrapper generator
   (`async for session in get_db(): yield session`) that FastAPI throws
   an escaped exception *into*, not through to `get_db()`'s own
   generator frame — so even a naively added `try/except` in `get_db()`
   would not have run its cleanup deterministically.
2. `ScanOrchestrator.run()` held **one** session/transaction for an
   entire scan run, committing only once at the very end. Postgres holds
   an `UPDATE`'s row lock until `COMMIT`, so the first status transition
   (`PREPARING`) locked the `scans` row for the rest of the run —
   `POST /scans/{id}/cancel`'s own `UPDATE` blocked behind it until the
   run finished. Progress was also invisible to any other session until
   the run ended.
3. Retry scheduling (`orchestrator._handle_failure`) and initial
   dispatch (`ScanService._enqueue`) both called
   `JobQueue.enqueue_scan()` — which ultimately calls Celery's
   `send_task()` — from **inside** a not-yet-committed transaction. A
   crash or rollback after the queue message was sent but before the DB
   commit would leave a Celery task referencing a scan row that might
   never actually exist.
4. `orchestrator._finalize_failed`/`_handle_failure` put a raw
   `str(exc)` into `Scan.error_summary`, the `ScanFailed` domain event,
   and (via `ScanRead`) the API response — a future scanner's exception
   could carry credentials, tokens, or an authenticated URL embedded in
   whatever a subprocess/HTTP client happened to print.
5. `Tenant.status` (`ACTIVE`/`SUSPENDED`/`ARCHIVED`) existed but
   `get_current_principal()` never checked it — only
   `TenantMembership.status`.
6. `APIKeyService.authenticate()` was fully correct and unit-tested but
   never called from `app.api.deps` — there was no `X-API-Key`
   dependency at all, so API keys could be issued and revoked but could
   not authenticate a single request. Scopes were stored but never
   enforced anywhere.

This ADR documents the repaired model. It intentionally does **not**
introduce a distributed lock anywhere — every concurrency problem here is
resolved with either an explicit, short-lived commit boundary or a
SQL-level conditional `UPDATE`, both of which Postgres already gives us
for free.

## Decision

### 1. Request transaction ownership

`get_db()` (`app.db.session`) is the **only** place a request-scoped
transaction is committed or rolled back:

```python
async def get_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

Every successful request commits once, at the very end, regardless of
whether it wrote anything — no "was anything dirty" special-casing, no
scattered `session.commit()` calls in endpoint code. Any exception
(domain error, validation failure, infrastructure failure) rolls back
the entire request's writes.

`app.api.deps.get_db_session` is now a plain alias (`get_db_session =
get_db`), not a wrapper generator — the delegation bug described above
made a wrapper look like safe delegation while silently breaking
exception propagation.

**Two documented, narrow exceptions** to "only `get_db()` commits":

- **Security-relevant audit events that must survive the enclosing
  request's rollback.** `app.services.auth_service` writes an
  `AuditEvent` for a failed login, a detected refresh-token reuse, or an
  invalid verification/reset token, then immediately raises a domain
  error. `docs/security.md` requires these to be recorded regardless of
  the request's outcome — an audit record that vanishes when the
  request it's auditing fails would defeat the point. Each of these four
  call sites has its own explicit `await self._session.commit()`
  immediately after the audit write, with a comment pointing back here.
- **`ScanStateMachine.transition()`** — see below. This is a
  unit-of-work class, not a repository, and CLAUDE.md's "repositories
  never commit" rule does not apply to it; the rule exists to keep
  transaction boundaries explicit, and this class's whole purpose is to
  make one specific boundary (a status transition) as short as possible.

### 2. Worker transaction boundaries: commit at every stage, not once at the end

`ScanStateMachine.transition()` now:

1. Validates the edge in-memory (`assert_valid`, unchanged from ADR
   0007).
2. Issues a SQL-level conditional `UPDATE ... WHERE status = :expected
   RETURNING *` (`ScanRepository.conditional_update`) — flush-only,
   preserving "repositories never commit."
3. Raises `StaleScanStateError` if zero rows matched — someone else
   already moved the row.
4. **Commits immediately.**

This is what releases the row's write lock right after each transition
instead of holding it for the whole run — the direct fix for problem #2
above. `ScanOrchestrator.run()` calls `transition()` at every stage
boundary (`PREPARING → RUNNING → COLLECTING → PROCESSING → SUCCEEDED`,
or the failure/cancellation/timeout equivalents), so a `POST /scans/{id}
/cancel` request's own conditional `UPDATE` never waits longer than the
gap between two stages, not the whole run.

The event-bus's durable-persistence subscriber
(`app.scan_engine.bootstrap._wire_subscribers`) commits immediately
after each `ScanEvent` insert, for the same reason: this is what makes
mid-run progress (`GET /scans/{id}/progress`) visible from a separate
session while a scan is still running, and what makes a crash mid-run
leave *partial, coherent* progress behind — a scan that reaches
`RUNNING` and then the worker process dies is left durably `RUNNING`
with the events already recorded, a legitimate state for a future
stuck-scan reaper to find (see "Known limitations" in
`docs/orchestrator.md`), not silently lost.

No DB lock is ever held across a plugin method call
(`prepare`/`execute`/`collect_results`/`normalize_findings`) — every
lock a stage transition takes is released by that same transition's
commit before the next plugin method is even called.

### 3. Optimistic concurrency, not distributed locks

Two SQL-level conditional-`UPDATE` mechanisms, both on `ScanRepository`,
both flush-only (the caller commits):

- **`conditional_update(tenant_id, scan_id, expected_status, data)`** —
  the general guard under every `ScanStateMachine.transition()` call.
  `UPDATE ... WHERE status = :expected RETURNING *`; `None` (not an
  exception) if zero rows matched, which `ScanStateMachine` turns into
  `StaleScanStateError`.
- **`claim_for_execution(tenant_id, scan_id, expected_attempt,
  worker_id)`** — the **sole** "only one worker may acquire execution
  ownership for a scan attempt" enforcement point. Atomic `QUEUED →
  PREPARING` plus `attempt += 1`, guarded by `WHERE status = 'QUEUED'
  AND attempt = :expected`. A duplicate/late Celery delivery of the same
  logical attempt finds `attempt` already incremented by whichever
  delivery won the race, matches zero rows, and gets `None` back — the
  orchestrator logs `duplicate_delivery_ignored` and returns cleanly.
  This is a genuine no-op, not a retried operation and not an error.

**Why not a distributed lock (Redis or otherwise):** the row itself,
plus Postgres's own MVCC/row-locking, already is the coordination
primitive every one of these races needs. A distributed lock would add
a TTL/lease-renewal/split-brain failure mode for no correctness benefit
over a conditional `UPDATE` — introduce one only if a future requirement
needs coordination across a resource Postgres doesn't itself own.

`ScanOrchestrator.run()` catches `StaleScanStateError` **once**, around
the whole execution (not at every call site): if the DB has moved the
scan to `CANCELLED` since the orchestrator's last read, it publishes
`ScanCancelled`; otherwise it logs
`scan_engine.stale_scan_state_during_run` at error level as a backstop
(the primary, fast-path cancellation detection is still
`_assert_not_cancelled`'s explicit re-fetch between stages).

### 4. Cancellation vs. completion races

Both a completion (`ScanStateMachine.transition(scan, SUCCEEDED/FAILED/
TIMED_OUT)`) and a cancellation (`ScanService.cancel_scan` →
`ScanStateMachine.transition(scan, CANCELLED)`) go through the same
`conditional_update` guard, keyed on the status each side believes the
row is currently in. Whichever commits first wins; the other's `UPDATE`
re-evaluates against the now-committed row under Postgres's own
read-committed semantics, matches zero rows, and raises
`StaleScanStateError` — turned into a clean `ScanNotCancellableError`
(409) by `ScanService.cancel_scan`, or handled by the orchestrator's
single `StaleScanStateError` handler (§3) on the worker side. A
cancelled scan can never later be overwritten as completed, and a
completed scan can never later be overwritten as cancelled — proven with
two genuinely independent Postgres connections in both orderings, see
`tests/test_scan_concurrency_races.py`.

### 5. Transactional outbox for queue dispatch

`ScanService._enqueue()` (initial dispatch) and
`ScanOrchestrator._handle_failure()` (transient-error retry) no longer
call `JobQueue.enqueue_scan()` directly. Both write a `ScanJobOutbox`
row (flush-only) immediately **before** the scan's `QUEUED` transition,
on the same session — the transition's own commit (§2) lands the
outbox row and the state change together, atomically, by sequencing
alone, no new transactional machinery required.

A **fragile "commit, then directly call `.delay()`" approach was
explicitly rejected**: even with the request/worker transaction fixed
(§1, §2), a commit can still succeed while the process crashes before
the subsequent (separate) `enqueue_scan()` call — or the reverse, a
network partition after the Celery `send_task()` succeeds but before the
caller learns about it. Neither ordering is atomic without an outbox;
"commit then call `.delay()`" only narrows the window, it doesn't close
it, and narrowing a real correctness gap without saying so out loud is
worse than a documented one.

**`ScanJobOutbox`** (`backend/app/models/scan_job_outbox.py`,
`alembic/versions/a1fb5cd36013_add_scan_job_outbox_table.py`) fields:

| Field | Purpose |
|---|---|
| `id` | Stable event id (also the row's own idempotency handle) |
| `event_type` | `"scan.run"` today — both initial dispatch and retry |
| `scan_id` / `tenant_id` | Aggregate id + tenant ownership (composite FK to `scans`, CASCADE) |
| `scan_attempt` | Which `Scan.attempt` this dispatch corresponds to |
| `payload` | `{scan_id, tenant_id, correlation_id}` — no secrets, no scan config |
| `countdown_seconds` | Retry backoff delay, carried to `enqueue_scan(..., countdown_seconds=)` |
| `created_at` | Set at insert |
| `published_at` | `NULL` until a successful publish — never set before, never on a failed attempt |
| `attempt_count` | The **dispatcher's own** publish attempts for this row (distinct from `scan_attempt`) |
| `last_attempt_at` | Timestamp of the most recent publish attempt |
| `last_error` | Sanitized only (§7) — never a raw exception |

`UNIQUE (tenant_id, scan_id, scan_attempt)` — a bug that tried to
enqueue one attempt twice fails loudly at insert time (`IntegrityError`)
rather than silently producing a second Celery message later.

**Dispatcher** (`app.workers.tasks.outbox_dispatcher`, a Celery-Beat
task on a 2-second default schedule —
`settings.scan_outbox_dispatch_interval_seconds`): claims one pending
row (`SELECT ... WHERE published_at IS NULL ORDER BY created_at LIMIT 1
FOR UPDATE SKIP LOCKED`), publishes it via the existing
`CeleryJobQueue.enqueue_scan()`, marks `published_at`/`attempt_count`,
commits — **one row, one transaction, one commit**, looped up to
`settings.scan_outbox_dispatch_batch_size` per tick, rather than
claiming a batch inside one transaction. `Scan.job_id` capture moves
here too (best-effort, only after a successful publish), since no real
Celery task id exists at outbox-write time anymore.

`FOR UPDATE SKIP LOCKED` gives **at-least-once delivery with safe
multi-dispatcher claiming**: two dispatcher processes/ticks running the
claim query at the same moment never claim the same row — the second
one simply finds nothing to claim, it does not block waiting for the
first (see `tests/test_scan_outbox_dispatcher.py`, which proves this
non-blocking behavior with two genuinely independent connections).

**The one honestly-documented at-least-once gap**: a dispatcher crash
between a successful `send_task()` and this same function's own
`commit()` (which records `published_at`) leaves a row that still looks
unpublished and gets retried on the next tick — producing a second,
redundant Celery message for the same `scan_attempt`. This is
**never** silently exactly-once; it is deliberately at-least-once, made
safe by the idempotent-consumer property below.

### 6. Idempotent consumers make duplicate delivery harmless

`ScanOrchestrator.run()`'s entry guard generalizes from "if already
CANCELLED, return" to **"if status is not exactly `QUEUED`, return"** —
duplicate/late delivery of an already-claimed, already-terminal, or
already-cancelled scan is a clean, expected no-op under at-least-once
delivery, logged as `duplicate_or_stale_delivery_ignored`, not a task
failure. The actual claiming happens via `claim_for_execution` (§3): a
second delivery of the same logical attempt always loses that race and
returns from `run()` immediately, before any plugin method is called.

Combined with the outbox's own `UNIQUE (tenant_id, scan_id,
scan_attempt)` constraint (which prevents the *write* side from ever
enqueueing the same attempt twice), duplicate delivery can only ever
originate from the queue layer's own at-least-once redelivery — and
that is exactly the case `claim_for_execution` exists to make safe.

### 7. Central error-sanitization boundary

`app.core.error_sanitization.sanitize_exception(exc)` maps
`type(exc).__name__` to a fixed `ErrorCategory` (`NETWORK`, `TIMEOUT`,
`CONFIGURATION`, `TARGET_UNREACHABLE`, `RESOURCE_LIMIT`, `INTERNAL`) and
a **fixed, generic summary sentence per category** — it never reads
`str(exc)` or `exc.args`. `code` is the exception's class name (a Python
identifier, safe by construction). `_finalize_failed` uses this instead
of `str(exc)` for `Scan.error_summary`, the `ScanFailed` event, and (via
the outbox's `last_error`) publish-failure records.

This is deliberately a static lookup, not regex redaction on the raw
message: no real scanner exists yet to validate a "redact but keep some
content" approach against, so "safe by construction" (never reading the
message at all) is the only option that doesn't rest on an untested
assumption. Proven with a deliberately secret-bearing custom exception
type, checked absent from the DB field, the API response shape, every
`ScanEvent` row, and — via a **separate** mechanism, below — structured
logs (see `tests/test_error_sanitization.py`).

**`app.core.log_redaction.redact_log_secrets`** is the operator-only log
surface's own, deliberately different, mechanism: logs need full
diagnostic detail, so this regex-redacts known secret *shapes* (bearer
tokens, `Authorization`/`Cookie` header lines, the `sx_<prefix>.<secret>`
API-key format, JWT-shaped strings, `password=`/`token=`/`secret=`/
`api_key=` key-value pairs, credentials embedded in a URL) out of
otherwise-unrestricted text, plus redacts by structured-field *name*
(`password=`, `api_key=`, etc. passed as `structlog` kwargs) at any
nesting depth, independent of the value's shape. Wired into
`app.core.logging`'s `shared_processors`, applied to every log line
app-wide. Explicitly defense-in-depth, not the primary control — §7's
sanitizer already keeps raw exception text off every other surface
before it would reach a log call, and `auth_service`/`api_key_service`
never log full secrets to begin with.

### 8. Inactive-tenant enforcement and API-key authentication

`app.api.deps._ensure_tenant_active(session, tenant_id)` checks
`Tenant.status == ACTIVE`, applied identically after both authentication
paths in `get_current_principal` — a suspended/archived tenant loses
access immediately on its next request, through either door, not just
at its next token refresh.

`X-API-Key` is now a real request-authentication path
(`app.api.deps._principal_from_api_key`), routed through the previously
unused `APIKeyService.authenticate()`. If `X-API-Key` is present, it is
authenticated exclusively — any Bearer token sent alongside is ignored,
a simple deterministic rule. `Principal` gains `scopes: frozenset[str] |
None` and a nullable `user`; `role` and `scopes` are mutually exclusive
by construction (a JWT principal sets `role`, an API-key principal sets
`scopes`). `app.core.permissions.principal_has_permission()` is the
single dispatch point `require_permission` now uses: an API-key
principal's `scopes` are checked on their own and **never** fall back to
a role-based grant, so a key structurally cannot exceed what it was
actually issued, regardless of how permissive the issuing tenant's own
roles are. Scopes are validated against the `Permission` enum at
issuance time, not just checked later — nothing invalid can ever be
stored in the first place.

The `last_used_at` bump (`APIKeyService.authenticate()`) commits in its
own short transaction, immediately, rather than being held for the rest
of the request — a single automation key can receive many concurrent
requests, and letting `get_db()`'s own end-of-request commit be the only
thing releasing that row's lock would serialize every one of them behind
whichever request's `UPDATE` landed first.

## Consequences

- **Explicitly avoided, as required:** a global mutable DB session; a
  long-lived transaction during scanner execution; queue publication
  before a DB commit; tests that hide failures by sharing one session;
  unrestricted commits buried unpredictably in repositories; a
  distributed lock anywhere a conditional `UPDATE` already solves the
  problem.
- **Every existing test that shared one uncommitted `db_session` across
  multiple HTTP requests via `auth_client` was restructured** — the
  fixture now mints a fresh `AsyncSession` per request from a shared
  connection (`db_session_factory`), so each request's commit/rollback
  is real, not simulated. `real_session_factory` (independent
  connections, real commits, no SAVEPOINT wrapping) is the fixture every
  genuine cross-connection concurrency/durability test in this repair
  uses.
- **A dispatcher crash between publish and recording `published_at` is
  a real, accepted, documented gap** (§5) — at-least-once delivery, not
  exactly-once. Nothing in this system claims otherwise.
- **No stuck-scan reaper still exists** (unchanged limitation from ADR
  0007/`docs/orchestrator.md`) — a worker process that crashes mid-run
  leaves the scan durably at its last-committed, non-terminal status
  with no live worker and nothing yet resuming it. This repair makes
  that state *coherent and inspectable* (§2) where before the whole
  run's progress would have been silently lost; it does not add the
  periodic reaper itself, which remains a Celery Beat seam
  (`celery_app.conf.beat_schedule`) ready for future work — the same
  seam `outbox_dispatcher` now occupies one entry of.
- **Metrics/alerts not yet built, future operational work**: outbox
  backlog depth (count of `published_at IS NULL` rows, and the age of
  the oldest one) and dispatcher publish-failure rate
  (`attempt_count`/`last_error` trends) are the natural signals for a
  future alert — the schema already carries everything needed to
  compute them, nothing here builds the alerting itself.
