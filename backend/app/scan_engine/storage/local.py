"""`LocalFilesystemArtifactStorage` — the only `ArtifactStorage`
implementation this phase ships (`settings.artifact_storage_backend`
only has the `"local"` `Literal` option for exactly this reason).

Methods are `async def` to satisfy the `ArtifactStorage` Protocol, but the
underlying filesystem I/O is synchronous — honestly documented, not
pretended to be true async I/O. This runs inside a Celery task's
single-task-at-a-time event loop, not a concurrently-serving FastAPI
request path, where that distinction would matter more.
"""

import uuid
from pathlib import Path

from app.scan_engine.exceptions import ArtifactPathError
from app.scan_engine.storage.interfaces import ArtifactRef


class LocalFilesystemArtifactStorage:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir.resolve()

    def _resolve_path(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, name: str) -> Path:
        """`name` is untrusted input — eventually scanner-tool-derived
        (a scanner's own reported filenames, once a real one exists).
        Rejects any name that would resolve outside the `(tenant_id,
        scan_id)` namespace — `..` segments, an absolute path (which,
        via `pathlib`'s join semantics, would otherwise silently discard
        the namespace prefix entirely), or a symlink escape."""
        if not name:
            raise ArtifactPathError("Artifact name must not be empty")

        scan_dir = (self._base_dir / str(tenant_id) / str(scan_id)).resolve()
        candidate = (scan_dir / name).resolve()

        if candidate != scan_dir and scan_dir not in candidate.parents:
            raise ArtifactPathError(f"Artifact name escapes its scan namespace: {name!r}")

        return candidate

    async def save(
        self,
        tenant_id: uuid.UUID,
        scan_id: uuid.UUID,
        name: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> ArtifactRef:
        path = self._resolve_path(tenant_id, scan_id, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ArtifactRef(key=name, size_bytes=len(data), content_type=content_type)

    async def get(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, name: str) -> bytes:
        path = self._resolve_path(tenant_id, scan_id, name)
        if not path.is_file():
            raise FileNotFoundError(f"Artifact not found: {name!r}")
        return path.read_bytes()

    async def delete(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, name: str) -> None:
        path = self._resolve_path(tenant_id, scan_id, name)
        path.unlink(missing_ok=True)

    async def list(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> list[ArtifactRef]:
        scan_dir = (self._base_dir / str(tenant_id) / str(scan_id)).resolve()
        if not scan_dir.is_dir():
            return []
        return [
            ArtifactRef(key=str(path.relative_to(scan_dir)), size_bytes=path.stat().st_size)
            for path in sorted(scan_dir.rglob("*"))
            if path.is_file()
        ]

    async def exists(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, name: str) -> bool:
        path = self._resolve_path(tenant_id, scan_id, name)
        return path.is_file()
