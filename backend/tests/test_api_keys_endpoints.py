"""API key endpoints: issuance, listing, revocation, rotation, RBAC."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_secret
from app.models.api_key import APIKeyMetadata
from app.models.audit_event import AuditEvent
from app.models.enums import APIKeyStatus, AuditOutcome, MembershipRole
from app.models.membership import TenantMembership
from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.schemas.api_key import APIKeyIssueRequest
from app.services.api_key_service import APIKeyService
from tests.auth_helpers import create_tenant_user_membership, login_and_get_headers

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def _membership_for_user(session: AsyncSession, user_id: uuid.UUID) -> TenantMembership:
    return (
        await session.execute(select(TenantMembership).where(TenantMembership.user_id == user_id))
    ).scalar_one()


async def test_create_api_key_returns_plaintext_key_once(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="keyowner@example.com", password=PASSWORD, tenant_slug="key-tenant"
    )
    headers = await login_and_get_headers(auth_client, "keyowner@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/api-keys", json={"name": "CI key", "scopes": ["scans:read"]}, headers=headers
    )
    assert response.status_code == 201
    body = response.json()
    assert "." in body["api_key"]
    assert body["prefix"] in body["api_key"]


async def test_created_key_is_never_stored_in_plaintext(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="nostore@example.com", password=PASSWORD, tenant_slug="nostore-t"
    )
    headers = await login_and_get_headers(auth_client, "nostore@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/api-keys", json={"name": "leak check"}, headers=headers
    )
    body = response.json()

    record = (
        await db_session.execute(select(APIKeyMetadata).where(APIKeyMetadata.id == body["id"]))
    ).scalar_one()
    assert record.hashed_secret == hash_secret(body["api_key"].split(".", 1)[1])
    assert record.hashed_secret != body["api_key"]


async def test_list_api_keys_never_includes_plaintext_or_hash(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="lister@example.com", password=PASSWORD, tenant_slug="lister-tenant"
    )
    headers = await login_and_get_headers(auth_client, "lister@example.com", PASSWORD)
    await auth_client.post("/api/v1/api-keys", json={"name": "listed key"}, headers=headers)

    response = await auth_client.get("/api/v1/api-keys", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert "hashed_secret" not in body["items"][0]
    assert "api_key" not in body["items"][0]


async def test_revoke_api_key_marks_it_revoked_and_audits(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="revoker@example.com", password=PASSWORD, tenant_slug="revoker-t"
    )
    headers = await login_and_get_headers(auth_client, "revoker@example.com", PASSWORD)
    created = (
        await auth_client.post("/api/v1/api-keys", json={"name": "to revoke"}, headers=headers)
    ).json()

    response = await auth_client.post(f"/api/v1/api-keys/{created['id']}/revoke", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == APIKeyStatus.REVOKED.value

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.actor_user_id == user.id, AuditEvent.action == "api_key.revoke"
            )
        )
    ).scalar_one()
    assert event.outcome == AuditOutcome.SUCCESS


async def test_rotate_api_key_revokes_old_and_issues_new(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session, email="rotator@example.com", password=PASSWORD, tenant_slug="rotator-t"
    )
    headers = await login_and_get_headers(auth_client, "rotator@example.com", PASSWORD)
    created = (
        await auth_client.post("/api/v1/api-keys", json={"name": "rotate-me"}, headers=headers)
    ).json()

    rotated_response = await auth_client.post(
        f"/api/v1/api-keys/{created['id']}/rotate", headers=headers
    )
    assert rotated_response.status_code == 200
    rotated = rotated_response.json()
    assert rotated["id"] != created["id"]
    assert rotated["api_key"] != created["api_key"]

    old_record = await db_session.get(APIKeyMetadata, created["id"])
    assert old_record is not None
    assert old_record.status == APIKeyStatus.REVOKED


async def test_api_key_view_only_role_cannot_create(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="viewer-key@example.com",
        password=PASSWORD,
        tenant_slug="viewer-key-t",
        role=MembershipRole.VIEWER,
    )
    headers = await login_and_get_headers(auth_client, "viewer-key@example.com", PASSWORD)

    response = await auth_client.post(
        "/api/v1/api-keys", json={"name": "should fail"}, headers=headers
    )
    assert response.status_code == 403


async def test_developer_role_can_view_but_not_manage_api_keys(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="dev-key@example.com",
        password=PASSWORD,
        tenant_slug="dev-key-tenant",
        role=MembershipRole.DEVELOPER,
    )
    headers = await login_and_get_headers(auth_client, "dev-key@example.com", PASSWORD)

    list_response = await auth_client.get("/api/v1/api-keys", headers=headers)
    assert list_response.status_code == 200

    create_response = await auth_client.post(
        "/api/v1/api-keys", json={"name": "should fail"}, headers=headers
    )
    assert create_response.status_code == 403


async def test_authenticate_service_rejects_revoked_key(db_session: AsyncSession) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="svcauth@example.com", password=PASSWORD, tenant_slug="svcauth-tenant"
    )
    membership = await _membership_for_user(db_session, user.id)

    service = APIKeyService(APIKeyMetadataRepository(db_session))
    issued = await service.issue(
        tenant_id=membership.tenant_id, user_id=user.id, data=APIKeyIssueRequest(name="svc-key")
    )
    record = await APIKeyMetadataRepository(db_session).get_by_id(membership.tenant_id, issued.id)
    assert record is not None
    await service.revoke(record)

    assert await service.authenticate(issued.api_key) is None


async def test_authenticate_service_accepts_valid_key_and_updates_last_used(
    db_session: AsyncSession,
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="validkey@example.com", password=PASSWORD, tenant_slug="validkey-t"
    )
    membership = await _membership_for_user(db_session, user.id)

    service = APIKeyService(APIKeyMetadataRepository(db_session))
    issued = await service.issue(
        tenant_id=membership.tenant_id, user_id=user.id, data=APIKeyIssueRequest(name="valid-key")
    )

    authenticated = await service.authenticate(issued.api_key)
    assert authenticated is not None
    assert authenticated.last_used_at is not None


async def test_authenticate_service_rejects_malformed_key(db_session: AsyncSession) -> None:
    service = APIKeyService(APIKeyMetadataRepository(db_session))
    assert await service.authenticate("not-a-valid-key-format") is None


async def test_authenticate_service_rejects_wrong_secret_for_a_real_prefix(
    db_session: AsyncSession,
) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="wrongsecret@example.com", password=PASSWORD, tenant_slug="wrongsec-t"
    )
    membership = await _membership_for_user(db_session, user.id)
    service = APIKeyService(APIKeyMetadataRepository(db_session))
    issued = await service.issue(
        tenant_id=membership.tenant_id, user_id=user.id, data=APIKeyIssueRequest(name="wrong-key")
    )

    prefix = issued.api_key.split(".", 1)[0]
    assert await service.authenticate(f"{prefix}.completely-wrong-secret") is None


async def test_authenticate_service_rejects_expired_key(db_session: AsyncSession) -> None:
    _, user, _ = await create_tenant_user_membership(
        db_session, email="expiredkey@example.com", password=PASSWORD, tenant_slug="expiredkey-t"
    )
    membership = await _membership_for_user(db_session, user.id)
    service = APIKeyService(APIKeyMetadataRepository(db_session))
    issued = await service.issue(
        tenant_id=membership.tenant_id,
        user_id=user.id,
        data=APIKeyIssueRequest(
            name="expiring", expires_at=datetime.now(UTC) - timedelta(minutes=1)
        ),
    )

    assert await service.authenticate(issued.api_key) is None
