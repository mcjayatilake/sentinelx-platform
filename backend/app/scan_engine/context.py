"""`ScanContext` — the dependency-injection bundle passed into every
`ScannerPlugin` method.

This *is* the "every scan must inherit tenant, user, permissions, audit
context" requirement and the "use dependency injection" requirement in
one: a plugin never imports a global settings/session/storage singleton —
everything it needs to act on behalf of a specific tenant is passed in
here, built once per execution by `ScanOrchestrator`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from structlog.stdlib import BoundLogger

    from app.scan_engine.metrics import Metrics
    from app.scan_engine.storage.interfaces import ArtifactStorage


@dataclass(frozen=True, slots=True)
class ScanContext:
    tenant_id: uuid.UUID
    scan_id: uuid.UUID
    project_id: uuid.UUID
    asset_id: uuid.UUID
    scanner_type: str
    requested_by_user_id: uuid.UUID | None
    correlation_id: str
    config: dict[str, Any]
    timeout_seconds: int
    artifact_storage: ArtifactStorage
    metrics: Metrics
    logger: BoundLogger
