"""Development-only seed data.

Run manually with `python -m app.db.seed` (or `make seed`). Never invoked
automatically at application startup, and refuses to run when
`ENVIRONMENT=production`. Idempotent — safe to run repeatedly; existing
rows (matched by slug/email) are reused rather than duplicated.

Creates exactly one clearly-labeled tenant/user/membership/project, no
production credentials of any kind.
"""

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.session import engine
from app.models.enums import MembershipRole
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.schemas.membership import TenantMembershipCreate
from app.schemas.project import ProjectCreate
from app.schemas.tenant import TenantCreate
from app.schemas.user import UserCreate

logger = get_logger(__name__)

DEV_TENANT_SLUG = "sentinelx-dev"
DEV_TENANT_NAME = "SentinelX Dev Tenant (seed data)"
DEV_USER_EMAIL = "dev-seed@sentinelx.local"
DEV_USER_DISPLAY_NAME = "SentinelX Dev Seed User"
DEV_PROJECT_SLUG = "sample-project"
DEV_PROJECT_NAME = "Sample Project (seed data)"


async def seed(session: AsyncSession) -> None:
    settings = get_settings()
    if settings.is_production:
        raise SystemExit("Refusing to seed: ENVIRONMENT=production")

    tenant_repo = TenantRepository(session)
    user_repo = UserRepository(session)
    membership_repo = TenantMembershipRepository(session)
    project_repo = ProjectRepository(session)

    tenant = await tenant_repo.get_by_slug(DEV_TENANT_SLUG)
    if tenant is None:
        tenant = await tenant_repo.create(TenantCreate(name=DEV_TENANT_NAME, slug=DEV_TENANT_SLUG))
        logger.info("seed.tenant_created", tenant_id=str(tenant.id))
    else:
        logger.info("seed.tenant_exists", tenant_id=str(tenant.id))

    user = await user_repo.get_by_email(DEV_USER_EMAIL)
    if user is None:
        user = await user_repo.create(
            UserCreate(email=DEV_USER_EMAIL, display_name=DEV_USER_DISPLAY_NAME)
        )
        logger.info("seed.user_created", user_id=str(user.id))
    else:
        logger.info("seed.user_exists", user_id=str(user.id))

    membership = await membership_repo.get_by_tenant_and_user(tenant.id, user.id)
    if membership is None:
        membership = await membership_repo.create(
            TenantMembershipCreate(tenant_id=tenant.id, user_id=user.id, role=MembershipRole.OWNER)
        )
        logger.info("seed.membership_created", membership_id=str(membership.id))
    else:
        logger.info("seed.membership_exists", membership_id=str(membership.id))

    project = await project_repo.get_by_slug(tenant.id, DEV_PROJECT_SLUG)
    if project is None:
        project = await project_repo.create(
            ProjectCreate(tenant_id=tenant.id, name=DEV_PROJECT_NAME, slug=DEV_PROJECT_SLUG)
        )
        logger.info("seed.project_created", project_id=str(project.id))
    else:
        logger.info("seed.project_exists", project_id=str(project.id))

    await session.commit()
    logger.info("seed.completed", tenant_slug=tenant.slug, user_email=user.email)


async def main() -> None:
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_factory() as session:
        await seed(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
