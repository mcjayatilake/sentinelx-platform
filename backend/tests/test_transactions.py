"""Transaction rollback behavior.

Also serves as the test proving the `db_session` fixture's isolation model
(SAVEPOINT-per-test via `join_transaction_mode="create_savepoint"`) works
as documented in conftest.py: a nested rollback undoes only what happened
inside it, and the session remains usable afterward.
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.tenant_repository import TenantRepository
from app.schemas.tenant import TenantCreate


async def test_nested_rollback_discards_only_its_own_work(db_session: AsyncSession) -> None:
    repo = TenantRepository(db_session)
    kept = await repo.create(TenantCreate(name="Kept", slug="kept-tenant"))

    # Roll back the SAVEPOINT transaction object itself, not the session as
    # a whole — `session.rollback()` would unwind everything back to the
    # session's root transaction, including `kept` above.
    nested = await db_session.begin_nested()
    await repo.create(TenantCreate(name="Discarded", slug="discarded-tenant"))
    await nested.rollback()

    assert await repo.get_by_slug("discarded-tenant") is None
    # The session must still be usable, and the earlier, already-flushed
    # row must still be visible — the rollback did not affect the outer
    # (fixture-level) transaction.
    still_there = await repo.get_by_slug("kept-tenant")
    assert still_there is not None
    assert still_there.id == kept.id


async def test_failed_flush_can_be_recovered_with_rollback(db_session: AsyncSession) -> None:
    """`async with session.begin_nested()` rolls back to the SAVEPOINT
    automatically when an exception propagates out of the block — the
    idiomatic way to recover from an expected constraint violation without
    losing earlier work in the same test."""
    repo = TenantRepository(db_session)
    await repo.create(TenantCreate(name="Original", slug="dup-slug"))

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await repo.create(TenantCreate(name="Duplicate", slug="dup-slug"))

    # Session is usable again after recovering from the failed flush.
    found = await repo.get_by_slug("dup-slug")
    assert found is not None
    assert found.name == "Original"


async def test_fixture_isolation_starts_clean(db_session: AsyncSession) -> None:
    """No cross-test leakage: if a previous test's rollback failed, this
    slug (unique to this test) would already exist from a prior run within
    the same session-scoped database."""
    repo = TenantRepository(db_session)
    assert await repo.get_by_slug("kept-tenant") is None
    assert await repo.get_by_slug("dup-slug") is None
