# CLAUDE.md

Guidance for AI agents (and humans) working in this repository.

## General

- Follow [`docs/CODING_STANDARDS.md`](docs/CODING_STANDARDS.md) for
  language/formatting/testing conventions — this file does not repeat
  those, only what's specific to working with the database layer.
- Follow [`docs/architecture/SECURITY.md`](docs/architecture/SECURITY.md)
  for tenancy, authorization, and secrets-handling constraints — they are
  binding, not suggestions.

## Database development rules

These rules exist because this platform stores multi-tenant customer
security data. Violating them is a security issue, not a style nit.

1. **Never add a tenant-owned repository method without a required
   `tenant_id` argument.** No `list_all()`/`get_by_id()` for tenant-owned
   data that doesn't take `tenant_id` as a required, positional parameter.
   See [`docs/tenant-isolation.md`](docs/tenant-isolation.md).

2. **Never add a generic `Repository[T]` base class.** Each repository is
   deliberately small and explicit — see
   [`backend/app/repositories/base.py`](backend/app/repositories/base.py)
   for why. A shared generic base is exactly the kind of abstraction that
   can hide a missing tenant filter.

3. **When a new tenant-owned entity references another tenant-owned
   entity, propagate `tenant_id` via a composite foreign key**, not a
   plain single-column FK — see any of `Asset`/`Scan`/`Finding` in
   `backend/app/models/` for the pattern, and add the matching
   `UNIQUE (tenant_id, id)` constraint on the parent if it doesn't already
   have one.

4. **Every enum column must use
   `sa.Enum(PyEnum, native_enum=False, create_constraint=True, validate_strings=True, length=32)`
   — both `native_enum=False` and `create_constraint=True`.** Omitting
   `create_constraint=True` silently produces a column with no `CHECK`
   constraint at all (SQLAlchemy 2.x does not default it to `True`). This
   was a real bug caught by tests during the database-foundation phase.

5. **Never use a native PostgreSQL `ENUM` type** (`native_enum=True`) — it
   makes adding a new enum value a non-transactional migration
   (`ALTER TYPE ... ADD VALUE`). See
   [`docs/database-architecture.md`](docs/database-architecture.md).

6. **Never trust `alembic revision --autogenerate` output without reading
   it.** Generate, then open the file and check it against the models that
   changed, before committing. Autogenerate has missed real bugs during
   this project's development (a missing composite-FK-supporting unique
   constraint, and a `CheckConstraint` referencing the wrong column name)
   that were only caught by actually running the migration against
   PostgreSQL — see `database-architecture.md`.

7. **`AuditEvent` is append-only.** Never add an `update()`/`delete()`
   method to `AuditEventRepository`, and never add an `updated_at` column
   to the `AuditEvent` model.

8. **Never store a plaintext secret.** `APIKeyMetadata.hashed_secret` (and
   any future credential field) must be named to make clear it's hashed,
   and must never appear on a `*Read` schema.

9. **Repositories never call `session.commit()`.** They may call
   `session.flush()` (so constraint violations surface immediately), but
   committing is the caller's responsibility — this keeps transaction
   boundaries explicit and testable.

10. **Tests that touch the database run against real PostgreSQL**, not
    SQLite — most of what's worth testing here (composite FKs,
    `CHECK`-constraint enums, `JSONB` size limits) is PostgreSQL-specific.
    See [`docs/local-development.md`](docs/local-development.md) for the
    fixture pattern (`db_session`, `db_engine`) and a known
    pytest-asyncio pitfall it works around.

11. **`Base` uses `__mapper_args__ = {"eager_defaults": True}`
    (`backend/app/db/base.py`) — do not remove it.** Without it, a
    server-generated/`onupdate` column (`created_at`, `updated_at`)
    accessed after a flush inside an async request — e.g. serializing to
    a Pydantic schema right after a service-layer create-then-update —
    raises `MissingGreenlet`, since `AsyncSession` only permits DB IO from
    an awaited call and a later plain attribute access isn't one.

