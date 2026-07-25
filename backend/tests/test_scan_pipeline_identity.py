"""`PassthroughParser`/`IdentityNormalizer` — real, shipped reference
`Parser`/`Normalizer` implementations (not test-only stand-ins)."""

from app.models.enums import FindingConfidence, FindingSeverity
from app.scan_engine.interfaces import ArtifactRef, RawScanOutput
from app.scan_engine.pipeline.identity import IdentityNormalizer, PassthroughParser
from app.scan_engine.pipeline.stages import RawFinding


def _raw_finding(**overrides: object) -> RawFinding:
    defaults: dict[str, object] = {
        "rule_id": "rule-1",
        "title": "Reflected XSS",
        "detail": "XSS in form field",
        "locator": "https://example.com/login",
        "severity_raw": "high",
        "confidence_raw": "high",
    }
    defaults.update(overrides)
    return RawFinding(**defaults)  # type: ignore[arg-type]


def test_passthrough_parser_returns_findings_unchanged() -> None:
    findings = [_raw_finding()]
    output = RawScanOutput(artifacts=[], findings=findings)
    assert PassthroughParser().parse(output) == findings


def test_passthrough_parser_returns_empty_list_when_findings_unset() -> None:
    output = RawScanOutput(artifacts=[ArtifactRef(key="a", size_bytes=1)])
    assert PassthroughParser().parse(output) == []


def test_identity_normalizer_maps_fields_field_for_field() -> None:
    normalized = IdentityNormalizer("fake").normalize(_raw_finding())
    assert normalized.title == "Reflected XSS"
    assert normalized.description == "XSS in form field"
    assert normalized.severity == FindingSeverity.HIGH
    assert normalized.confidence == FindingConfidence.HIGH
    assert normalized.source_tool == "fake"
    assert normalized.rule_id == "rule-1"
    assert normalized.locator == "https://example.com/login"


def test_identity_normalizer_coerces_unrecognized_severity_to_default() -> None:
    normalized = IdentityNormalizer("fake").normalize(
        _raw_finding(severity_raw="not-a-real-severity")
    )
    assert normalized.severity == FindingSeverity.INFORMATIONAL


def test_identity_normalizer_coerces_unrecognized_confidence_to_default() -> None:
    normalized = IdentityNormalizer("fake").normalize(
        _raw_finding(confidence_raw="not-a-real-confidence")
    )
    assert normalized.confidence == FindingConfidence.LOW


def test_identity_normalizer_defaults_confidence_when_none() -> None:
    normalized = IdentityNormalizer("fake").normalize(_raw_finding(confidence_raw=None))
    assert normalized.confidence == FindingConfidence.LOW


def test_identity_normalizer_severity_matching_is_case_and_whitespace_insensitive() -> None:
    normalized = IdentityNormalizer("fake").normalize(_raw_finding(severity_raw="  HIGH  "))
    assert normalized.severity == FindingSeverity.HIGH
