"""Pipeline stage shapes: the data a `Parser` produces and a `Normalizer`
consumes/produces.

No scanner-specific logic here — a real scanner-specific parser/normalizer
pair lives alongside its plugin (e.g. a future
`app/modules/website_scanning/zap_parser.py`) and imports these types,
never the other way around.
"""

from dataclasses import dataclass
from typing import Protocol

from app.models.enums import FindingConfidence, FindingSeverity
from app.scan_engine.interfaces import RawScanOutput


@dataclass(frozen=True, slots=True)
class RawFinding:
    """One finding as a parser extracted it from raw scanner output —
    still in the scanner's own vocabulary (raw severity/confidence
    strings), not yet SentinelX's normalized shape."""

    rule_id: str
    title: str
    detail: str
    locator: str
    severity_raw: str
    confidence_raw: str | None = None
    remediation: str | None = None
    external_reference: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedFinding:
    """A finding translated into SentinelX's common vocabulary — the
    shape `app.scan_engine.pipeline.pipeline.FindingPipeline` hands to
    `FindingRepository.record_detection()` (after fingerprinting)."""

    title: str
    description: str | None
    severity: FindingSeverity
    confidence: FindingConfidence
    source_tool: str
    external_reference: str | None
    remediation: str | None
    rule_id: str
    locator: str


class Parser(Protocol):
    def parse(self, raw_output: RawScanOutput) -> list[RawFinding]: ...


class Normalizer(Protocol):
    def normalize(self, raw: RawFinding) -> NormalizedFinding: ...