## Authentication / authorization rules

These rules exist because getting auth wrong is a direct security
incident, not a style nit. See
[`docs/authentication.md`](docs/authentication.md),
[`docs/rbac.md`](docs/rbac.md), and [`docs/security.md`](docs/security.md)
for the full design; this is the enforceable summary.

1. **Passwords: Argon2id only, via `app.core.security.hash_password`/
   `verify_password`.** Never hash a password with anything else, never
   log or return one (raw or hashed) in a request/response, never add a
   `deprecated="auto"` fallback scheme without a real pre-existing corpus
   of hashes in that scheme to support (there wasn't one when bcrypt was
   removed from the `CryptContext` — see ADR 0002).

2. **High-entropy random secrets (refresh tokens, API key secrets,
   verification/reset tokens) are SHA-256-hashed via
   `app.core.security.hash_secret`, never Argon2id-hashed.** These are
   two different threat models (see ADR 0004) — do not "unify" them.

3. **No endpoint may resolve `tenant_id` from a client-supplied header,
   query parameter, or path parameter.** Use `CurrentPrincipalDep`
   (`principal.tenant_id`), which comes from a verified access-token
   claim plus a live `TenantMembership` re-check
   (`app.api.deps.get_current_principal`). This is the same discipline as
   rule 1 above (repository `tenant_id` argument), applied one layer up.

4. **No endpoint hand-rolls a role/permission check.** Use
   `Depends(require_permission(Permission.X))` as a router-level
   `dependencies=[...]` entry (not bound to the `principal` parameter's
   default — see `docs/rbac.md` for why that specific combination is
   wrong). Adding a new permission means adding it to the
   `Permission` enum and `ROLE_PERMISSIONS` matrix in
   `app/core/permissions.py`, not a new `if role == ...` somewhere.

5. **Never store a plaintext API key, refresh token, or verification
   token** — same rule as database rule 8, extended: `hashed_token`/
   `hashed_secret` fields, never on a `*Read` schema, and the one-time
   plaintext response (`APIKeyIssueResponse.api_key`,
   `TokenPair.refresh_token`) is never persisted anywhere after that
   response is sent.

6. **Every auth-relevant mutation writes an `AuditEvent` from
   `app.services.auth_service` (or the relevant endpoint for
   membership/API-key actions) — never construct the same audit event at
   more than one call site.** See `docs/security.md` for the full list of
   audited actions. Adding a new security-sensitive action means adding
   one `AuditEventCreate` call at its single natural call site, not
   scattering it across every place that could trigger it.

7. **`POST /auth/password/reset/request` must return the same generic
   response and write no audit event for an unregistered email.** This is
   deliberate enumeration resistance (see `docs/security.md`) — do not
   "fix" it to be more informative.

## Scan engine development rules

These rules exist because scan orchestration is concurrent, multi-worker,
and drives durable state across a long-lived transaction — the failure
modes are subtle (races, silent no-retries, stale reads), not style nits.
See [`docs/scan-engine.md`](docs/scan-engine.md),
[`docs/orchestrator.md`](docs/orchestrator.md), and
[`docs/scanner-interface.md`](docs/scanner-interface.md) for the full
design; this is the enforceable summary.

1. **`ScanStateMachine.transition()` is the sole sanctioned way to mutate
   `Scan.status` anywhere in this codebase.** Never `scan.status = X`
   directly. See ADR 0007. A transition not already listed in
   `_VALID_TRANSITIONS` needs a deliberate edge added, not a workaround.

2. **A new scanner is a `ScannerPlugin` implementation plus one
   `registry.register(...)` call in
   `app.scan_engine.bootstrap.build_default_registry()` — nothing else.**
   If adding a scanner requires touching `ScanOrchestrator`,
   `ScanStateMachine`, or `FindingPipeline`, that's a sign the plugin is
   leaking scanner-specific logic into generic orchestration code (see
   ADR 0005) — fix the plugin, not the orchestrator.

