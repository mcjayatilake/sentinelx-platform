"""RefreshToken repository.

`get_by_hashed_token` is intentionally not tenant-scoped, for the same
reason as `APIKeyMetadataRepository.get_by_prefix`: the caller (refresh
endpoint) doesn't know which tenant a bearer token belongs to until it's
looked up. Every other method is scoped by `user_id` (a refresh token
always belongs to exactly one user; there is no cross-user listing here).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import RefreshTokenStatus
from app.models.refresh_token import RefreshToken
from app.schemas.refresh_token import RefreshTokenCreate


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: RefreshTokenCreate, family_id: uuid.UUID) -> RefreshToken:
        token = RefreshToken(
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            family_id=family_id,
            hashed_token=data.hashed_token,
            expires_at=data.expires_at,
            created_by_ip=data.created_by_ip,
            user_agent=data.user_agent,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_by_hashed_token(self, hashed_token: str) -> RefreshToken | None:
        stmt = select(RefreshToken).where(RefreshToken.hashed_token == hashed_token)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, user_id: uuid.UUID, token_id: uuid.UUID) -> RefreshToken | None:
        stmt = select(RefreshToken).where(
            RefreshToken.id == token_id, RefreshToken.user_id == user_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def rotate(self, old_token: RefreshToken, data: RefreshTokenCreate) -> RefreshToken:
        """Mark `old_token` ROTATED and create its replacement in the same
        family. Both writes happen in this one call so a caller can never
        create the replacement without marking the old token consumed."""
        new_token = RefreshToken(
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            family_id=old_token.family_id,
            hashed_token=data.hashed_token,
            expires_at=data.expires_at,
            created_by_ip=data.created_by_ip,
            user_agent=data.user_agent,
        )
        self._session.add(new_token)
        await self._session.flush()

        old_token.status = RefreshTokenStatus.ROTATED
        old_token.replaced_by_id = new_token.id
        await self._session.flush()
        return new_token

    async def revoke(self, token: RefreshToken) -> RefreshToken:
        token.status = RefreshTokenStatus.REVOKED
        token.revoked_at = datetime.now(UTC)
        await self._session.flush()
        return token

    async def revoke_family(self, family_id: uuid.UUID) -> int:
        """Revoke every non-revoked token in `family_id`. Used both for
        reuse-detection (a rotated token was replayed — the whole chain is
        compromised) and for "log out this session everywhere"."""
        stmt = select(RefreshToken).where(
            RefreshToken.family_id == family_id,
            RefreshToken.status != RefreshTokenStatus.REVOKED,
        )
        tokens = (await self._session.execute(stmt)).scalars().all()
        now = datetime.now(UTC)
        for token in tokens:
            token.status = RefreshTokenStatus.REVOKED
            token.revoked_at = now
        await self._session.flush()
        return len(tokens)

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        """ "Log out everywhere": revoke every active/rotated token for a user."""
        stmt = select(RefreshToken).where(
            RefreshToken.user_id == user_id,
            RefreshToken.status != RefreshTokenStatus.REVOKED,
        )
        tokens = (await self._session.execute(stmt)).scalars().all()
        now = datetime.now(UTC)
        for token in tokens:
            token.status = RefreshTokenStatus.REVOKED
            token.revoked_at = now
        await self._session.flush()
        return len(tokens)

    async def list_active_sessions(self, user_id: uuid.UUID) -> list[RefreshToken]:
        """One row per family currently in ACTIVE status — i.e. the latest
        token in every rotation chain that hasn't been revoked, which is
        what "active session" means from the user's point of view."""
        stmt = (
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user_id, RefreshToken.status == RefreshTokenStatus.ACTIVE
            )
            .order_by(RefreshToken.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())
