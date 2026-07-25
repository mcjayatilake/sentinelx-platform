"""`compute_fingerprint` — deterministic, collision-resistant across its
component boundaries."""

from app.scan_engine.pipeline.fingerprint import compute_fingerprint


def test_fingerprint_is_deterministic() -> None:
    a = compute_fingerprint("zap", "rule-1", "https://example.com/login", "XSS in form field")
    b = compute_fingerprint("zap", "rule-1", "https://example.com/login", "XSS in form field")
    assert a == b


def test_fingerprint_changes_with_any_component() -> None:
    base = compute_fingerprint("zap", "rule-1", "https://example.com/login", "detail")
    assert base != compute_fingerprint("nuclei", "rule-1", "https://example.com/login", "detail")
    assert base != compute_fingerprint("zap", "rule-2", "https://example.com/login", "detail")
    assert base != compute_fingerprint("zap", "rule-1", "https://example.com/other", "detail")
    assert base != compute_fingerprint("zap", "rule-1", "https://example.com/login", "other")


def test_fingerprint_is_sha256_hex_digest() -> None:
    fingerprint = compute_fingerprint("zap", "rule-1", "https://example.com", "detail")
    assert len(fingerprint) == 64
    int(fingerprint, 16)  # raises ValueError if not valid hex


def test_fingerprint_separator_prevents_boundary_collision() -> None:
    # Without an unambiguous separator, ("a", "b|c", ...) and ("a|b", "c",
    # ...) could hash identically under a naive "|".join(...).
    a = compute_fingerprint("a", "b|c", "locator", "detail")
    b = compute_fingerprint("a|b", "c", "locator", "detail")
    assert a != b
