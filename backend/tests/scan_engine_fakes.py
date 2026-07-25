"""Shared test doubles for `app.scan_engine` tests — not a test module
itself (no `test_` prefix, so pytest never collects it directly)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.models.enums import AssetType
from app.scan_engine.context import ScanContext
from app.scan_engine.interfaces import ArtifactRef, RawScanOutput, ScannerCapabilities
from app.scan_engine.job_queue import JobQueue, ScanJobPayload
from app.scan_engine.pipeline.stages import NormalizedFinding, RawFinding


class FakePlugin:
    """A `ScannerPlugin` whose every stage is independently
    scriptable — set `raise_in` to the stage name that should raise
    `error_to_raise`, `sleep_in`/`sleep_seconds` to stall a stage (for
    timeout tests), and `hooks` to run an arbitrary side effect at the
    start of a named stage (e.g. cancelling the scan mid-run, simulating
    a concurrent `POST /scans/{id}/cancel`). Everything else succeeds and
    records that it ran, in `self.calls`."""

    def __init__(
        self,
        *,
        raise_in: str | None = None,
        error_to_raise: Exception | None = None,
        findings: list[NormalizedFinding] | None = None,
        sleep_in: str | None = None,
        sleep_seconds: float = 0,
        hooks: dict[str, Callable[[], Awaitable[None]]] | None = None,
    ) -> None:
        self.capabilities = ScannerCapabilities(
            scanner_type="fake",
            display_name="Fake Scanner",
            supported_asset_types=frozenset({AssetType.WEBSITE}),
            default_timeout_seconds=60,
        )
        self._raise_in = raise_in
        self._error_to_raise = error_to_raise or RuntimeError("scripted failure")
        self._findings = findings or []
        self._sleep_in = sleep_in
        self._sleep_seconds = sleep_seconds
        self._hooks = hooks or {}
        self.calls: list[str] = []

    async def _maybe_raise(self, stage: str) -> None:
        self.calls.append(stage)
        hook = self._hooks.get(stage)
        if hook is not None:
            await hook()
        if self._sleep_in == stage:
            await asyncio.sleep(self._sleep_seconds)
        if self._raise_in == stage:
            raise self._error_to_raise

    async def validate_config(self, context: ScanContext) -> None:
        await self._maybe_raise("validate_config")

    async def prepare(self, context: ScanContext) -> None:
        await self._maybe_raise("prepare")

    async def execute(self, context: ScanContext) -> RawScanOutput:
        await self._maybe_raise("execute")
        return RawScanOutput(artifacts=[ArtifactRef(key="raw.json", size_bytes=2)])

    async def collect_results(self, context: ScanContext, raw: RawScanOutput) -> list[RawFinding]:
        await self._maybe_raise("collect_results")
        return []

    def normalize_findings(
        self, context: ScanContext, raw: list[RawFinding]
    ) -> list[NormalizedFinding]:
        self.calls.append("normalize_findings")
        return self._findings

    async def cleanup(self, context: ScanContext) -> None:
        self.calls.append("cleanup")


@dataclass
class FakeJobQueue(JobQueue):
    enqueued: list[tuple[ScanJobPayload, float]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    dead_lettered: list[tuple[ScanJobPayload, str]] = field(default_factory=list)
    next_job_id: str = "job-1"
    # Set to make enqueue_scan() raise instead of succeeding — for
    # tests of the outbox dispatcher's publish-failure path (see
    # tests/test_scan_outbox_dispatcher.py).
    enqueue_error: Exception | None = None

    async def enqueue_scan(self, payload: ScanJobPayload, *, countdown_seconds: float = 0) -> str:
        if self.enqueue_error is not None:
            raise self.enqueue_error
        self.enqueued.append((payload, countdown_seconds))
        return self.next_job_id

    async def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)

    async def dead_letter_scan(self, payload: ScanJobPayload, *, reason: str) -> None:
        self.dead_lettered.append((payload, reason))


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
