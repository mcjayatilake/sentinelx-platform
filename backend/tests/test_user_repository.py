"""User repository: normalized-email uniqueness."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_repository import UserRepository
from app.schemas.user import UserCreate


async def test_create_and_get_by_email(db_session: AsyncSession) -> None:
    repo = UserRepository(db_session)
    user = await repo.create(UserCreate(email="Alice@Example.com", display_name="Alice"))

    # UserCreate normalizes on assignment; the repository trusts that.
    assert user.email == "alice@example.com"
    fetched = await repo.get_by_email("alice@example.com")
    assert fetched is not None
    assert fetched.id == user.id


async def test_duplicate_normalized_email_rejected(db_session: AsyncSession) -> None:
    repo = UserRepository(db_session)
    await repo.create(UserCreate(email="bob@example.com", display_name="Bob"))

    with pytest.raises(IntegrityError):
        # Different casing/whitespace normalizes to the same stored value.
        await repo.create(UserCreate(email="  BOB@EXAMPLE.COM  ", display_name="Bob Duplicate"))
