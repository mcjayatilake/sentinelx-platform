"""`ScannerRegistry` — factory-per-scanner-type, fresh instance per `get()`."""

import pytest

from app.scan_engine.exceptions import ScannerNotRegisteredError
from app.scan_engine.registry import ScannerRegistry
from tests.scan_engine_fakes import FakePlugin


def test_get_unregistered_scanner_raises() -> None:
    registry = ScannerRegistry()
    with pytest.raises(ScannerNotRegisteredError):
        registry.get("nonexistent")


def test_is_registered_reflects_registration_state() -> None:
    registry = ScannerRegistry()
    assert registry.is_registered("fake") is False
    registry.register(FakePlugin().capabilities, FakePlugin)
    assert registry.is_registered("fake") is True


def test_get_returns_a_fresh_instance_per_call() -> None:
    registry = ScannerRegistry()
    registry.register(FakePlugin().capabilities, FakePlugin)
    first = registry.get("fake")
    second = registry.get("fake")
    assert first is not second


def test_register_replaces_previous_registration() -> None:
    registry = ScannerRegistry()
    capabilities = FakePlugin().capabilities
    registry.register(capabilities, lambda: FakePlugin(raise_in=None))
    registry.register(capabilities, lambda: FakePlugin(raise_in="execute"))

    plugin = registry.get("fake")
    assert plugin._raise_in == "execute"  # noqa: SLF001 - white-box test of the replacement


def test_list_capabilities_returns_registered_scanners() -> None:
    registry = ScannerRegistry()
    registry.register(FakePlugin().capabilities, FakePlugin)
    capabilities = registry.list_capabilities()
    assert [c.scanner_type for c in capabilities] == ["fake"]
