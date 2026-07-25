"""Fingerprint computation for finding deduplication.

`docs/data-model.md`'s "Finding deduplication" section documents that
computing the fingerprint is "the scan engine's responsibility, not
persisted [in the Finding model] here" — this is that responsibility.
Persistence of the dedup policy itself (new-fingerprint-inserts,
seen-fingerprint-updates-last-seen) already exists and is reused, not
reimplemented — see `app.repositories.finding_repository.FindingRepository.record_detection`.
"""

import hashlib

# ASCII Unit Separator: an unambiguous joiner between fingerprint
# components, so e.g. scanner_type="a", rule_id="b|c" can never collide
# with scanner_type="a|b", rule_id="c" the way a plain "|" join could.
_SEPARATOR = "\x1f"


def compute_fingerprint(scanner_type: str, rule_id: str, locator: str, detail: str) -> str:
    """A deterministic SHA-256 hex digest of a finding's stable identity.
    The same `(scanner_type, rule_id, locator, detail)` always produces
    the same fingerprint; any difference produces a different one."""
    payload = _SEPARATOR.join([scanner_type, rule_id, locator, detail])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
