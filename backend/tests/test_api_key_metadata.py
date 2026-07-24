"""APIKeyMetadata: hashed_secret never exposed via the read schema."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.api_key import APIKeyMetadataCreate, APIKeyMetadataRead
from app.schemas.tenant import TenantCreate


async def test_read_schema_excludes_hashed_secret(db_session: AsyncSession) -> None:
    tenant = await TenantRepository(db_session).create(TenantCreate(name="Acme", slug="acme"))
    repo = APIKeyMetadataRepository(db_session)
    api_key = await repo.create(
        APIKeyMetadataCreate(
            tenant_id=tenant.id,
            name="CI key",
            prefix="sx_live_abcd1234",
            hashed_secret="sha256:deadbeef",
            scopes=["scans:read"],
        )
    )

    assert "hashed_secret" not in APIKeyMetadataRead.model_fields

    read = APIKeyMetadataRead.model_validate(api_key)
    dumped = read.model_dump()
    assert "hashed_secret" not in dumped
    assert dumped["prefix"] == "sx_live_abcd1234"


async def test_duplicate_prefix_rejected(db_session: AsyncSession) -> None:
    tenant = await TenantRepository(db_session).create(TenantCreate(name="Acme", slug="acme2"))
    repo = APIKeyMetadataRepository(db_session)
    await repo.create(
        APIKeyMetadataCreate(
            tenant_id=tenant.id, name="Key 1", prefix="sx_live_dupe", hashed_secret="hash-1"
        )
    )

    with pytest.raises(IntegrityError):
        await repo.create(
            APIKeyMetadataCreate(
                tenant_id=tenant.id, name="Key 2", prefix="sx_live_dupe", hashed_secret="hash-2"
            )
        )


async def test_get_by_prefix(db_session: AsyncSession) -> None:
    tenant = await TenantRepository(db_session).create(TenantCreate(name="Acme", slug="acme3"))
    repo = APIKeyMetadataRepository(db_session)
    created = await repo.create(
        APIKeyMetadataCreate(
            tenant_id=tenant.id, name="Key", prefix="sx_live_lookup", hashed_secret="hash"
        )
    )

    found = await repo.get_by_prefix("sx_live_lookup")
    assert found is not None
    assert found.id == created.id
