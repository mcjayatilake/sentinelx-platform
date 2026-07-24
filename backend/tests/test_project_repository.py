"""Project repository: slug uniqueness scoped to tenant."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.project_repository import ProjectRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.project import ProjectCreate
from app.schemas.tenant import TenantCreate


async def test_duplicate_slug_within_tenant_rejected(db_session: AsyncSession) -> None:
    tenant = await TenantRepository(db_session).create(TenantCreate(name="Acme", slug="acme"))
    repo = ProjectRepository(db_session)
    await repo.create(ProjectCreate(tenant_id=tenant.id, name="Web App", slug="web-app"))

    with pytest.raises(IntegrityError):
        await repo.create(ProjectCreate(tenant_id=tenant.id, name="Web App 2", slug="web-app"))


async def test_same_slug_allowed_across_different_tenants(db_session: AsyncSession) -> None:
    tenant_repo = TenantRepository(db_session)
    tenant_a = await tenant_repo.create(TenantCreate(name="A", slug="tenant-a"))
    tenant_b = await tenant_repo.create(TenantCreate(name="B", slug="tenant-b"))
    repo = ProjectRepository(db_session)

    project_a = await repo.create(ProjectCreate(tenant_id=tenant_a.id, name="Web", slug="web"))
    project_b = await repo.create(ProjectCreate(tenant_id=tenant_b.id, name="Web", slug="web"))

    assert project_a.id != project_b.id
    assert project_a.slug == project_b.slug == "web"


async def test_cross_tenant_project_retrieval_returns_none(db_session: AsyncSession) -> None:
    tenant_repo = TenantRepository(db_session)
    tenant_a = await tenant_repo.create(TenantCreate(name="A", slug="tenant-a2"))
    tenant_b = await tenant_repo.create(TenantCreate(name="B", slug="tenant-b2"))
    repo = ProjectRepository(db_session)
    project = await repo.create(ProjectCreate(tenant_id=tenant_a.id, name="Web", slug="web"))

    assert await repo.get_by_id(tenant_b.id, project.id) is None
    assert await repo.get_by_slug(tenant_b.id, "web") is None
