"""User repository. Not tenant-scoped — User is a global identity."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: UserCreate) -> User:
        user = User(email=data.email, display_name=data.display_name)
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update(self, user: User, data: UserUpdate) -> User:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(user, field, value)
        await self._session.flush()
        return user

    async def set_password(
        self, user: User, hashed_password: str, *, invalidate_sessions: bool
    ) -> User:
        """Set `hashed_password`. `invalidate_sessions=True` bumps
        `token_version`, invalidating every previously-issued access token
        at once — used by change-password and password-reset, not by the
        transparent rehash-on-login upgrade path (which must not log the
        user out of the session they're actively creating)."""
        user.hashed_password = hashed_password
        if invalidate_sessions:
            user.token_version += 1
        await self._session.flush()
        return user

    async def mark_email_verified(self, user: User) -> User:
        user.email_verified_at = datetime.now(UTC)
        await self._session.flush()
        return user

    async def bump_token_version(self, user: User) -> User:
        """Invalidate every previously-issued access token for `user` at
        once ("log out everywhere") without touching the refresh-token
        table or Redis."""
        user.token_version += 1
        await self._session.flush()
        return user
