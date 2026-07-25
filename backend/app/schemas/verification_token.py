"""UserVerificationToken schemas. Internal to the auth service."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import VerificationTokenPurpose


class VerificationTokenCreate(BaseModel):
    user_id: UUID
    purpose: VerificationTokenPurpose
    hashed_token: str
    expires_at: datetime
