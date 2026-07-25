"""`ScanStateMachine` — the sole enforcement point for `Scan.status`
transitions, and the automatic timestamp behavior it applies.

Genuine cross-connection races (the actual point of the SQL-level
conditional `UPDATE` this class uses) are covered in
`test_scan_concurrency_races.py`, using `real_session_factory` — a single
shared `db_session` here can't produce a real race, only prove the
non-concurrent behavior.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, ScanStatus
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.exceptions import InvalidScanTransitionError
from app.scan_engine.state_machine import ScanStateMachine
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.tenant import TenantCreate


async def _make_scan(session: AsyncSession, slug: str):
    tenant = await TenantRepository(session).create(TenantCreate(name=slug, slug=slug))
    project = await ProjectRepository(session).create(
        ProjectCreate(tenant_id=tenant.id, name=slug, slug=slug)
    )
    asset = await AssetRepository(session).create(
        AssetCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_type=AssetType.WEBSITE,
            name=slug,
            locator="https://example.com",
        )
    )
    scan = await ScanRepository(session).create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="fake"
        )
    )
    return scan


def test_assert_valid_accepts_documented_edges(db_session: AsyncSession) -> None:
    machine = ScanStateMachine(db_session)
    machine.assert_valid(ScanStatus.PENDING, ScanStatus.QUEUED)
    machine.assert_valid(ScanStatus.QUEUED, ScanStatus.PREPARING)
    machine.assert_valid(ScanStatus.PROCESSING, ScanStatus.SUCCEEDED)


def test_assert_valid_rejects_skipping_a_stage(db_session: AsyncSession) -> None:
    machine = ScanStateMachine(db_session)
    with pytest.raises(InvalidScanTransitionError):
        machine.assert_valid(ScanStatus.PENDING, ScanStatus.RUNNING)


def test_assert_valid_rejects_transitions_out_of_terminal_states(db_session: AsyncSession) -> None:
    machine = ScanStateMachine(db_session)
    for terminal in (
        ScanStatus.SUCCEEDED,
        ScanStatus.FAILED,
        ScanStatus.CANCELLED,
        ScanStatus.TIMED_OUT,
    ):
        with pytest.raises(InvalidScanTransitionError):
            machine.assert_valid(terminal, ScanStatus.QUEUED)


def test_processing_has_no_retry_edge_back_to_queued(db_session: AsyncSession) -> None:
    # Deliberate design point: a failure while persisting findings goes
    # straight to FAILED, not back through an in-place retry.
    machine = ScanStateMachine(db_session)
    with pytest.raises(InvalidScanTransitionError):
        machine.assert_valid(ScanStatus.PROCESSING, ScanStatus.QUEUED)


async def test_transition_persists_status_and_sets_queued_at(db_session: AsyncSession) -> None:
    scan = await _make_scan(db_session, "sm1")
    machine = ScanStateMachine(db_session)

    updated = await machine.transition(scan, ScanStatus.QUEUED)
    assert updated.status == ScanStatus.QUEUED
    assert updated.queued_at is not None


async def test_transition_sets_started_at_only_on_first_running_transition(
    db_session: AsyncSession,
) -> None:
    scan = await _make_scan(db_session, "sm2")
    machine = ScanStateMachine(db_session)

    scan = await machine.transition(scan, ScanStatus.QUEUED)
    scan = await machine.transition(scan, ScanStatus.PREPARING)
    scan = await machine.transition(scan, ScanStatus.RUNNING)
    first_started_at = scan.started_at
    assert first_started_at is not None

    # RUNNING -> QUEUED (in-place retry edge) -> PREPARING -> RUNNING again
    # must not overwrite the original started_at.
    scan = await machine.transition(scan, ScanStatus.QUEUED)
    scan = await machine.transition(scan, ScanStatus.PREPARING)
    scan = await machine.transition(scan, ScanStatus.RUNNING)
    assert scan.started_at == first_started_at


async def test_transition_to_failed_sets_failed_at_and_error_fields(
    db_session: AsyncSession,
) -> None:
    scan = await _make_scan(db_session, "sm3")
    machine = ScanStateMachine(db_session)

    scan = await machine.transition(scan, ScanStatus.QUEUED)
    scan = await machine.transition(scan, ScanStatus.PREPARING)
    scan = await machine.transition(
        scan, ScanStatus.FAILED, error_code="boom", error_summary="it broke"
    )
    assert scan.status == ScanStatus.FAILED
    assert scan.failed_at is not None
    assert scan.error_code == "boom"
    assert scan.error_summary == "it broke"


async def test_transition_raises_and_does_not_mutate_on_invalid_edge(
    db_session: AsyncSession,
) -> None:
    scan = await _make_scan(db_session, "sm4")
    machine = ScanStateMachine(db_session)

    with pytest.raises(InvalidScanTransitionError):
        await machine.transition(scan, ScanStatus.SUCCEEDED)
    assert scan.status == ScanStatus.PENDING


async def test_transition_commits_and_releases_the_row_immediately(
    db_session: AsyncSession,
) -> None:
    """A successful `transition()` must not leave the row's write lock
    held for the caller to release later — it commits itself. Proven here
    by transitioning, then immediately updating the *same* row again on
    the *same* session without any explicit commit in between; if the
    first transition hadn't actually committed (only flushed), this
    second statement would still succeed today (same session, same
    uncommitted transaction) so this test alone can't distinguish
    "committed" from "merely flushed" — see
    test_scan_progress_durability.py for the real cross-session proof."""
    scan = await _make_scan(db_session, "sm5")
    machine = ScanStateMachine(db_session)

    scan = await machine.transition(scan, ScanStatus.QUEUED)
    # A second, unrelated write on the same session must proceed without
    # ever blocking on a lock held by the transition above.
    scan = await machine.transition(scan, ScanStatus.PREPARING)
    assert scan.status == ScanStatus.PREPARING
