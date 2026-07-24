"""Tenant repository: unique slug, pagination."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.tenant_repository import TenantRepository
from app.schemas.common import PaginationParams
from app.schemas.tenant import TenantCreate, TenantUpdate


async def test_create_and_get_tenant(db_session: AsyncSession) -> None:
    repo = TenantRepository(db_session)
    tenant = await repo.create(TenantCreate(name="Acme Corp", slug="acme"))

    fetched = await repo.get_by_slug("acme")
    assert fetched is not None
    assert fetched.id == tenant.id
    assert fetched.status.value == "active"


async def test_duplicate_slug_rejected(db_session: AsyncSession) -> None:
    repo = TenantRepository(db_session)
    await repo.create(TenantCreate(name="Acme", slug="acme"))

    with pytest.raises(IntegrityError):
        await repo.create(TenantCreate(name="Acme Again", slug="acme"))


async def test_update_status(db_session: AsyncSession) -> None:
    repo = TenantRepository(db_session)
    tenant = await repo.create(TenantCreate(name="Acme", slug="acme"))

    from app.models.enums import TenantStatus

    updated = await repo.update(tenant, TenantUpdate(status=TenantStatus.SUSPENDED))
    assert updated.status == TenantStatus.SUSPENDED


async def test_list_all_pagination(db_session: AsyncSession) -> None:
    repo = TenantRepository(db_session)
    for i in range(3):
        await repo.create(TenantCreate(name=f"Tenant {i}", slug=f"tenant-{i}"))

    page = await repo.list_all(PaginationParams(limit=2, offset=0))
    assert page.total == 3
    assert len(page.items) == 2

    next_page = await repo.list_all(PaginationParams(limit=2, offset=2))
    assert len(next_page.items) == 1
