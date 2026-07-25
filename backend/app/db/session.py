"""Async SQLAlchemy engine and session factory."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

settings = get_settings()


def _build_engine() -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        echo=settings.debug and settings.environment == "local",
    )


def _build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


engine = _build_engine()
AsyncSessionLocal = _build_session_factory(engine)


async def reset_engine() -> None:
    """Disposes and recreates the module-level `engine`/`AsyncSessionLocal`.

    Mirrors `app.core.redis_client.close_redis_client()`'s reasoning: a
    connection pool is bound to whichever asyncio event loop was running
    when a pooled connection was first opened. A real deployment only
    ever has one event loop for the life of the process, so this is a
    non-issue there — it matters for a test suite where pytest-asyncio
    gives each test function its own loop by default, and a test exercises
    this module-level engine directly (via `app.api.deps.get_db_session`,
    e.g. through the plain `client` fixture) rather than the
    per-test-isolated `db_session`/`real_session_factory` fixtures, which
    each build their own engine already. See `tests/conftest.py`'s
    `client` fixture.
    """
    global engine, AsyncSessionLocal
    await engine.dispose()
    engine = _build_engine()
    AsyncSessionLocal = _build_session_factory(engine)


async def get_db() -> AsyncGenerator[AsyncSession]:
    """FastAPI dependency yielding a request-scoped database session.

    Explicit commit-on-success / rollback-on-exception transaction policy:
    a request handler's mutations are durable only if the handler (and
    FastAPI's own response serialization, which runs before this
    generator resumes — see docs/decisions/0008-transaction-and-concurrency-model.md)
    completes without raising. This is the sole place in the codebase that
    commits a request-scoped session — repositories only ever `flush()`
    (see CLAUDE.md's database rule 9), and no endpoint/service calls
    `commit()` directly.

    `app.api.deps.get_db_session` is a plain alias for this generator, not
    a wrapper — a prior `async for session in get_db(): yield session`
    wrapper looked like safe delegation but wasn't: FastAPI throws an
    escaped exception back into the *wrapper's* suspended `yield`, not
    into this generator's, so this function's `except` would never run
    deterministically through that indirection.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