3. **Never call `FindingRepository`/`AuditEventRepository` directly from
   a `ScannerPlugin`.** A plugin returns `NormalizedFinding`s from
   `normalize_findings()`; only `FindingPipeline.process()` (called by
   the orchestrator) touches those repositories. This is what keeps
   dedup/audit/event logic in one place instead of duplicated per
   scanner.

4. **`app.scan_engine` must never import from `app.workers` (or
   `celery` directly).** The dependency only ever points one way:
   `app.workers.tasks.scan_tasks` calls into
   `app.scan_engine.orchestrator`, never the reverse. `JobQueue`
   (`app.scan_engine.job_queue`) is the Celery-free seam; `CeleryJobQueue`
   (`app.workers.job_queue_celery`) is the concrete adapter on the other
   side of it.

5. **Retry *decisions* live in `app.scan_engine.retry.RetryPolicy`, never
   in Celery's own `self.retry()`/`autoretry_for`.** `JobQueue` only ever
   does what it's told (`enqueue_scan`/`cancel`/`dead_letter_scan`) — this
   is what lets a future non-Celery job queue swap in with zero
   orchestrator changes.

6. **`max_attempts` is per-scan (`Scan.max_attempts`), read at retry-
   decision time — never bake a fixed attempt limit into `RetryPolicy`
   itself.** `RetryPolicy.should_retry(attempt, max_attempts)` takes it
   as an argument for exactly this reason; a policy-level `max_attempts`
   field was a real bug caught by `tests/test_scan_orchestrator.py`
   during this phase (a scan's own configured limit was silently
   ignored).

7. **A repository method a long-lived session depends on for detecting
   externally-committed changes (e.g. `ScanRepository.get_by_id()`,
   which the orchestrator's cooperative-cancellation check relies on)
   must use `execution_options(populate_existing=True)`.** Without it,
   SQLAlchemy's identity map silently returns a stale, already-loaded
   object instead of refreshing it from a fresh `SELECT` — see
   `docs/orchestrator.md#cancellation`.

8. **`ScanStateMachine` is a unit-of-work class, not a repository —
   database rule 9 ("repositories never commit") does not apply to it.**
   It takes a session at construction and commits internally after every
   successful conditional transition, deliberately: its whole purpose
   (see [ADR 0008](docs/decisions/0008-transaction-and-concurrency-model.md))
   is bounding how long a `Scan` row's write lock is held, which only
   works if `transition()` itself is the commit boundary. Do not "fix"
   this by moving the commit out to a caller.

9. **`ScanOutboxRepository.claim_next_pending()` is deliberately not
   tenant-scoped**, the same carve-out `TenantRepository.list_all()`
   already documents for the one other legitimately platform-wide
   repository method in this codebase: the outbox dispatcher is a
   background process with no tenant context of its own to filter by —
   it drains pending rows across every tenant by design. Every other
   `ScanOutboxRepository` method still requires `tenant_id`.

## Where things live

```
backend/app/
├── models/         SQLAlchemy ORM — one file per entity, enums.py, mixins.py
├── schemas/         Pydantic Create/Update/Read — never expose ORM models directly
├── repositories/     One explicit class per entity, tenant-scoped where applicable
├── services/        Business orchestration (auth, API keys, email, scans) — the
│                     only place that constructs multi-step flows across repositories
├── api/
│   ├── deps.py        Shared FastAPI dependencies (current user/principal, db session)
│   └── v1/endpoints/  One router module per resource
├── scan_engine/      Plugin contract, orchestrator, event system, retry, storage,
│                       finding pipeline — see docs/scan-engine.md. No Celery import.
├── workers/          Celery app factory, JobQueue adapter, tasks/ — the only
│                       code that imports both Celery and app.scan_engine
└── db/
    ├── base.py        Declarative Base + naming convention
    ├── session.py      Async engine/session
    └── seed.py          Dev-only seed data (never runs automatically)
```
