"""FindingReference schemas.

`tenant_id` is intentionally absent from the create schema: the repository
derives it from the parent finding rather than trusting caller input, since
it exists on this table only to support the composite tenant-propagating
foreign key (see app.models.finding_reference).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import FindingReferenceType


class FindingReferenceCreate(BaseModel):
    finding_id: UUID
    reference_type: FindingReferenceType
    value: str


class FindingReferenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    finding_id: UUID
    reference_type: FindingReferenceType
    value: str
    created_at: datetime
    updated_at: datetime
