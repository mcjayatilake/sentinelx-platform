"""`ScanStateMachine` — the sole enforcement point for `Scan.status`
transitions.

The only sanctioned way to mutate `Scan.status` anywhere in this
codebase is `ScanStateMachine.transition()` — never assign
`scan.status = X` directly (see CLAUDE.md's "Scan engine development
rules"). `SUCCEEDED` is this codebase's name for the spec's "Completed"
state — see docs/decisions/0007-scan-state-machine.md.

Session-owning (`ScanStateMachine(session)`), not stateless: each
`transition()` call commits immediately after its own conditional
`UPDATE`, releasing the row's lock right away instead of holding it for
however long the caller does next — see
docs/decisions/0008-transaction-and-concurrency-model.md for why a
long-held lock on this row was a real production-blocking bug (it let a
worker's in-progress scan block a concurrent cancel request for the
scan's entire run).
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ScanStatus
from app.models.scan import Scan
from app.repositories.scan_repository import ScanRepository
from app.scan_engine.exceptions import InvalidScanTransitionError, StaleScanStateError
from app.schemas.scan import ScanUpdate

# `QUEUED` appears as a valid target from PREPARING/RUNNING/COLLECTING —
# that's the in-place retry edge (see docs/orchestrator.md): a transient
# failure re-queues the *same* Scan row rather than creating a new one.
# PROCESSING deliberately has no such edge — a failure while persisting
# findings goes straight to FAILED, not back through the state machine.
_VALID_TRANSITIONS: dict[ScanStatus, frozenset[ScanStatus]] = {
    ScanStatus.PENDING: frozenset({ScanStatus.QUEUED, ScanStatus.CANCELLED}),
    ScanStatus.QUEUED: frozenset({ScanStatus.PREPARING, ScanStatus.CANCELLED, ScanStatus.FAILED}),
    ScanStatus.PREPARING: frozenset(
        {
            ScanStatus.RUNNING,
            ScanStatus.QUEUED,
            ScanStatus.CANCELLED,
            ScanStatus.FAILED,
            ScanStatus.TIMED_OUT,
        }
    ),
    ScanStatus.RUNNING: frozenset(
        {
            ScanStatus.COLLECTING,
            ScanStatus.QUEUED,
            ScanStatus.CANCELLED,
            ScanStatus.FAILED,
            ScanStatus.TIMED_OUT,
        }
    ),
    ScanStatus.COLLECTING: frozenset(
        {
            ScanStatus.PROCESSING,
            ScanStatus.QUEUED,
            ScanStatus.CANCELLED,
            ScanStatus.FAILED,
            ScanStatus.TIMED_OUT,
        }
    ),
    ScanStatus.PROCESSING: frozenset(
        {ScanStatus.SUCCEEDED, ScanStatus.FAILED, ScanStatus.CANCELLED}
    ),
    # Terminal states: no outbound edges.
    ScanStatus.SUCCEEDED: frozenset(),
    ScanStatus.FAILED: frozenset(),
    ScanStatus.CANCELLED: frozenset(),
    ScanStatus.TIMED_OUT: frozenset(),
}

_TERMINAL_TIMESTAMP_FIELD: dict[ScanStatus, str] = {
    ScanStatus.SUCCEEDED: "completed_at",
    ScanStatus.FAILED: "failed_at",
    ScanStatus.CANCELLED: "cancelled_at",
    ScanStatus.TIMED_OUT: "timed_out_at",
}


class ScanStateMachine:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._scans = ScanRepository(session)

    def assert_valid(self, current: ScanStatus, target: ScanStatus) -> None:
        if target not in _VALID_TRANSITIONS.get(current, frozenset()):
            raise InvalidScanTransitionError(
                f"Cannot transition scan from {current!r} to {target!r}"
            )

    async def transition(
        self,
        scan: Scan,
        target: ScanStatus,
        **fields: Any,
    ) -> Scan:
        """Validates the `scan.status -> target` edge, then atomically
        updates the row to `target` plus `fields` — guarded by a SQL-level
        `WHERE status = <scan's currently-believed status>` (see
        `ScanRepository.conditional_update`), not just the in-memory
        `assert_valid()` check above. The matching terminal timestamp
        (`completed_at`/`failed_at`/`cancelled_at`/`timed_out_at`) and
        `queued_at`/`started_at` (on their first-ever transition) are set
        to now automatically unless already present in `fields`.

        Commits immediately on success, releasing the row's lock. Raises
        `StaleScanStateError` (never silently overwrites) if a concurrent
        transaction already moved `scan` out of the state this call
        believed it was in — e.g. a cancellation racing this same scan's
        completion.
        """
        self.assert_valid(scan.status, target)

        update_fields: dict[str, Any] = {"status": target, **fields}

        terminal_field = _TERMINAL_TIMESTAMP_FIELD.get(target)
        if terminal_field is not None and terminal_field not in update_fields:
            update_fields[terminal_field] = datetime.now(UTC)

        if target == ScanStatus.QUEUED and scan.queued_at is None:
            update_fields.setdefault("queued_at", datetime.now(UTC))
        if target == ScanStatus.RUNNING and scan.started_at is None:
            update_fields.setdefault("started_at", datetime.now(UTC))

        updated = await self._scans.conditional_update(
            scan.tenant_id,
            scan.id,
            expected_status=scan.status,
            data=ScanUpdate.model_validate(update_fields),
        )
        if updated is None:
            raise StaleScanStateError(
                f"Scan {scan.id} was no longer {scan.status!r} when transitioning to {target!r}"
            )
        await self._session.commit()
        return updated
