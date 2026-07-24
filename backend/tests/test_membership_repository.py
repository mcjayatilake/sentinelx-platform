"""TenantMembership repository: unique pair, tenant-scoped access."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MembershipRole
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.schemas.membership import TenantMembershipCreate
from app.schemas.tenant import TenantCreate
from app.schemas.user import UserCreate


async def _make_tenant_and_user(session: AsyncSession, slug: str, email: str):
    tenant = await TenantRepository(session).create(TenantCreate(name=slug, slug=slug))
    user = await UserRepository(session).create(UserCreate(email=email, display_name=email))
    return tenant, user


async def test_create_and_fetch_membership(db_session: AsyncSession) -> None:
    tenant, user = await _make_tenant_and_user(db_session, "acme", "owner@acme.example")
    repo = TenantMembershipRepository(db_session)

    membership = await repo.create(
        TenantMembershipCreate(tenant_id=tenant.id, user_id=user.id, role=MembershipRole.OWNER)
    )

    fetched = await repo.get_by_id(tenant.id, membership.id)
    assert fetched is not None
    assert fetched.role == MembershipRole.OWNER


async def test_duplicate_tenant_user_pair_rejected(db_session: AsyncSession) -> None:
    tenant, user = await _make_tenant_and_user(db_session, "acme", "dupe@acme.example")
    repo = TenantMembershipRepository(db_session)
    await repo.create(
        TenantMembershipCreate(tenant_id=tenant.id, user_id=user.id, role=MembershipRole.VIEWER)
    )

    with pytest.raises(IntegrityError):
        await repo.create(
            TenantMembershipCreate(
                tenant_id=tenant.id, user_id=user.id, role=MembershipRole.DEVELOPER
            )
        )


async def test_cross_tenant_membership_retrieval_returns_none(db_session: AsyncSession) -> None:
    tenant_a, user_a = await _make_tenant_and_user(db_session, "tenant-a", "a@a.example")
    tenant_b, _ = await _make_tenant_and_user(db_session, "tenant-b", "b@b.example")
    repo = TenantMembershipRepository(db_session)

    membership = await repo.create(
        TenantMembershipCreate(tenant_id=tenant_a.id, user_id=user_a.id, role=MembershipRole.OWNER)
    )

    # Same membership ID, wrong tenant_id: must not be visible.
    assert await repo.get_by_id(tenant_b.id, membership.id) is None
    # Correct tenant_id: visible.
    assert (await repo.get_by_id(tenant_a.id, membership.id)) is not None


async def test_list_by_tenant_excludes_other_tenants(db_session: AsyncSession) -> None:
    tenant_a, user_a = await _make_tenant_and_user(db_session, "tenant-a", "a2@a.example")
    tenant_b, user_b = await _make_tenant_and_user(db_session, "tenant-b", "b2@b.example")
    repo = TenantMembershipRepository(db_session)
    await repo.create(
        TenantMembershipCreate(tenant_id=tenant_a.id, user_id=user_a.id, role=MembershipRole.OWNER)
    )
    await repo.create(
        TenantMembershipCreate(tenant_id=tenant_b.id, user_id=user_b.id, role=MembershipRole.OWNER)
    )

    from app.schemas.common import PaginationParams

    page = await repo.list_by_tenant(tenant_a.id, PaginationParams(limit=10, offset=0))
    assert page.total == 1
    assert page.items[0].tenant_id == tenant_a.id
