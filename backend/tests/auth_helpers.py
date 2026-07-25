"""Shared setup helpers for authentication/authorization tests.

Not a `conftest.py` fixture module — these are plain importable functions,
used by the many auth test files that all need the same "tenant + user +
membership + hashed password" scaffolding, to avoid copy-pasting it into
every file.
"""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hash_password
from app.models.enums import MembershipRole
from app.models.membership import TenantMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.tenant_repository import TenantRepository
from app.repositories.user_repository import UserRepository
from app.schemas.membership import TenantMembershipCreate
from app.schemas.tenant import TenantCreate
from app.schemas.user import UserCreate
from app.services.auth_service import AuthService
from app.services.email_service import EmailService


class RecordingEmailSender:
    """Test double satisfying `app.services.email_service.EmailSender`:
    captures every call instead of delivering it, so a test can extract
    the raw verification/reset token embedded in the body. That raw token
    is never available anywhere else, by design — only its SHA-256 hash
    is ever persisted."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))


def extract_token(body: str) -> str:
    """Pulls the `token=<...>` query value out of a stubbed email body
    built by `app.services.email_service.EmailService`."""
    return body.rsplit("token=", 1)[1]


class AlwaysBreachedPasswordChecker:
    """Test double satisfying `app.core.security.BreachedPasswordChecker`:
    every password is "breached", for exercising the hook's rejection
    path without a real HaveIBeenPwned-style integration."""

    async def is_breached(self, password: str) -> bool:
        return True


def auth_service_with_recording_sender(
    session: AsyncSession, sender: RecordingEmailSender
) -> AuthService:
    """An `AuthService` wired to `sender` instead of the default
    `LoggingEmailSender`, so a test can call `request_password_reset` /
    `request_email_verification` directly and recover the raw token
    `sender` captured."""
    return AuthService(session, email_service=EmailService(sender=sender, settings=get_settings()))


async def create_tenant_user_membership(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    tenant_slug: str,
    role: MembershipRole = MembershipRole.OWNER,
) -> tuple[Tenant, User, TenantMembership]:
    """Creates a tenant, an active user with a usable password, and an
    ACTIVE membership binding them — the common precondition for every
    login/permission/tenant-isolation test."""
    tenant = await TenantRepository(session).create(
        TenantCreate(name=tenant_slug, slug=tenant_slug)
    )
    users = UserRepository(session)
    user = await users.create(UserCreate(email=email, display_name=email))
    await users.set_password(user, hash_password(password), invalidate_sessions=False)
    membership = await TenantMembershipRepository(session).create(
        TenantMembershipCreate(tenant_id=tenant.id, user_id=user.id, role=role)
    )
    return tenant, user, membership


async def login(
    auth_client: AsyncClient, email: str, password: str, tenant_id: str | None = None
) -> dict[str, str]:
    """POSTs to `/auth/login` and returns the `TokenPair` dict. Asserts a
    single-membership (or explicit-tenant_id) success — tests exercising
    the multi-tenant-selection response shape call the endpoint directly
    instead."""
    payload: dict[str, str] = {"email": email, "password": password}
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    response = await auth_client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 200, response.text
    token: dict[str, str] = response.json()["token"]
    return token


def bearer(token: dict[str, str]) -> dict[str, str]:
    """`{"Authorization": "Bearer <access_token>"}` for a `TokenPair`
    dict — the header shape every authenticated request needs."""
    return {"Authorization": f"Bearer {token['access_token']}"}


async def login_and_get_headers(
    auth_client: AsyncClient, email: str, password: str, tenant_id: str | None = None
) -> dict[str, str]:
    """`login` + `bearer` in one call, for tests that only need the
    header and never touch the token pair itself."""
    return bearer(await login(auth_client, email, password, tenant_id))
