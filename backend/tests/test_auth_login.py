"""POST /auth/login."""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.models.audit_event import AuditEvent
from app.models.enums import AuditOutcome, MembershipRole, UserStatus
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.schemas.membership import TenantMembershipCreate
from app.schemas.tenant import TenantCreate
from app.schemas.user import UserUpdate
from tests.auth_helpers import create_tenant_user_membership

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_login_with_single_membership_returns_token_immediately(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, user, _ = await create_tenant_user_membership(
        db_session, email="solo@example.com", password=PASSWORD, tenant_slug="solo-tenant"
    )

    response = await auth_client.post(
        "/api/v1/auth/login", json={"email": "solo@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["token"] is not None
    assert body["available_tenants"] == []
    payload = decode_token(body["token"]["access_token"], expected_type="access")
    assert payload["sub"] == str(user.id)
    assert payload["tenant_id"] == str(tenant.id)
    assert payload["role"] == MembershipRole.OWNER.value


async def test_login_with_multiple_memberships_requires_tenant_selection(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant_a, user, _ = await create_tenant_user_membership(
        db_session, email="multi@example.com", password=PASSWORD, tenant_slug="multi-tenant-a"
    )
    tenant_b = await TenantRepository(db_session).create(
        TenantCreate(name="multi-tenant-b", slug="multi-tenant-b")
    )
    await TenantMembershipRepository(db_session).create(
        TenantMembershipCreate(tenant_id=tenant_b.id, user_id=user.id, role=MembershipRole.VIEWER)
    )

    response = await auth_client.post(
        "/api/v1/auth/login", json={"email": "multi@example.com", "password": PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["token"] is None
    tenant_ids = {t["tenant_id"] for t in body["available_tenants"]}
    assert tenant_ids == {str(tenant_a.id), str(tenant_b.id)}


async def test_login_with_explicit_tenant_id_resolves_directly(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant_a, user, _ = await create_tenant_user_membership(
        db_session, email="pick@example.com", password=PASSWORD, tenant_slug="pick-tenant-a"
    )
    tenant_b = await TenantRepository(db_session).create(
        TenantCreate(name="pick-tenant-b", slug="pick-tenant-b")
    )
    await TenantMembershipRepository(db_session).create(
        TenantMembershipCreate(
            tenant_id=tenant_b.id, user_id=user.id, role=MembershipRole.SECURITY_ANALYST
        )
    )

    response = await auth_client.post(
        "/api/v1/auth/login",
        json={"email": "pick@example.com", "password": PASSWORD, "tenant_id": str(tenant_b.id)},
    )
    assert response.status_code == 200
    body = response.json()
    payload = decode_token(body["token"]["access_token"], expected_type="access")
    assert payload["tenant_id"] == str(tenant_b.id)
    assert payload["role"] == MembershipRole.SECURITY_ANALYST.value
    assert tenant_a.id  # tenant_a exists but was not selected


async def test_login_with_tenant_id_not_a_member_of_is_rejected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="notmine@example.com", password=PASSWORD, tenant_slug="notmine-tenant"
    )
    other_tenant = await TenantRepository(db_session).create(
        TenantCreate(name="other-tenant", slug="other-tenant")
    )
    assert user  # user has no membership in other_tenant

    response = await auth_client.post(
        "/api/v1/auth/login",
        json={
            "email": "notmine@example.com",
            "password": PASSWORD,
            "tenant_id": str(other_tenant.id),
        },
    )
    assert response.status_code == 403


async def test_login_wrong_password_returns_401_and_does_not_echo_it(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert "wrong-password" not in response.text


async def test_login_unknown_email_returns_same_401_as_wrong_password(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="known@example.com", password=PASSWORD, tenant_slug="known-tenant"
    )

    unknown_response = await auth_client.post(
        "/api/v1/auth/login", json={"email": "unknown@example.com", "password": PASSWORD}
    )
    wrong_password_response = await auth_client.post(
        "/api/v1/auth/login", json={"email": "known@example.com", "password": "totally-wrong"}
    )

    assert unknown_response.status_code == wrong_password_response.status_code == 401
    assert unknown_response.json() == wrong_password_response.json()


async def test_login_rejects_inactive_account(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="suspended@example.com", password=PASSWORD, tenant_slug="suspended-t"
    )
    await UserRepository(db_session).update(user, UserUpdate(status=UserStatus.SUSPENDED))

    response = await auth_client.post(
        "/api/v1/auth/login", json={"email": "suspended@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401


async def test_successful_login_writes_audit_event(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, user, _ = await create_tenant_user_membership(
        db_session, email="audited@example.com", password=PASSWORD, tenant_slug="audited-tenant"
    )

    await auth_client.post(
        "/api/v1/auth/login", json={"email": "audited@example.com", "password": PASSWORD}
    )

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == user.id, AuditEvent.action == "auth.login"
            )
        )
    ).scalar_one()
    assert event.outcome == AuditOutcome.SUCCESS
    assert event.tenant_id == tenant.id


async def test_failed_login_writes_audit_event_with_failure_outcome(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="badpass@example.com", password=PASSWORD, tenant_slug="badpass-tenant"
    )

    await auth_client.post(
        "/api/v1/auth/login", json={"email": "badpass@example.com", "password": "nope-nope-nope"}
    )

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == user.id,
                AuditEvent.action == "auth.login",
                AuditEvent.outcome == AuditOutcome.FAILURE,
            )
        )
    ).scalar_one()
    assert event.outcome == AuditOutcome.FAILURE
