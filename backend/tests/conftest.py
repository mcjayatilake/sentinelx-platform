"""Shared pytest fixtures."""

from collections.abc import AsyncGenerator

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture(scope="session", autouse=True)
def _run_migrations() -> None:
    """Run `alembic upgrade head` against the test database once per session.

    Uses the synchronous Alembic environment (see alembic/env.py) so it can
    run outside pytest-asyncio's event loop. Idempotent: a database already
    at `head` is a no-op.
    """
    config = Config("alembic.ini")
    command.upgrade(config, "head")


@pytest.fixture
async def db_engine() -> AsyncGenerator[AsyncEngine]:
    """Function-scoped (not session-scoped): an async SQLAlchemy engine is
    bound to the event loop it was created in, and pytest-asyncio gives
    each test function its own loop by default. Sharing one engine across
    tests causes cross-loop connection errors; creating one per test avoids
    that entirely at negligible cost.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession]:
    """A session bound to a connection-level transaction that is always
    rolled back at teardown, so each test starts from a clean database
    regardless of what it commits.

    `join_transaction_mode="create_savepoint"` means an explicit
    `session.commit()` inside a test (or inside repository/service code)
    only releases a SAVEPOINT rather than ending the outer transaction —
    the standard SQLAlchemy 2.x pattern for isolating tests against a real
    database. See test_transactions.py for a test that exercises this.
    """
    async with db_engine.connect() as connection:
        await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with session_factory() as session:
            yield session
        await connection.rollback()
