# ADR 0006: Scan Event System

## Status

Accepted — scan-engine-foundation phase.

## Context

The scan engine needs internal domain events (`ScanQueued`, `ScanStarted`,
`ScanProgress`, `ScanCompleted`, `ScanFailed`, `ScanCancelled`,
`ScanTimedOut`, `FindingCreated`, `FindingUpdated`) publishable without
tight coupling — the orchestrator should not need to know that progress
reporting, metrics, structured logging, and durable persistence all care
about the same events. It also needs a durable, queryable record of a
scan's execution history (for `GET /scans/{id}/progress` and future
debugging/audit needs), and a future-compatible shape for a websocket
endpoint to tail.

## Decision

**Three layers, not one:**

1. **Typed `DomainEvent` dataclasses** (`app.scan_engine.events`) — one
   frozen dataclass per event, all sharing a `scan_id`/`tenant_id`/
   `correlation_id`/`occurred_at` base. Value objects, not ORM models;
   they exist to be published, not queried.
2. **`InMemoryEventBus`** — a process-local pub/sub bus. `subscribe(cls,
   handler)` registers an async handler per event type; `publish(event)`
   fans out to every subscriber for `type(event)`. One handler raising
   never breaks `publish()` for the others or for the producer — the bus
   catches and logs per-handler, never lets a subscriber's bug become the
   orchestrator's bug.
3. **Durable persistence to `ScanEvent`** — a new, append-only table
   (`app.models.scan_event`), populated by a persistence subscriber
   `app.scan_engine.bootstrap._wire_subscribers` attaches for all nine
   event types. This is the actual backing store for progress reporting
   and event history; the bus itself has no persistence of its own and is
   scoped to one execution's process (Celery workers are separate
   processes — there is no cross-process pub/sub need).

**Why not just write to the database directly, skipping the bus?**
Because "publishable without tight coupling" is a real requirement, not
decoration: the orchestrator calls `self._events.publish(...)` and knows
nothing about persistence, metrics, or logging as concrete concerns.
`build_orchestrator` wires three subscribers today (persist, count a
metric, log structurally) to every event type from one place
(`_EVENT_TYPE_BY_CLASS`); a fourth (a future notification system
subscribing to `FindingCreated`/`FindingUpdated`) is one more
`bus.subscribe()` call in `bootstrap.py`, zero orchestrator changes.

**`ScanEvent.event_type` is free text, not an enum-CHECK column.** The
nine formal events always populate it with one of a fixed, documented
set of strings, but the orchestrator also logs an incidental operational
sub-event this way (a `scan.progress` event carrying a "retry scheduled"
message) rather than inventing a tenth `DomainEvent` subclass — or a
migration each time a similar operational note is needed — for something
that isn't a first-class domain event a subscriber needs to react to
differently.

**`ScanEvent.created_at` uses `clock_timestamp()`, not the
`CreatedAtMixin` default of `now()`.** At the time this decision was
made, a single scan's entire run executed inside one long-lived,
uncommitted transaction (committed once at the very end). PostgreSQL's
`now()` returns the *transaction start* time, frozen for the whole
transaction — every `ScanEvent` a run created would otherwise carry the
exact same timestamp, making `ORDER BY created_at DESC` (which
`ScanEventRepository.get_latest_progress` depends on) non-deterministic
for any scan with more than one progress update. `clock_timestamp()`
returns the actual wall-clock time at each statement, which is what
ordering by "most recent" actually needs.

*Update (Phase 4A repair, [ADR 0008](0008-transaction-and-concurrency-model.md)):*
each `ScanEvent` insert now commits in its own short transaction, so the
original root cause above no longer applies as stated — `now()` would
technically return a distinct value per insert today too. `clock_timestamp()`
remains the right column default regardless: it's correct independent of
whether future inserts end up batched into one transaction again, and
there is no correctness reason to revert it. This column-level override
is scoped to `ScanEvent` only — `AuditEvent` and other `CreatedAtMixin`
tables are unaffected.

## What this is not

`InMemoryEventBus` is not a message queue and not a replacement for
Celery/Redis — it has no durability, no cross-process delivery, and no
retry of its own. It is purely an in-process fan-out mechanism scoped to
a single scan execution, existing to decouple the orchestrator from
"who's listening," not to move events between processes.

## Consequences

- Every new domain event type needs three things kept in sync: the
  dataclass in `events.py`, an entry in `bootstrap.py`'s
  `_EVENT_TYPE_BY_CLASS`, and (if it carries fields worth persisting) a
  branch in `_to_scan_event_create()`. This is an accepted, explicit cost
  of the typed-event approach over "just pass a dict everywhere."
- A subscriber that's slow (e.g. a synchronous-feeling blocking call
  inside an `async def` handler) delays `publish()`'s caller, since
  `InMemoryEventBus.publish()` awaits each handler in sequence — there is
  no background dispatch. Acceptable for this phase's three
  lightweight subscribers (a DB insert, a counter increment, a log call);
  worth revisiting if a slow subscriber is ever added.
- No event survives a worker process crash before it reaches the
  persistence subscriber (i.e. before `session.flush()` inside that
  handler) — this is the same "no stuck-scan reaper" limitation
  documented in `docs/orchestrator.md`, not a separate gap.
