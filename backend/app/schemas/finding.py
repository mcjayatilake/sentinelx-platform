"""Finding schemas."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import FindingConfidence, FindingSeverity, FindingStatus


class FindingCreate(BaseModel):
    tenant_id: UUID
    project_id: UUID
    asset_id: UUID
    scan_id: UUID
    title: str
    description: str | None = None
    severity: FindingSeverity
    confidence: FindingConfidence = FindingConfidence.MEDIUM
    source_tool: str
    external_reference: str | None = None
    fingerprint: str
    remediation: str | None = None


class FindingUpdate(BaseModel):
    status: FindingStatus | None = None
    severity: FindingSeverity | None = None
    confidence: FindingConfidence | None = None
    remediation: str | None = None
    resolved_at: datetime | None = None


class FindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    project_id: UUID
    asset_id: UUID
    scan_id: UUID
    title: str
    description: str | None
    severity: FindingSeverity
    status: FindingStatus
    confidence: FindingConfidence
    source_tool: str
    external_reference: str | None
    fingerprint: str
    remediation: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
