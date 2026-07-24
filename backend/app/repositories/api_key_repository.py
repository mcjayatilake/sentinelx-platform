"""APIKeyMetadata repository — tenant-scoped, except lookup-by-prefix.

`get_by_prefix` is intentionally not tenant-scoped: a key's prefix is how a
future auth layer would locate the record *before* it knows which tenant is
calling (the same reasoning as `UserRepository.get_by_email`). Every other
method requires `tenant_id`.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import APIKeyMetadata
from app.repositories.base import Page
from app.schemas.api_key import APIKeyMetadataCreate, APIKeyMetadataUpdate
from app.schemas.common import PaginationParams


class APIKeyMetadataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: APIKeyMetadataCreate) -> APIKeyMetadata:
        api_key = APIKeyMetadata(
            tenant_id=data.tenant_id,
            user_id=data.user_id,
            name=data.name,
            prefix=data.prefix,
            hashed_secret=data.hashed_secret,
            scopes=data.scopes,
            expires_at=data.expires_at,
        )
        self._session.add(api_key)
        await self._session.flush()
        return api_key

    async def get_by_id(self, tenant_id: uuid.UUID, api_key_id: uuid.UUID) -> APIKeyMetadata | None:
        stmt = select(APIKeyMetadata).where(
            APIKeyMetadata.id == api_key_id, APIKeyMetadata.tenant_id == tenant_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_prefix(self, prefix: str) -> APIKeyMetadata | None:
        stmt = select(APIKeyMetadata).where(APIKeyMetadata.prefix == prefix)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_tenant(
        self, tenant_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[APIKeyMetadata]:
        base_filter = APIKeyMetadata.tenant_id == tenant_id
        total = (
            await self._session.execute(
                select(func.count()).select_from(APIKeyMetadata).where(base_filter)
            )
        ).scalar_one()
        stmt = (
            select(APIKeyMetadata)
            .where(base_filter)
            .order_by(APIKeyMetadata.created_at)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)

    async def update(self, api_key: APIKeyMetadata, data: APIKeyMetadataUpdate) -> APIKeyMetadata:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(api_key, field, value)
        await self._session.flush()
        return api_key
