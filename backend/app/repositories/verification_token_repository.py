"""UserVerificationToken repository (email verification + password reset)."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import VerificationTokenPurpose
from app.models.verification_token import UserVerificationToken
from app.schemas.verification_token import VerificationTokenCreate


class VerificationTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: VerificationTokenCreate) -> UserVerificationToken:
        token = UserVerificationToken(
            user_id=data.user_id,
            purpose=data.purpose,
            hashed_token=data.hashed_token,
            expires_at=data.expires_at,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def get_valid_by_hashed_token(
        self, hashed_token: str, purpose: VerificationTokenPurpose
    ) -> UserVerificationToken | None:
        """Returns the token only if it matches `purpose`, hasn't been
        consumed, and hasn't expired — a caller never has to remember to
        re-check those separately."""
        stmt = select(UserVerificationToken).where(
            UserVerificationToken.hashed_token == hashed_token,
            UserVerificationToken.purpose == purpose,
            UserVerificationToken.consumed_at.is_(None),
            UserVerificationToken.expires_at > datetime.now(UTC),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def consume(self, token: UserVerificationToken) -> UserVerificationToken:
        token.consumed_at = datetime.now(UTC)
        await self._session.flush()
        return token

    async def invalidate_pending_for_user(
        self, user_id: uuid.UUID, purpose: VerificationTokenPurpose
    ) -> int:
        """Consume every still-valid token of `purpose` for `user_id` — so
        issuing a new reset/verification link invalidates any older ones,
        and only the most recently issued link ever works."""
        stmt = select(UserVerificationToken).where(
            UserVerificationToken.user_id == user_id,
            UserVerificationToken.purpose == purpose,
            UserVerificationToken.consumed_at.is_(None),
            UserVerificationToken.expires_at > datetime.now(UTC),
        )
        tokens = (await self._session.execute(stmt)).scalars().all()
        now = datetime.now(UTC)
        for token in tokens:
            token.consumed_at = now
        await self._session.flush()
        return len(tokens)
