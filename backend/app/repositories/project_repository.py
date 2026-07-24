"""Project repository — tenant-scoped."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.repositories.base import Page
from app.schemas.common import PaginationParams
from app.schemas.project import ProjectCreate, ProjectUpdate


class ProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, data: ProjectCreate) -> Project:
        project = Project(
            tenant_id=data.tenant_id,
            name=data.name,
            slug=data.slug,
            description=data.description,
        )
        self._session.add(project)
        await self._session.flush()
        return project

    async def get_by_id(self, tenant_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
        stmt = select(Project).where(Project.id == project_id, Project.tenant_id == tenant_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_slug(self, tenant_id: uuid.UUID, slug: str) -> Project | None:
        stmt = select(Project).where(Project.tenant_id == tenant_id, Project.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_by_tenant(
        self, tenant_id: uuid.UUID, pagination: PaginationParams
    ) -> Page[Project]:
        total_stmt = select(func.count()).select_from(Project).where(Project.tenant_id == tenant_id)
        total = (await self._session.execute(total_stmt)).scalar_one()
        stmt = (
            select(Project)
            .where(Project.tenant_id == tenant_id)
            .order_by(Project.created_at)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        items = list((await self._session.execute(stmt)).scalars().all())
        return Page(items=items, total=total, limit=pagination.limit, offset=pagination.offset)

    async def update(self, project: Project, data: ProjectUpdate) -> Project:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(project, field, value)
        await self._session.flush()
        return project
