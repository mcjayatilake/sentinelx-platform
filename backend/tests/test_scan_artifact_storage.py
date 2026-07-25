"""`LocalFilesystemArtifactStorage` — save/get/list/delete/exists, and the
path-traversal defense in `_resolve_path`."""

import uuid
from pathlib import Path

import pytest

from app.scan_engine.exceptions import ArtifactPathError
from app.scan_engine.storage.local import LocalFilesystemArtifactStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalFilesystemArtifactStorage:
    return LocalFilesystemArtifactStorage(tmp_path)


async def test_save_then_get_round_trips(storage: LocalFilesystemArtifactStorage) -> None:
    tenant_id, scan_id = uuid.uuid4(), uuid.uuid4()
    ref = await storage.save(tenant_id, scan_id, "raw/output.json", b'{"ok": true}')
    assert ref.key == "raw/output.json"
    assert ref.size_bytes == len(b'{"ok": true}')
    assert await storage.get(tenant_id, scan_id, "raw/output.json") == b'{"ok": true}'


async def test_get_missing_artifact_raises_file_not_found(
    storage: LocalFilesystemArtifactStorage,
) -> None:
    with pytest.raises(FileNotFoundError):
        await storage.get(uuid.uuid4(), uuid.uuid4(), "missing.txt")


async def test_exists_reflects_presence(storage: LocalFilesystemArtifactStorage) -> None:
    tenant_id, scan_id = uuid.uuid4(), uuid.uuid4()
    assert await storage.exists(tenant_id, scan_id, "a.txt") is False
    await storage.save(tenant_id, scan_id, "a.txt", b"data")
    assert await storage.exists(tenant_id, scan_id, "a.txt") is True


async def test_delete_removes_artifact_and_is_idempotent(
    storage: LocalFilesystemArtifactStorage,
) -> None:
    tenant_id, scan_id = uuid.uuid4(), uuid.uuid4()
    await storage.save(tenant_id, scan_id, "a.txt", b"data")
    await storage.delete(tenant_id, scan_id, "a.txt")
    assert await storage.exists(tenant_id, scan_id, "a.txt") is False
    await storage.delete(tenant_id, scan_id, "a.txt")  # must not raise


async def test_list_returns_empty_for_unknown_scan(storage: LocalFilesystemArtifactStorage) -> None:
    assert await storage.list(uuid.uuid4(), uuid.uuid4()) == []


async def test_list_returns_all_saved_artifacts(storage: LocalFilesystemArtifactStorage) -> None:
    tenant_id, scan_id = uuid.uuid4(), uuid.uuid4()
    await storage.save(tenant_id, scan_id, "a.txt", b"1")
    await storage.save(tenant_id, scan_id, "sub/b.txt", b"22")
    refs = await storage.list(tenant_id, scan_id)
    assert {r.key for r in refs} == {"a.txt", "sub/b.txt"}


async def test_artifacts_are_isolated_per_tenant_and_scan(
    storage: LocalFilesystemArtifactStorage,
) -> None:
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    scan_id = uuid.uuid4()
    await storage.save(tenant_a, scan_id, "a.txt", b"tenant-a-data")
    assert await storage.exists(tenant_b, scan_id, "a.txt") is False


async def test_empty_name_rejected(storage: LocalFilesystemArtifactStorage) -> None:
    with pytest.raises(ArtifactPathError):
        await storage.save(uuid.uuid4(), uuid.uuid4(), "", b"data")


async def test_relative_traversal_rejected(storage: LocalFilesystemArtifactStorage) -> None:
    with pytest.raises(ArtifactPathError):
        await storage.save(uuid.uuid4(), uuid.uuid4(), "../../../etc/passwd", b"data")


async def test_absolute_path_override_rejected(storage: LocalFilesystemArtifactStorage) -> None:
    # A pathlib join quirk: Path("/a") / "/etc/passwd" discards the left
    # operand entirely and evaluates to "/etc/passwd" — this must be
    # caught, not silently escape the tenant/scan namespace.
    with pytest.raises(ArtifactPathError):
        await storage.save(uuid.uuid4(), uuid.uuid4(), "/etc/passwd", b"data")


async def test_traversal_within_a_valid_looking_name_rejected(
    storage: LocalFilesystemArtifactStorage,
) -> None:
    with pytest.raises(ArtifactPathError):
        await storage.save(uuid.uuid4(), uuid.uuid4(), "reports/../../secret.txt", b"data")
