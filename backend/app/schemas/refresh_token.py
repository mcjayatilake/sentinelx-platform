"""RefreshToken schemas.

Internal to the auth service — there is no public API schema that exposes
a refresh token row directly (session listing uses `SessionRead` in
`app.schemas.auth`, a deliberately smaller, public-safe shape).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class RefreshTokenCreate(BaseModel):
    tenant_id: UUID
    user_id: UUID
    family_id: UUID
    hashed_token: str
    expires_at: datetime
    created_by_ip: str | None = None
    user_agent: str | None = None
