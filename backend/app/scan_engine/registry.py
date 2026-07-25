"""`ScannerRegistry` — where scanner plugins register themselves.

Holds a *factory* (`Callable[[], ScannerPlugin]`) per `scanner_type`, not
a shared instance — a fresh plugin object per execution, so no scanner
can accidentally leak state between concurrent scans. Adding a new
scanner is exactly one `registry.register(capabilities, MyPlugin)` call
(see `app.scan_engine.bootstrap.build_default_registry`, currently
empty — "scanners are registered here as they're implemented," mirroring
`app.workers.celery_app`'s `include=[]`).
"""

from collections.abc import Callable

from app.scan_engine.exceptions import ScannerNotRegisteredError
from app.scan_engine.interfaces import ScannerCapabilities, ScannerPlugin


class ScannerRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], ScannerPlugin]] = {}
        self._capabilities: dict[str, ScannerCapabilities] = {}

    def register(
        self, capabilities: ScannerCapabilities, factory: Callable[[], ScannerPlugin]
    ) -> None:
        """Registers `factory` under `capabilities.scanner_type`.
        Re-registering the same `scanner_type` replaces the previous
        registration (useful for tests); this is not itself an error."""
        self._capabilities[capabilities.scanner_type] = capabilities
        self._factories[capabilities.scanner_type] = factory

    def get(self, scanner_type: str) -> ScannerPlugin:
        factory = self._factories.get(scanner_type)
        if factory is None:
            raise ScannerNotRegisteredError(f"No scanner plugin registered for {scanner_type!r}")
        plugin = factory()
        if not isinstance(plugin, ScannerPlugin):
            raise ScannerNotRegisteredError(
                f"Factory for {scanner_type!r} did not produce a ScannerPlugin"
            )
        return plugin

    def is_registered(self, scanner_type: str) -> bool:
        return scanner_type in self._factories

    def list_capabilities(self) -> list[ScannerCapabilities]:
        return list(self._capabilities.values())
