"""Metrics hook — a no-op default, ready for a real backend later.

Mirrors `app.core.security.BreachedPasswordChecker`/
`NullBreachedPasswordChecker` from the auth phase: the hook exists at
every call site now, so a real Prometheus/OpenTelemetry-metrics backend
is a drop-in replacement (`ScanOrchestrator(..., metrics=RealMetrics())`),
not a new integration point to build later.
"""

from typing import Protocol


class Metrics(Protocol):
    def increment(self, name: str, *, tags: dict[str, str] | None = None) -> None: ...

    def observe_duration(
        self, name: str, seconds: float, *, tags: dict[str, str] | None = None
    ) -> None: ...


class NoOpMetrics:
    def increment(self, name: str, *, tags: dict[str, str] | None = None) -> None:
        pass

    def observe_duration(
        self, name: str, seconds: float, *, tags: dict[str, str] | None = None
    ) -> None:
        pass
