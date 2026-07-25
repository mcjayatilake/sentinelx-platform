"""API-key request authentication (`app.api.deps._principal_from_api_key`)
— end-to-end via the real `X-API-Key` header path: authentication,
malformed/unknown/expired/revoked rejection, scope enforcement, tenant
isolation, and secret non-leakage. `APIKeyService.authenticate()` itself
was already unit-tested (Phase 3); this file proves it's actually wired
to a request.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from app.core.log_redaction import redact_log_secrets
from app.core.logging import get_logger
from app.models.enums import APIKeyStatus, AssetType
from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.schemas.api_key import APIKeyIssueRequest, APIKeyMetadataUpdate
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.services.api_key_service import APIKeyService
from tests.auth_helpers import create_tenant_user_membership, login_and_get_headers

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def _issue_key(
    db_session: AsyncSession, tenant_id, *, scopes: list[str], expires_at=None
) -> str:
    issued = await APIKeyService(APIKeyMetadataRepository(db_session)).issue(
        tenant_id=tenant_id,
        user_id=None,
        data=APIKeyIssueRequest(name="test-key", scopes=scopes, expires_at=expires_at),
    )
    return issued.api_key


async def test_valid_api_key_authenticates_and_scope_grants_access(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey1@example.com", password=PASSWORD, tenant_slug="apikey-t1"
    )
    api_key = await _issue_key(db_session, tenant.id, scopes=["scans:view"])

    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": api_key})
    assert response.status_code == 200


async def test_api_key_without_the_required_scope_is_forbidden(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey2@example.com", password=PASSWORD, tenant_slug="apikey-t2"
    )
    # scans:view only — never scans:manage.
    api_key = await _issue_key(db_session, tenant.id, scopes=["scans:view"])

    response = await auth_client.post(
        "/api/v1/scans",
        json={
            "project_id": "00000000-0000-0000-0000-000000000000",
            "asset_id": "00000000-0000-0000-0000-000000000000",
            "scanner_type": "fake",
        },
        headers={"X-API-Key": api_key},
    )
    assert response.status_code == 403


async def test_api_key_with_no_scopes_at_all_is_forbidden_everywhere(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey3@example.com", password=PASSWORD, tenant_slug="apikey-t3"
    )
    api_key = await _issue_key(db_session, tenant.id, scopes=[])

    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": api_key})
    assert response.status_code == 403


async def test_api_key_scoped_to_api_keys_manage_still_cannot_exceed_it(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    """The tenant itself has an OWNER (all-permissions) member, but that
    has no bearing on what an individual key may do — a key with only
    `scans:view` cannot manage other API keys, proving scopes never fall
    back to a role-based grant (see
    `app.core.permissions.principal_has_permission`)."""
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey4@example.com", password=PASSWORD, tenant_slug="apikey-t4"
    )
    api_key = await _issue_key(db_session, tenant.id, scopes=["scans:view"])

    response = await auth_client.get("/api/v1/api-keys", headers={"X-API-Key": api_key})
    assert response.status_code == 403


async def test_malformed_api_key_is_rejected(auth_client: AsyncClient) -> None:
    response = await auth_client.get(
        "/api/v1/scans", headers={"X-API-Key": "not-a-real-key-no-dot"}
    )
    assert response.status_code == 401


async def test_unknown_prefix_api_key_is_rejected(auth_client: AsyncClient) -> None:
    response = await auth_client.get(
        "/api/v1/scans", headers={"X-API-Key": "sx_doesnotexist.somesecret"}
    )
    assert response.status_code == 401


async def test_expired_api_key_is_rejected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey5@example.com", password=PASSWORD, tenant_slug="apikey-t5"
    )
    api_key = await _issue_key(
        db_session,
        tenant.id,
        scopes=["scans:view"],
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )

    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": api_key})
    assert response.status_code == 401


async def test_revoked_api_key_is_rejected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey6@example.com", password=PASSWORD, tenant_slug="apikey-t6"
    )
    api_key = await _issue_key(db_session, tenant.id, scopes=["scans:view"])

    prefix = api_key.split(".", 1)[0]
    record = await APIKeyMetadataRepository(db_session).get_by_prefix(prefix)
    assert record is not None
    await APIKeyService(APIKeyMetadataRepository(db_session)).revoke(record)

    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": api_key})
    assert response.status_code == 401


async def test_disabled_status_api_key_is_rejected(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    """Directly forcing a non-ACTIVE status (distinct from revoke's own
    codepath, which also sets `revoked_at`) — same rejection either way."""
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey7@example.com", password=PASSWORD, tenant_slug="apikey-t7"
    )
    api_key = await _issue_key(db_session, tenant.id, scopes=["scans:view"])

    prefix = api_key.split(".", 1)[0]
    repo = APIKeyMetadataRepository(db_session)
    record = await repo.get_by_prefix(prefix)
    assert record is not None
    await repo.update(record, APIKeyMetadataUpdate(status=APIKeyStatus.EXPIRED))

    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": api_key})
    assert response.status_code == 401


async def test_api_key_only_sees_its_own_tenants_data(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant_a, _, _ = await create_tenant_user_membership(
        db_session, email="apikey8a@example.com", password=PASSWORD, tenant_slug="apikey-t8a"
    )
    tenant_b, _, _ = await create_tenant_user_membership(
        db_session, email="apikey8b@example.com", password=PASSWORD, tenant_slug="apikey-t8b"
    )
    project_b = await ProjectRepository(db_session).create(
        ProjectCreate(tenant_id=tenant_b.id, name="proj-b", slug="proj-b")
    )
    asset_b = await AssetRepository(db_session).create(
        AssetCreate(
            tenant_id=tenant_b.id,
            project_id=project_b.id,
            asset_type=AssetType.WEBSITE,
            name="asset-b",
            locator="https://b.example.com",
        )
    )
    headers_b = await login_and_get_headers(auth_client, "apikey8b@example.com", PASSWORD)
    created = await auth_client.post(
        "/api/v1/scans",
        json={
            "project_id": str(project_b.id),
            "asset_id": str(asset_b.id),
            "scanner_type": "fake",
        },
        headers=headers_b,
    )
    assert created.status_code == 201

    # Tenant A's own key, broad scopes, but must never see tenant B's scan.
    api_key_a = await _issue_key(db_session, tenant_a.id, scopes=["scans:view"])
    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": api_key_a})
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_successful_authentication_bumps_last_used_at(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey9@example.com", password=PASSWORD, tenant_slug="apikey-t9"
    )
    api_key = await _issue_key(db_session, tenant.id, scopes=["scans:view"])
    prefix = api_key.split(".", 1)[0]

    record_before = await APIKeyMetadataRepository(db_session).get_by_prefix(prefix)
    assert record_before is not None
    assert record_before.last_used_at is None

    response = await auth_client.get("/api/v1/scans", headers={"X-API-Key": api_key})
    assert response.status_code == 200

    # The bump happened on auth_client's own request-scoped session (a
    # separate commit — see app.api.deps._principal_from_api_key). This
    # session's identity map still holds record_before from the query
    # above; APIKeyMetadataRepository.get_by_prefix has no
    # populate_existing=True (unlike ScanRepository.get_by_id, which
    # needs it for the same reason), so an explicit expire is needed to
    # force a real re-read rather than silently returning the stale
    # cached object.
    db_session.expire_all()
    record_after = await APIKeyMetadataRepository(db_session).get_by_prefix(prefix)
    assert record_after is not None
    assert record_after.last_used_at is not None


async def test_full_api_key_never_appears_in_the_list_response_or_logs(
    auth_client: AsyncClient, db_session: AsyncSession
) -> None:
    tenant, _, _ = await create_tenant_user_membership(
        db_session, email="apikey10@example.com", password=PASSWORD, tenant_slug="apikey-t10"
    )
    api_key = await _issue_key(db_session, tenant.id, scopes=["scans:view"])
    secret = api_key.split(".", 1)[1]

    headers = await login_and_get_headers(auth_client, "apikey10@example.com", PASSWORD)
    with capture_logs(processors=[redact_log_secrets]) as captured_logs:
        list_response = await auth_client.get("/api/v1/api-keys", headers=headers)
        get_logger("test").info("noting the key under test", api_key_used=api_key)

    assert list_response.status_code == 200
    body_text = list_response.text
    assert secret not in body_text
    assert api_key not in body_text

    for entry in captured_logs:
        assert secret not in str(entry)
