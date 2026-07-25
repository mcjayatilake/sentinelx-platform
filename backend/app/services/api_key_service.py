"""API key issuance, authentication, rotation, and revocation.

Never stores or logs a plaintext key. `issue` and `rotate` are the only
methods that ever see one, and both return it exactly once in their
result — no method here (or anywhere else) can retrieve it again.
"""

import hmac
import uuid
from datetime import UTC, datetime

from app.core.security import generate_secret, hash_secret
from app.models.api_key import APIKeyMetadata
from app.models.enums import APIKeyStatus
from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.schemas.api_key import (
    APIKeyIssueRequest,
    APIKeyIssueResponse,
    APIKeyMetadataCreate,
    APIKeyMetadataUpdate,
)

_PREFIX_LABEL = "sx"


class APIKeyService:
    def __init__(self, repository: APIKeyMetadataRepository) -> None:
        self._repository = repository

    async def issue(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID | None, data: APIKeyIssueRequest
    ) -> APIKeyIssueResponse:
        # `prefix` is the public lookup identifier (stored, unique, safe to
        # display); `secret` is the actual bearer credential and is never
        # persisted in plaintext — only `hash_secret(secret)` is stored.
        prefix = f"{_PREFIX_LABEL}_{generate_secret(6)}"
        secret = generate_secret(32)
        plaintext_key = f"{prefix}.{secret}"

        record = await self._repository.create(
            APIKeyMetadataCreate(
                tenant_id=tenant_id,
                user_id=user_id,
                name=data.name,
                prefix=prefix,
                hashed_secret=hash_secret(secret),
                scopes=data.scopes,
                expires_at=data.expires_at,
            )
        )
        return APIKeyIssueResponse(
            id=record.id,
            name=record.name,
            prefix=record.prefix,
            api_key=plaintext_key,
            scopes=record.scopes,
            expires_at=record.expires_at,
            created_at=record.created_at,
        )

    async def authenticate(self, plaintext_key: str) -> APIKeyMetadata | None:
        """Verify a raw `prefix.secret` key and, on success, bump
        `last_used_at`. Returns `None` for every failure mode (malformed
        key, unknown prefix, wrong secret, revoked, expired) without
        distinguishing which, so a caller can't use the response to probe
        for valid prefixes."""
        try:
            prefix, secret = plaintext_key.split(".", 1)
        except ValueError:
            return None

        record = await self._repository.get_by_prefix(prefix)
        if record is None:
            return None
        if record.status != APIKeyStatus.ACTIVE:
            return None
        if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
            return None
        if not hmac.compare_digest(record.hashed_secret, hash_secret(secret)):
            return None

        return await self._repository.update(
            record, APIKeyMetadataUpdate(last_used_at=datetime.now(UTC))
        )

    async def revoke(self, record: APIKeyMetadata) -> APIKeyMetadata:
        return await self._repository.update(
            record,
            APIKeyMetadataUpdate(status=APIKeyStatus.REVOKED, revoked_at=datetime.now(UTC)),
        )

    async def rotate(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID | None, record: APIKeyMetadata
    ) -> APIKeyIssueResponse:
        """Revoke `record` and issue a fresh key with the same name, scopes,
        and expiry. The old key stops authenticating immediately."""
        await self.revoke(record)
        return await self.issue(
            tenant_id=tenant_id,
            user_id=user_id,
            data=APIKeyIssueRequest(
                name=record.name, scopes=record.scopes, expires_at=record.expires_at
            ),
        )
