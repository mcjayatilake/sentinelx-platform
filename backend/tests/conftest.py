"""Shared pytest fixtures."""

from collections.abc import AsyncGenerator

import pytest
import redis.asyncio as redis
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_db_session
from app.core.config import get_settings
from app.core.redis_client import close_redis_client
from app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
async def auth_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """Like `client`, but every request made in the test shares `db_session`
    — the same connection-level transaction, rolled back at teardown —
    instead of each request opening its own connection against
    `settings.database_url`. Required for any test that exercises more
    than one auth endpoint in sequence (register -> login -> refresh ->
    ...), since later calls must see the writes made by earlier ones
    before the outer transaction is ever committed.
    """

    async def _override_get_db_session() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _override_get_db_session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            yield ac
    finally:
        del app.dependency_overrides[get_db_session]


_REDIS_TEST_KEY_PATTERNS = ("auth:ratelimit:*", "auth:denylist:jti:*")


async def _flush_auth_redis_keys(client: redis.Redis) -> None:
    for pattern in _REDIS_TEST_KEY_PATTERNS:
        keys = [key async for key in client.scan_iter(match=pattern)]
        if keys:
            await client.delete(*keys)


@pytest.fixture(autouse=True)
async def redis_client() -> AsyncGenerator[redis.Redis]:
    """Auto-applied so rate-limit counters and logout-denylist entries
    never leak between tests via the shared local Redis instance. Only
    ever clears SentinelX's own key prefixes (`auth:ratelimit:*`,
    `auth:denylist:jti:*`) — never `FLUSHDB` — so this is safe even
    against a Redis instance shared with something else. Also handed to
    any test that declares it as a parameter, for direct assertions
    (e.g. "logging out actually denylisted this jti").

    Also tears down `app.core.redis_client`'s process-wide singleton
    before and after each test — same reasoning as `db_engine` being
    function-scoped: that client is bound to whatever event loop was
    running when it was first created, and pytest-asyncio gives each
    test function its own loop, so a connection left over from a
    previous test raises `RuntimeError: Event loop is closed`.
    """
    await close_redis_client()
    settings = get_settings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    await _flush_auth_redis_keys(client)
    yield client
    await _flush_auth_redis_keys(client)
    await client.aclose()
    await close_redis_client()


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
