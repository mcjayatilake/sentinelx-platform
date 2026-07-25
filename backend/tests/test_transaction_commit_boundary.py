"""`app.db.session.get_db()`'s request transaction policy — commit on
success, rollback on exception — proven against the REAL production
dependency path (the plain `client` fixture, which does NOT override
`get_db_session`, so every request here opens its own genuine connection
against `settings.database_url`, exactly like a real deployment), verified
from a **separate, independent session** (`real_session_factory`) rather
than the same session that made the request.

This is the regression suite for the bug the technical review found:
`get_db()` previously never called `commit()` at all, so a successful
mutating request's writes were silently discarded once the connection
returned to the pool — invisible to this exact kind of separate-session
check, which is precisely why the old test suite (sharing one uncommitted
session across requests) never caught it.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import hash_password
from app.models.asset import Asset
from app.models.enums import AssetType, MembershipRole, ScanStatus
from app.models.membership import TenantMembership
from app.models.project import Project
from app.models.refresh_token import RefreshToken
from app.models.scan import Scan
from app.models.tenant import Tenant
from app.models.user import User
from app.models.verification_token import UserVerificationToken
from app.repositories.asset_repository import AssetRepository
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.schemas.asset import AssetCreate
from app.schemas.membership import TenantMembershipCreate
from app.schemas.project import ProjectCreate
from app.schemas.tenant import TenantCreate
from app.schemas.user import UserCreate
from tests.auth_helpers import bearer

PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def _delete_user_and_dependents(session: AsyncSession, user_id: uuid.UUID) -> None:
    """`RefreshToken`/`UserVerificationToken` RESTRICT their `user_id` FK,
    so they must go before the `User` row itself."""
    await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
    await session.execute(
        delete(UserVerificationToken).where(UserVerificationToken.user_id == user_id)
    )
    await session.execute(delete(User).where(User.id == user_id))


async def test_committed_registration_is_visible_from_a_separate_session(
    client: AsyncClient, real_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    email = f"commit-visible-{uuid.uuid4().hex[:8]}@example.com"

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": "Commit Visible"},
    )
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["user"]["id"])

    try:
        async with real_session_factory() as verify_session:
            found = await UserRepository(verify_session).get_by_id(user_id)
            assert found is not None
            assert found.email == email
    finally:
        async with real_session_factory() as cleanup_session:
            await _delete_user_and_dependents(cleanup_session, user_id)
            await cleanup_session.commit()


async def test_failed_request_rolls_back_and_leaves_nothing_visible(
    client: AsyncClient, real_session_factory: async_sessionmaker[AsyncSession], monkeypatch
) -> None:
    """`AuthService.register()` writes the `User` row (and sets its
    password) *before* sending the verification email. Forcing that send
    to raise, deep inside the same request, proves a downstream failure
    unwinds everything already written in that request — not just that
    nothing was ever attempted."""
    email = f"commit-rollback-{uuid.uuid4().hex[:8]}@example.com"

    async def _boom(self: object, to: str, subject: str, body: str) -> None:
        raise RuntimeError("simulated email provider outage")

    monkeypatch.setattr("app.services.email_service.LoggingEmailSender.send", _boom)

    # httpx's ASGITransport re-raises an unhandled server exception into
    # the caller by default (it doesn't have Starlette's production
    # ServerErrorMiddleware in front of it here) rather than returning a
    # generic 500 response — the point of this test is the rollback that
    # happens on the way out, not the exact client-visible shape of an
    # unhandled crash.
    with pytest.raises(RuntimeError, match="simulated email provider outage"):
        await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": PASSWORD, "display_name": "Commit Rollback"},
        )

    async with real_session_factory() as verify_session:
        found = await UserRepository(verify_session).get_by_email(email)
        assert found is None


async def test_read_only_request_does_not_error(client: AsyncClient) -> None:
    # A plain read-only request must still complete cleanly under the
    # unconditional commit-on-success policy (an empty commit is a no-op,
    # not an error).
    response = await client.get("/api/v1/health")
    assert response.status_code == 200


async def test_scan_created_via_api_is_visible_before_any_worker_runs(
    client: AsyncClient, real_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The original review scenario, end to end through the real
    per-request session: `201 Created` must mean the `Scan` row already
    exists for a completely independent reader — not just for the
    request's own (by-then-closed) session."""
    slug = f"txn-scan-{uuid.uuid4().hex[:8]}"
    email = f"{slug}@example.com"
    tenant_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    scan_id: uuid.UUID | None = None

    try:
        async with real_session_factory() as setup_session:
            tenant = await TenantRepository(setup_session).create(
                TenantCreate(name=slug, slug=slug)
            )
            user = await UserRepository(setup_session).create(
                UserCreate(email=email, display_name=slug)
            )
            await UserRepository(setup_session).set_password(
                user, hash_password(PASSWORD), invalidate_sessions=False
            )
            await TenantMembershipRepository(setup_session).create(
                TenantMembershipCreate(
                    tenant_id=tenant.id, user_id=user.id, role=MembershipRole.OWNER
                )
            )
            project = await ProjectRepository(setup_session).create(
                ProjectCreate(tenant_id=tenant.id, name=slug, slug=slug)
            )
            asset = await AssetRepository(setup_session).create(
                AssetCreate(
                    tenant_id=tenant.id,
                    project_id=project.id,
                    asset_type=AssetType.WEBSITE,
                    name=slug,
                    locator="https://example.com",
                )
            )
            await setup_session.commit()
            tenant_id, user_id = tenant.id, user.id
            project_id, asset_id = project.id, asset.id

        login_response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert login_response.status_code == 200, login_response.text
        headers = bearer(login_response.json()["token"])

        create_response = await client.post(
            "/api/v1/scans",
            json={
                "project_id": str(project_id),
                "asset_id": str(asset_id),
                "scanner_type": "fake",
            },
            headers=headers,
        )
        assert create_response.status_code == 201, create_response.text
        scan_id = uuid.UUID(create_response.json()["id"])

        async with real_session_factory() as verify_session:
            scan = await ScanRepository(verify_session).get_by_id(tenant_id, scan_id)
            assert scan is not None
            assert scan.status == ScanStatus.QUEUED
    finally:
        async with real_session_factory() as cleanup_session:
            if scan_id is not None:
                await cleanup_session.execute(delete(Scan).where(Scan.id == scan_id))
            if asset_id is not None:
                await cleanup_session.execute(delete(Asset).where(Asset.id == asset_id))
            if project_id is not None:
                await cleanup_session.execute(delete(Project).where(Project.id == project_id))
            if tenant_id is not None:
                await cleanup_session.execute(
                    delete(TenantMembership).where(TenantMembership.tenant_id == tenant_id)
                )
            if user_id is not None:
                await _delete_user_and_dependents(cleanup_session, user_id)
            if tenant_id is not None:
                await cleanup_session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await cleanup_session.commit()
