"""`app.workers.tasks.outbox_dispatcher` — claiming, publishing, and the
multi-dispatcher claim race.

Uses `real_session_factory` (independent connections, real commits) for
every test here: the claim query's `FOR UPDATE SKIP LOCKED` behavior is
inherently a cross-connection property (a single session can't observe
its own lock), and the publish-then-commit sequence needs a real,
separately-committed row to prove `published_at` actually landed.
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.asset import Asset
from app.models.enums import AssetType
from app.models.project import Project
from app.models.scan import Scan
from app.models.tenant import Tenant
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_outbox_repository import ScanOutboxRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.scan_engine.job_queue import ScanJobPayload, scan_job_payload_to_dict
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.scan_outbox import ScanJobOutboxCreate
from app.schemas.tenant import TenantCreate
from app.workers.tasks.outbox_dispatcher import _dispatch_one
from tests.scan_engine_fakes import FakeJobQueue


async def _make_scan_with_outbox_row(session_factory: async_sessionmaker[AsyncSession], slug: str):
    async with session_factory() as session:
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
        outbox = await ScanOutboxRepository(session).create(
            ScanJobOutboxCreate(
                tenant_id=tenant.id,
                scan_id=scan.id,
                scan_attempt=scan.attempt,
                payload=scan_job_payload_to_dict(
                    ScanJobPayload(
                        scan_id=scan.id, tenant_id=tenant.id, correlation_id="corr-outbox"
                    )
                ),
            )
        )
        await session.commit()
        return tenant.id, project.id, asset.id, scan.id, outbox.id


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scan_id,
    asset_id,
    project_id,
    tenant_id,
) -> None:
    async with session_factory() as session:
        await session.execute(delete(Scan).where(Scan.id == scan_id))
        await session.execute(delete(Asset).where(Asset.id == asset_id))
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        await session.commit()


async def test_dispatch_publishes_a_pending_row_and_records_job_id(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, project_id, asset_id, scan_id, _outbox_id = await _make_scan_with_outbox_row(
        real_session_factory, "dispatch1"
    )
    try:
        job_queue = FakeJobQueue(next_job_id="job-abc")
        async with real_session_factory() as session:
            claimed = await _dispatch_one(session, job_queue)
            assert claimed is True

        assert len(job_queue.enqueued) == 1
        payload, _countdown = job_queue.enqueued[0]
        assert payload.scan_id == scan_id

        async with real_session_factory() as verify:
            rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert len(rows) == 1
            assert rows[0].published_at is not None
            assert rows[0].attempt_count == 1
            assert rows[0].last_error is None

            scan = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert scan is not None
            assert scan.job_id == "job-abc"
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_dispatch_of_an_empty_outbox_returns_false(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with real_session_factory() as session:
        claimed = await _dispatch_one(session, FakeJobQueue())
        assert claimed is False


async def test_dispatch_leaves_the_row_pending_with_a_sanitized_error_on_publish_failure(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id, project_id, asset_id, scan_id, _outbox_id = await _make_scan_with_outbox_row(
        real_session_factory, "dispatch2"
    )
    try:
        job_queue = FakeJobQueue(
            enqueue_error=RuntimeError("broker refused: password=hunter2 secret-token=abc123")
        )
        async with real_session_factory() as session:
            claimed = await _dispatch_one(session, job_queue)
            assert claimed is True  # a row was claimed, even though publish failed

        async with real_session_factory() as verify:
            rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert len(rows) == 1
            # Never published on a failed send — stays eligible for the
            # next tick's claim query.
            assert rows[0].published_at is None
            assert rows[0].attempt_count == 1
            assert rows[0].last_error is not None
            assert "hunter2" not in rows[0].last_error
            assert "secret-token" not in rows[0].last_error
            assert "abc123" not in rows[0].last_error

            scan = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert scan is not None
            assert scan.job_id is None  # never captured — publish never actually succeeded

        # And the row really is still claimable — retried on the next tick.
        async with real_session_factory() as session:
            claimed_again = await _dispatch_one(session, FakeJobQueue(next_job_id="job-retry"))
            assert claimed_again is True

        async with real_session_factory() as verify:
            rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert rows[0].published_at is not None
            assert rows[0].attempt_count == 2
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_two_dispatchers_racing_the_same_row_only_one_claims_it(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`FOR UPDATE SKIP LOCKED` is non-blocking, unlike the plain
    conditional `UPDATE` races in test_scan_concurrency_races.py — the
    second dispatcher's claim attempt returns immediately with nothing,
    it never waits on the first dispatcher's lock. Proving that requires
    two genuinely independent connections; a single session can't hold a
    lock against itself."""
    tenant_id, project_id, asset_id, scan_id, outbox_id = await _make_scan_with_outbox_row(
        real_session_factory, "dispatch3"
    )
    try:
        session_a = real_session_factory()
        session_b = real_session_factory()
        try:
            claimed_a = await ScanOutboxRepository(session_a).claim_next_pending()
            assert claimed_a is not None
            assert claimed_a.id == outbox_id

            # session_a hasn't committed yet — the row is still locked —
            # but SKIP LOCKED means session_b doesn't block on it, it
            # just finds nothing else to claim.
            claimed_b = await ScanOutboxRepository(session_b).claim_next_pending()
            assert claimed_b is None

            await session_a.commit()
            await session_b.commit()
        finally:
            await session_a.close()
            await session_b.close()
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_dispatch_drains_multiple_pending_rows_one_at_a_time_without_duplication(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    made = [
        await _make_scan_with_outbox_row(real_session_factory, f"dispatch4-{i}") for i in range(3)
    ]
    scan_ids = {m[3] for m in made}
    try:
        job_queue = FakeJobQueue()
        async with real_session_factory() as session:
            claims = [await _dispatch_one(session, job_queue) for _ in range(4)]
        # Exactly 3 rows existed; the 4th call finds the outbox empty.
        assert claims == [True, True, True, False]
        assert len(job_queue.enqueued) == 3
        assert {payload.scan_id for payload, _ in job_queue.enqueued} == scan_ids

        async with real_session_factory() as verify:
            for tenant_id, _project_id, _asset_id, scan_id, _outbox_id in made:
                rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
                assert len(rows) == 1
                assert rows[0].published_at is not None
    finally:
        for tenant_id, project_id, asset_id, scan_id, _outbox_id in made:
            await _cleanup(
                real_session_factory,
                scan_id=scan_id,
                asset_id=asset_id,
                project_id=project_id,
                tenant_id=tenant_id,
            )


async def test_unique_constraint_rejects_a_duplicate_outbox_row_for_the_same_attempt(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Guards against a bug that tried to enqueue one scan attempt
    twice: the second insert for the same (tenant_id, scan_id,
    scan_attempt) fails loudly at insert time instead of silently
    producing a redundant Celery message later."""
    tenant_id, project_id, asset_id, scan_id, _outbox_id = await _make_scan_with_outbox_row(
        real_session_factory, "dispatch5"
    )
    try:
        async with real_session_factory() as session:
            with pytest.raises(IntegrityError):
                await ScanOutboxRepository(session).create(
                    ScanJobOutboxCreate(
                        tenant_id=tenant_id,
                        scan_id=scan_id,
                        scan_attempt=0,
                        payload=scan_job_payload_to_dict(
                            ScanJobPayload(
                                scan_id=scan_id, tenant_id=tenant_id, correlation_id="dup"
                            )
                        ),
                    )
                )
            await session.rollback()

        async with real_session_factory() as verify:
            rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert len(rows) == 1  # the duplicate never landed
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_dispatch_one_publishes_via_the_real_celery_broker(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every other test in this file uses `FakeJobQueue` to isolate the
    DB-side claiming/marking logic. This one uses the real
    `CeleryJobQueue` against the local Redis broker instead — no worker
    needs to consume the message; a `send_task` client call only needs a
    reachable broker (see tests/test_celery_job_queue.py) — proving the
    dispatcher is actually wired to a real publish, not just tested
    against a double."""
    from app.workers.celery_app import celery_app
    from app.workers.job_queue_celery import CeleryJobQueue

    tenant_id, project_id, asset_id, scan_id, _outbox_id = await _make_scan_with_outbox_row(
        real_session_factory, "dispatch6"
    )
    try:
        job_queue = CeleryJobQueue(celery_app)
        async with real_session_factory() as session:
            claimed = await _dispatch_one(session, job_queue)
            assert claimed is True

        async with real_session_factory() as verify:
            rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert len(rows) == 1
            assert rows[0].published_at is not None

            scan = await ScanRepository(verify).get_by_id(tenant_id, scan_id)
            assert scan is not None
            assert scan.job_id is not None  # a real Celery task id
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )


async def test_run_async_drains_the_outbox_end_to_end(
    real_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exercises `outbox_dispatcher._run_async` directly — the actual
    body of the Celery-Beat-scheduled task, including its own
    per-invocation engine creation/disposal and default `CeleryJobQueue`
    — rather than only its `_dispatch_one` building block. Called
    directly with `await`, the same pattern
    `tests/test_scan_tasks.py` uses for `scan_tasks._run_async`: the
    sync Celery-task wrapper's own `anyio.run()` can't be called from
    inside a already-running event loop (this test's own), which is
    exactly the scenario it's designed for in production (a Celery
    worker process with no event loop of its own yet)."""
    from app.workers.tasks.outbox_dispatcher import _run_async

    tenant_id, project_id, asset_id, scan_id, _outbox_id = await _make_scan_with_outbox_row(
        real_session_factory, "dispatch7"
    )
    try:
        dispatched = await _run_async(batch_size=10)
        assert dispatched >= 1

        async with real_session_factory() as verify:
            rows = await ScanOutboxRepository(verify).list_by_scan(tenant_id, scan_id)
            assert len(rows) == 1
            assert rows[0].published_at is not None
    finally:
        await _cleanup(
            real_session_factory,
            scan_id=scan_id,
            asset_id=asset_id,
            project_id=project_id,
            tenant_id=tenant_id,
        )
