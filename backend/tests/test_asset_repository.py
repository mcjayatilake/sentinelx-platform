"""Asset repository: tenant/project ownership, composite FK propagation."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.tenant import TenantCreate


async def _make_tenant_and_project(session: AsyncSession, slug: str):
    tenant = await TenantRepository(session).create(TenantCreate(name=slug, slug=slug))
    project = await ProjectRepository(session).create(
        ProjectCreate(tenant_id=tenant.id, name=slug, slug=slug)
    )
    return tenant, project


async def test_create_and_get_asset(db_session: AsyncSession) -> None:
    tenant, project = await _make_tenant_and_project(db_session, "acme")
    repo = AssetRepository(db_session)
    asset = await repo.create(
        AssetCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_type=AssetType.REPOSITORY,
            name="backend",
            locator="github.com/acme/backend",
        )
    )

    fetched = await repo.get_by_id(tenant.id, asset.id)
    assert fetched is not None
    assert fetched.asset_type == AssetType.REPOSITORY
    assert fetched.authorization_status.value == "unauthorized"


async def test_asset_project_must_belong_to_same_tenant(db_session: AsyncSession) -> None:
    """The composite FK (tenant_id, project_id) -> projects(tenant_id, id)
    must reject a project_id that belongs to a *different* tenant, even
    though the plain project_id itself is real."""
    _, project_a = await _make_tenant_and_project(db_session, "tenant-a")
    tenant_b, _ = await _make_tenant_and_project(db_session, "tenant-b")
    repo = AssetRepository(db_session)

    with pytest.raises(IntegrityError):
        await repo.create(
            AssetCreate(
                tenant_id=tenant_b.id,  # mismatched: project_a belongs to tenant_a
                project_id=project_a.id,
                asset_type=AssetType.WEBSITE,
                name="mismatched",
                locator="https://example.com",
            )
        )


async def test_cross_tenant_asset_retrieval_returns_none(db_session: AsyncSession) -> None:
    tenant_a, project_a = await _make_tenant_and_project(db_session, "tenant-a2")
    tenant_b, _ = await _make_tenant_and_project(db_session, "tenant-b2")
    repo = AssetRepository(db_session)
    asset = await repo.create(
        AssetCreate(
            tenant_id=tenant_a.id,
            project_id=project_a.id,
            asset_type=AssetType.DOMAIN,
            name="example.com",
            locator="example.com",
        )
    )

    assert await repo.get_by_id(tenant_b.id, asset.id) is None


async def test_asset_requires_existing_project(db_session: AsyncSession) -> None:
    tenant, _ = await _make_tenant_and_project(db_session, "tenant-c")
    repo = AssetRepository(db_session)

    with pytest.raises(IntegrityError):
        await repo.create(
            AssetCreate(
                tenant_id=tenant.id,
                project_id=uuid.uuid4(),  # no such project
                asset_type=AssetType.IP_RANGE,
                name="bogus",
                locator="10.0.0.0/24",
            )
        )
