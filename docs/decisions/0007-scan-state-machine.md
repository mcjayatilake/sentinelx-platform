# ADR 0007: Scan State Machine

## Status

Accepted — scan-engine-foundation phase.

## Context

The database-foundation phase gave `Scan` a six-value `status` enum
(`PENDING, QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED`) with no
enforcement beyond the DB-level CHECK constraint backing the enum column
— nothing stopped application code from setting `scan.status = X`
directly, in any order. This phase's spec requires ten states (adding
`PREPARING`, `COLLECTING`, `PROCESSING`, `TIMED_OUT`) plus explicit retry,
cancellation, and timeout handling — assigning `.status` freely across a
growing set of call sites was already fragile at six states and would not
scale to ten with retry edges.

## Decision

**`ScanStateMachine.transition()` (`app.scan_engine.state_machine`) is
the sole sanctioned way to mutate `Scan.status` anywhere in this
codebase.** Never `scan.status = X` directly — this is now a binding rule
in `CLAUDE.md`'s "Scan engine development rules." `transition()`:

1. Validates `current -> target` against a static edge table
   (`_VALID_TRANSITIONS`), raising `InvalidScanTransitionError` for any
   edge not explicitly listed.
2. Applies caller-supplied fields (e.g. `error_code`/`error_summary` for
   `FAILED`, `worker_id`/`attempt` for `PREPARING`).
3. Automatically stamps the matching terminal timestamp
   (`completed_at`/`failed_at`/`cancelled_at`/`timed_out_at`) and
   `queued_at`/`started_at` on their first-ever transition, so no call
   site has to remember to set these by hand or risk overwriting an
   already-set `started_at` on an in-place retry.

**`SUCCEEDED` is kept as this codebase's name for "Completed."** The spec
lists `Completed` as a state name; the existing column (from the
database-foundation phase, already migrated and covered by
`test_scan_repository.py`'s existing assertions) uses `SUCCEEDED`.
Renaming it would be a breaking migration for zero behavioral gain —
`SUCCEEDED` and `Completed` mean the same terminal-success state.

**Ten states, four of them terminal with no outbound edges:** `PENDING`,
`QUEUED`, `PREPARING`, `RUNNING`, `COLLECTING`, `PROCESSING` (non-
terminal); `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT` (terminal). See
[`docs/orchestrator.md`](../orchestrator.md#state-machine) for the full
edge diagram.

**The in-place retry edge (`-> QUEUED`) exists on `PREPARING`, `RUNNING`,
and `COLLECTING`, but deliberately not on `PROCESSING`.** A transient
failure while the scanner itself is running or its results are being
collected is plausibly retryable (network blip, transient tool crash). A
failure while *persisting already-collected findings* (`PROCESSING`) is a
different risk profile — retrying risks double-processing or partial
writes against data that's already been through the scanner successfully
once; going straight to `FAILED` is the safer default. This is also
consistent with the finding pipeline's dedup semantics
(`FindingRepository.record_detection`) not being designed for "retry
processing the same batch of findings from scratch."

**Every terminal state has its own timestamp column**, generalizing the
predecessor's single `completed_failed_mutually_exclusive` CHECK
constraint (which only guarded `completed_at`/`failed_at`) to
`terminal_timestamps_mutually_exclusive` across all four
(`completed_at`/`failed_at`/`cancelled_at`/`timed_out_at`) — at most one
may ever be set, matching `status` being the single source of truth for
which terminal state (if any) a scan reached.

## Consequences

- Any future code path that wants to change `Scan.status` must go through
  `ScanStateMachine`, which means it must also justify a new edge in
  `_VALID_TRANSITIONS` if the transition it wants doesn't already exist —
  a deliberate speed bump against "just set the field," not friction to
  work around.
- `InvalidScanTransitionError` is a real, tested failure mode (see
  `tests/test_scan_state_machine.py`), not a theoretical safeguard — the
  orchestrator's own cooperative-cancellation handling had to be fixed
  during this phase specifically because it once attempted an invalid
  `CANCELLED -> CANCELLED` self-transition (see
  [`docs/orchestrator.md`](../orchestrator.md#cancellation)).
- Ten states is more than the six-state predecessor exposes across the
  rest of the platform's `ScanStatus` usages (e.g. any future frontend
  status badge needs to account for `PREPARING`/`COLLECTING`/`PROCESSING`/
  `TIMED_OUT`, not just the original six) — an intentional, spec-driven
  expansion, not scope creep.
