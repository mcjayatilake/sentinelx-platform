"""Tenant membership endpoints: list, add-by-email, role change, remove."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.enums import AuditOutcome, MembershipRole, MembershipStatus
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.user_repository import UserRepository
from app.schemas.membership import TenantMembershipCreate
from app.schemas.user import UserCreate
from tests.auth_helpers import create_tenant_user_membership, login_and_get_headers

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_list_memberships_visible_to_viewer(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="viewer-list@example.com",
        password=PASSWORD,
        tenant_slug="viewer-list-t",
        role=MembershipRole.VIEWER,
    )
    headers = await login_and_get_headers(auth_client, "viewer-list@example.com", PASSWORD)

    response = await auth_client.get("/api/v1/memberships", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 1


async def test_add_member_by_email_for_existing_user(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, owner, _ = await create_tenant_user_membership(
        db_session, email="owner@example.com", password=PASSWORD, tenant_slug="add-member-t"
    )
    new_user = await UserRepository(db_session).create(
        UserCreate(email="newmember@example.com", display_name="New Member")
    )
    headers = await login_and_get_headers(auth_client, "owner@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/memberships",
        json={"email": "newmember@example.com", "role": MembershipRole.VIEWER.value},
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["user_id"] == str(new_user.id)
    assert body["role"] == MembershipRole.VIEWER.value
    assert tenant.id and owner.id


async def test_add_member_with_unknown_email_returns_404(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="owner2@example.com", password=PASSWORD, tenant_slug="unknown-email-t"
    )
    headers = await login_and_get_headers(auth_client, "owner2@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/memberships",
        json={"email": "ghost@example.com", "role": MembershipRole.VIEWER.value},
        headers=headers,
    )
    assert response.status_code == 404


async def test_add_member_who_is_already_a_member_returns_409(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="owner3@example.com", password=PASSWORD, tenant_slug="dupe-member-t"
    )
    headers = await login_and_get_headers(auth_client, "owner3@example.com", PASSWORD)

    # Owner is already a member of their own tenant — re-adding by their
    # own email is the simplest way to hit the "already a member" path.
    response = await auth_client.post(
        "/api/v1/memberships",
        json={"email": "owner3@example.com", "role": MembershipRole.VIEWER.value},
        headers=headers,
    )
    assert response.status_code == 409


async def test_viewer_cannot_add_members(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="viewer-add@example.com",
        password=PASSWORD,
        tenant_slug="viewer-add-t",
        role=MembershipRole.VIEWER,
    )
    await UserRepository(db_session).create(
        UserCreate(email="blocked@example.com", display_name="Blocked")
    )
    headers = await login_and_get_headers(auth_client, "viewer-add@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/memberships",
        json={"email": "blocked@example.com", "role": MembershipRole.VIEWER.value},
        headers=headers,
    )
    assert response.status_code == 403


async def test_role_change_writes_audit_event(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, owner, _ = await create_tenant_user_membership(
        db_session, email="promoter@example.com", password=PASSWORD, tenant_slug="promote-t"
    )
    member_user = await UserRepository(db_session).create(
        UserCreate(email="promotee@example.com", display_name="Promotee")
    )
    membership = await TenantMembershipRepository(db_session).create(
        TenantMembershipCreate(
            tenant_id=tenant.id, user_id=member_user.id, role=MembershipRole.VIEWER
        )
    )
    headers = await login_and_get_headers(auth_client, "promoter@example.com", PASSWORD)

    response = await auth_client.patch(
        f"/api/v1/memberships/{membership.id}",
        json={"role": MembershipRole.ADMINISTRATOR.value},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["role"] == MembershipRole.ADMINISTRATOR.value

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == owner.id,
                AuditEvent.action == "membership.role_change",
            )
        )
    ).scalar_one()
    assert event.outcome == AuditOutcome.SUCCESS
    assert event.event_metadata is not None
    assert event.event_metadata["old_role"] == MembershipRole.VIEWER.value
    assert event.event_metadata["new_role"] == MembershipRole.ADMINISTRATOR.value


async def test_remove_member_sets_status_removed(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, owner, _ = await create_tenant_user_membership(
        db_session, email="remover@example.com", password=PASSWORD, tenant_slug="remove-t"
    )
    member_user = await UserRepository(db_session).create(
        UserCreate(email="removeme@example.com", display_name="Remove Me")
    )
    membership = await TenantMembershipRepository(db_session).create(
        TenantMembershipCreate(
            tenant_id=tenant.id, user_id=member_user.id, role=MembershipRole.VIEWER
        )
    )
    headers = await login_and_get_headers(auth_client, "remover@example.com", PASSWORD)

    response = await auth_client.delete(f"/api/v1/memberships/{membership.id}", headers=headers)
    assert response.status_code == 204

    await db_session.refresh(membership)
    assert membership.status == MembershipStatus.REMOVED
    assert owner.id


async def test_removed_member_can_be_re_added(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, owner, _ = await create_tenant_user_membership(
        db_session, email="readder@example.com", password=PASSWORD, tenant_slug="readd-t"
    )
    member_user = await UserRepository(db_session).create(
        UserCreate(email="readded@example.com", display_name="Re-added")
    )
    membership = await TenantMembershipRepository(db_session).create(
        TenantMembershipCreate(
            tenant_id=tenant.id, user_id=member_user.id, role=MembershipRole.VIEWER
        )
    )
    headers = await login_and_get_headers(auth_client, "readder@example.com", PASSWORD)
    await auth_client.delete(f"/api/v1/memberships/{membership.id}", headers=headers)

    response = await auth_client.post(
        "/api/v1/memberships",
        json={"email": "readded@example.com", "role": MembershipRole.SECURITY_ANALYST.value},
        headers=headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == str(membership.id)  # reactivated, not duplicated
    assert body["status"] == MembershipStatus.ACTIVE.value
    assert body["role"] == MembershipRole.SECURITY_ANALYST.value
    assert owner.id
