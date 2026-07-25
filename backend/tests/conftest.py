"""Shared pytest fixtures."""

from collections.abc import AsyncGenerator

import pytest
import redis.asyncio as redis
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.deps import get_db_session
from app.core.config import get_settings
from app.core.redis_client import close_redis_client
from app.db.session import reset_engine
from app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    """A client that exercises the REAL production `get_db_session`
    (unlike `auth_client`, which overrides it) — every request opens its
    own genuine connection against `settings.database_url`, exactly like
    a real deployment. `reset_engine()` rebuilds `app.db.session`'s
    module-level engine/pool before and after, since pytest-asyncio gives
    each test function its own event loop and a connection pool's
    connections are bound to whichever loop was running when first
    opened — see `reset_engine()`'s own docstring.
    """
    await reset_engine()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    await reset_engine()


@pytest.fixture
async def auth_client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient]:
    """Like `client`, but every request made in the test shares the same
    connection-level transaction (rolled back at teardown, via
    `db_session_factory`'s underlying `db_connection`) instead of each
    request opening its own connection against `settings.database_url`.
    Required for any test that exercises more than one auth endpoint in
    sequence (register -> login -> refresh -> ...), since later calls must
    see the writes made by earlier ones before the outer transaction is
    ever committed.

    Mirrors `app.db.session.get_db()`'s real commit-on-success/
    rollback-on-exception policy exactly (rather than a bare `yield` with
    no commit at all) — **and, deliberately, mints a fresh `AsyncSession`
    per request** (via the shared factory) rather than reusing one shared
    `Session` object across requests. A shared object was tried first and
    rejected: `Session.rollback()` (unlike `commit()`) always expires
    every loaded object regardless of `expire_on_commit`, so a single
    unrelated 404/403 mid-test would silently invalidate every ORM object
    the test itself was holding via `db_session` (e.g. a `tenant.id`
    access raising `MissingGreenlet` far later in the same test — this
    was caught by the existing suite, not designed around it). Separate
    per-request sessions on the *same* connection avoid that entirely:
    they still see each other's flushed-but-uncommitted writes (and the
    test's own `db_session` writes) via ordinary same-transaction Postgres
    visibility, while each request's own commit/rollback and any object
    expiry stay local to that one throwaway session.
    """

    async def _override_get_db_session() -> AsyncGenerator[AsyncSession]:
        async with db_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

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
async def db_connection(db_engine: AsyncEngine) -> AsyncGenerator[AsyncConnection]:
    """A single connection-level transaction, always rolled back at
    teardown, so each test starts from a clean database regardless of
    what it commits. Shared by `db_session` (one session for the whole
    test) and `db_session_factory` (a fresh session per call, same
    connection) — see `auth_client` for why the distinction matters.
    """
    async with db_engine.connect() as connection:
        await connection.begin()
        yield connection
        await connection.rollback()


@pytest.fixture
def db_session_factory(
    db_connection: AsyncConnection,
) -> async_sessionmaker[AsyncSession]:
    """Mints `AsyncSession`s bound to `db_connection` — each one its own
    identity map, `join_transaction_mode="create_savepoint"` so an
    explicit `session.commit()` only releases a SAVEPOINT rather than
    ending the shared outer transaction (the standard SQLAlchemy 2.x
    pattern for isolating tests against a real database — see
    test_transactions.py). Sessions from different calls still see each
    other's flushed-but-uncommitted writes, since they share one physical
    connection/transaction.
    """
    return async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
async def db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """The one session most tests use directly for repository calls
    outside of an HTTP request."""
    async with db_session_factory() as session:
        yield session


@pytest.fixture
def real_session_factory(db_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A sessionmaker producing genuinely independent sessions — each
    `real_session_factory()()` call opens its own connection (its own
    Postgres backend PID), with real, durable commits, no SAVEPOINT
    wrapping, and no automatic rollback at teardown.

    Use this (not `db_session`) for any test whose whole point is
    cross-connection behavior that `db_session`'s single shared connection
    cannot exercise: commit visibility from a second session, row-lock
    contention, `SELECT ... FOR UPDATE SKIP LOCKED` claiming races, or a
    genuine optimistic-concurrency lost-update race. Tests using this must
    create their own uniquely-named fixture data and clean it up in a
    `finally` block — there is no rollback safety net here (see
    `tests/test_scan_tasks.py` for the established create/commit/cleanup
    pattern this mirrors).
    """
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)
