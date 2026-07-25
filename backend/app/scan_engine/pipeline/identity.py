"""Reference `Parser`/`Normalizer` implementations.

`PassthroughParser` and `IdentityNormalizer` are real, shipped
infrastructure — not test-only stand-ins — the minimal, honest starting
point every future scanner-specific parser/normalizer adapts from. No
scanner-specific logic lives here: `PassthroughParser` expects a caller
that already produced `RawFinding`s (e.g. a scanner plugin that parses
its own tool's output format inline in `collect_results()` and doesn't
need a separate `Parser`), and `IdentityNormalizer` does the minimum
translation from "the scanner's own vocabulary" to SentinelX's common
vocabulary that's possible without knowing anything scanner-specific.
"""

from app.core.logging import get_logger
from app.models.enums import FindingConfidence, FindingSeverity
from app.scan_engine.interfaces import RawScanOutput
from app.scan_engine.pipeline.stages import NormalizedFinding, RawFinding

logger = get_logger(__name__)

_DEFAULT_SEVERITY = FindingSeverity.INFORMATIONAL
_DEFAULT_CONFIDENCE = FindingConfidence.LOW


class PassthroughParser:
    """Returns `raw_output.findings` unchanged (empty list if unset)."""

    def parse(self, raw_output: RawScanOutput) -> list[RawFinding]:
        return list(raw_output.findings or [])


class IdentityNormalizer:
    """Maps a `RawFinding` onto `NormalizedFinding` field-for-field,
    coercing an unrecognized `severity_raw`/`confidence_raw` string to a
    safe default (logged, not raised) rather than failing the whole scan
    over one malformed finding."""

    def __init__(self, source_tool: str) -> None:
        self._source_tool = source_tool

    def normalize(self, raw: RawFinding) -> NormalizedFinding:
        return NormalizedFinding(
            title=raw.title,
            description=raw.detail,
            severity=self._coerce_severity(raw.severity_raw),
            confidence=self._coerce_confidence(raw.confidence_raw),
            source_tool=self._source_tool,
            external_reference=raw.external_reference,
            remediation=raw.remediation,
            rule_id=raw.rule_id,
            locator=raw.locator,
        )

    def _coerce_severity(self, raw_value: str) -> FindingSeverity:
        try:
            return FindingSeverity(raw_value.strip().lower())
        except ValueError:
            logger.warning(
                "scan_engine.unrecognized_severity",
                raw_value=raw_value,
                coerced_to=_DEFAULT_SEVERITY.value,
            )
            return _DEFAULT_SEVERITY

    def _coerce_confidence(self, raw_value: str | None) -> FindingConfidence:
        if raw_value is None:
            return _DEFAULT_CONFIDENCE
        try:
            return FindingConfidence(raw_value.strip().lower())
        except ValueError:
            logger.warning(
                "scan_engine.unrecognized_confidence",
                raw_value=raw_value,
                coerced_to=_DEFAULT_CONFIDENCE.value,
            )
            return _DEFAULT_CONFIDENCE
