"""Migration correctness: upgrade on a clean database, downgrade, re-upgrade.

Runs against its own throwaway connection (not the `db_session` fixture)
since it needs to actually drop/recreate schema objects, which a
SAVEPOINT-wrapped test session must not do.
"""

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

EXPECTED_TABLES = {
    "tenants",
    "users",
    "tenant_memberships",
    "projects",
    "assets",
    "scans",
    "findings",
    "finding_references",
    "audit_events",
    "api_key_metadata",
}


def _table_names_sync(sync_url: str) -> set[str]:
    from sqlalchemy import create_engine

    engine = create_engine(sync_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


async def test_upgrade_creates_all_tables() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))
    await engine.dispose()

    assert tables >= EXPECTED_TABLES


def test_downgrade_then_upgrade_is_clean() -> None:
    """A full downgrade removes every table this migration created, and a
    subsequent upgrade recreates them all — proving `downgrade()` isn't a
    stub and the migration is safely reversible."""
    settings = get_settings()
    config = Config("alembic.ini")

    command.downgrade(config, "base")
    tables_after_downgrade = _table_names_sync(settings.database_url_sync)
    assert not (EXPECTED_TABLES & tables_after_downgrade)

    command.upgrade(config, "head")
    tables_after_reupgrade = _table_names_sync(settings.database_url_sync)
    assert tables_after_reupgrade >= EXPECTED_TABLES
