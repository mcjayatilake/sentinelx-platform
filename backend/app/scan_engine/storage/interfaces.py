"""`ArtifactStorage` — where raw scanner output, logs, artifacts,
screenshots, and future evidence files live.

`LocalFilesystemArtifactStorage` (`app.scan_engine.storage.local`) is the
only implementation this phase ships. This Protocol is the seam a future
S3-compatible implementation (`boto3` is already a backend dependency)
plugs into without any caller changing — see docs/scan-engine.md.

Every method takes `tenant_id` even though `scan_id` alone would resolve
to a unique artifact set, deliberately mirroring the tenant-owned-
repository rule in CLAUDE.md: artifact storage holds tenant customer data
too, and this shape maps directly onto a `{tenant_id}/{scan_id}/{name}`
key layout that keeps tenant isolation legible even in a single shared
bucket.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    key: str
    size_bytes: int
    content_type: str | None = None


class ArtifactStorage(Protocol):
    async def save(
        self,
        tenant_id: uuid.UUID,
        scan_id: uuid.UUID,
        name: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> ArtifactRef: ...

    async def get(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, name: str) -> bytes: ...

    async def delete(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, name: str) -> None: ...

    async def list(self, tenant_id: uuid.UUID, scan_id: uuid.UUID) -> list[ArtifactRef]: ...

    async def exists(self, tenant_id: uuid.UUID, scan_id: uuid.UUID, name: str) -> bool: ...
