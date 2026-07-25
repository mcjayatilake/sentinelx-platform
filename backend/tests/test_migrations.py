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
    "scan_events",
    "scan_job_outbox",
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


def test_outbox_migration_downgrades_one_revision_and_reupgrades_cleanly() -> None:
    """The specific single-revision downgrade/upgrade cycle the Phase 4A
    repair task requires: back to the pre-fix Phase 4A revision
    (b3214246fc2f, i.e. `-1` from head, since a1fb5cd36013 is the only
    corrective migration on top of it), then forward again. Downgrading
    past the outbox migration must remove *only* `scan_job_outbox`,
    leaving every earlier table (including `scans`/`scan_events` from
    the pre-fix revision) untouched — proving this migration doesn't
    reach back and alter anything b3214246fc2f already created."""
    settings = get_settings()
    config = Config("alembic.ini")

    tables_at_head = _table_names_sync(settings.database_url_sync)
    assert "scan_job_outbox" in tables_at_head

    command.downgrade(config, "-1")
    tables_after_downgrade = _table_names_sync(settings.database_url_sync)
    assert "scan_job_outbox" not in tables_after_downgrade
    assert tables_after_downgrade == tables_at_head - {"scan_job_outbox"}

    command.upgrade(config, "head")
    tables_after_reupgrade = _table_names_sync(settings.database_url_sync)
    assert tables_after_reupgrade == tables_at_head


def test_scan_job_outbox_constraints_and_indexes_exist() -> None:
    """Verifies the hand-checked-against-the-model constraints
    (CLAUDE.md rule 6: never trust autogenerate output without reading
    it) actually exist in the database, not just in the migration file's
    source text."""
    settings = get_settings()
    from sqlalchemy import create_engine

    engine = create_engine(settings.database_url_sync)
    try:
        inspector = inspect(engine)
        unique_constraints = {
            tuple(sorted(uc["column_names"]))
            for uc in inspector.get_unique_constraints("scan_job_outbox")
        }
        assert ("scan_attempt", "scan_id", "tenant_id") in unique_constraints

        fk_pairs = {
            (fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys("scan_job_outbox")
        }
        assert ("scans", ("tenant_id", "id")) in fk_pairs
        assert ("tenants", ("id",)) in fk_pairs

        check_constraint_names = {
            ck["name"] for ck in inspector.get_check_constraints("scan_job_outbox")
        }
        assert "ck_scan_job_outbox_attempt_count_non_negative" in check_constraint_names
        assert "ck_scan_job_outbox_scan_attempt_non_negative" in check_constraint_names
        assert "ck_scan_job_outbox_payload_size_limit" in check_constraint_names

        index_names = {ix["name"] for ix in inspector.get_indexes("scan_job_outbox")}
        assert "ix_scan_job_outbox_pending" in index_names

        columns = {col["name"]: col for col in inspector.get_columns("scan_job_outbox")}
        assert str(columns["created_at"]["type"]).upper().startswith("TIMESTAMP")
        assert columns["created_at"]["type"].timezone is True
        assert columns["published_at"]["type"].timezone is True
        assert columns["last_attempt_at"]["type"].timezone is True
    finally:
        engine.dispose()
