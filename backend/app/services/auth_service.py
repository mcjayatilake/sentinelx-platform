"""Authentication orchestration: register, login, refresh, logout, tenant
switching, password/email verification lifecycle, and session management.

This is the single place that constructs `AuditEventCreate` for
authentication events (login, failed login, logout, password reset
request/confirm, email verification) — every such event is emitted from
exactly one call site here, never duplicated across endpoints.

Access-token issuance always embeds the tenant and role resolved and
verified *here*, against a live `TenantMembership` row — callers never
pass a tenant ID that ends up on an issued token unchecked.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import (
    BreachedPasswordChecker,
    NullBreachedPasswordChecker,
    PasswordPolicyError,
    create_token,
    generate_secret,
    hash_password,
    hash_secret,
    needs_rehash,
    validate_password_policy,
    verify_password,
)
from app.models.enums import (
    AuditOutcome,
    MembershipRole,
    MembershipStatus,
    RefreshTokenStatus,
    UserStatus,
    VerificationTokenPurpose,
)
from app.models.membership import TenantMembership
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.repositories.audit_event_repository import AuditEventRepository
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.refresh_token_repository import RefreshTokenRepository
from app.repositories.user_repository import UserRepository
from app.repositories.verification_token_repository import VerificationTokenRepository
from app.schemas.audit_event import AuditEventCreate
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RegisterRequest,
    RegisterResponse,
    TenantOption,
    TokenPair,
)
from app.schemas.refresh_token import RefreshTokenCreate
from app.schemas.user import UserCreate, UserRead
from app.schemas.verification_token import VerificationTokenCreate
from app.services.email_service import EmailService, get_email_service


class AuthServiceError(Exception):
    """Base class for auth-service failures the API layer translates to
    HTTP responses. Never carries a password or raw token/secret."""


class InvalidCredentialsError(AuthServiceError):
    pass


class AccountInactiveError(AuthServiceError):
    pass


class EmailAlreadyRegisteredError(AuthServiceError):
    pass


class TenantAccessDeniedError(AuthServiceError):
    pass


class InvalidRefreshTokenError(AuthServiceError):
    pass


class RefreshTokenReuseDetectedError(AuthServiceError):
    pass


class InvalidVerificationTokenError(AuthServiceError):
    pass


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Caller metadata attached to security-sensitive records (refresh
    tokens, audit events) for forensics only — never used for
    authorization decisions."""

    ip_address: str | None
    user_agent: str | None


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        email_service: EmailService | None = None,
        breached_password_checker: BreachedPasswordChecker | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._email_service = email_service or get_email_service()
        self._breached_password_checker = breached_password_checker or NullBreachedPasswordChecker()

        self._users = UserRepository(session)
        self._memberships = TenantMembershipRepository(session)
        self._refresh_tokens = RefreshTokenRepository(session)
        self._verification_tokens = VerificationTokenRepository(session)
        self._audit_events = AuditEventRepository(session)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(self, data: RegisterRequest) -> RegisterResponse:
        if await self._users.get_by_email(data.email) is not None:
            raise EmailAlreadyRegisteredError()

        await self._check_password_policy(data.password)

        user = await self._users.create(
            UserCreate(email=data.email, display_name=data.display_name)
        )
        await self._users.set_password(
            user, hash_password(data.password), invalidate_sessions=False
        )

        await self._issue_verification_token_and_email(
            user, VerificationTokenPurpose.EMAIL_VERIFICATION
        )

        await self._audit_events.create(
            AuditEventCreate(
                actor_user_id=user.id,
                action="auth.register",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
            )
        )

        return RegisterResponse(user=UserRead.model_validate(user), email_verification_sent=True)

    # ------------------------------------------------------------------
    # Login / logout / refresh / switch-tenant
    # ------------------------------------------------------------------

    async def login(self, data: LoginRequest, context: RequestContext) -> LoginResponse:
        user = await self._users.get_by_email(data.email)

        if (
            user is None
            or user.hashed_password is None
            or not verify_password(data.password, user.hashed_password)
        ):
            await self._record_failed_login(user, data.email, context)
            raise InvalidCredentialsError()

        if user.status != UserStatus.ACTIVE:
            await self._record_failed_login(user, data.email, context)
            raise AccountInactiveError()

        if needs_rehash(user.hashed_password):
            await self._users.set_password(
                user, hash_password(data.password), invalidate_sessions=False
            )

        memberships = await self._memberships.list_by_user(user.id)
        tenant_id, role = self._resolve_tenant(memberships, data.tenant_id)

        if tenant_id is None:
            await self._audit_events.create(
                AuditEventCreate(
                    actor_user_id=user.id,
                    action="auth.login",
                    resource_type="user",
                    resource_id=str(user.id),
                    outcome=AuditOutcome.SUCCESS,
                    source_ip=context.ip_address,
                    user_agent=context.user_agent,
                    event_metadata={"available_tenant_count": len(memberships)},
                )
            )
            return LoginResponse(
                token=None,
                available_tenants=[
                    TenantOption(
                        tenant_id=membership.tenant_id,
                        tenant_name=membership.tenant.name,
                        role=membership.role,
                    )
                    for membership in memberships
                ],
            )

        assert role is not None  # narrowed by _resolve_tenant alongside tenant_id
        token_pair = await self._issue_fresh_token_pair(user, tenant_id, role, context)

        await self._audit_events.create(
            AuditEventCreate(
                tenant_id=tenant_id,
                actor_user_id=user.id,
                action="auth.login",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
                source_ip=context.ip_address,
                user_agent=context.user_agent,
            )
        )
        return LoginResponse(token=token_pair, available_tenants=[])

    async def refresh(self, raw_refresh_token: str, context: RequestContext) -> TokenPair:
        token = await self._refresh_tokens.get_by_hashed_token(hash_secret(raw_refresh_token))
        if token is None:
            raise InvalidRefreshTokenError()

        if token.status == RefreshTokenStatus.ROTATED:
            # An already-rotated token was presented again: the chain has
            # been stolen. Revoke every token in the family immediately.
            await self._refresh_tokens.revoke_family(token.family_id)
            await self._audit_events.create(
                AuditEventCreate(
                    tenant_id=token.tenant_id,
                    actor_user_id=token.user_id,
                    action="auth.refresh_token_reuse_detected",
                    resource_type="refresh_token_family",
                    resource_id=str(token.family_id),
                    outcome=AuditOutcome.DENIED,
                    source_ip=context.ip_address,
                    user_agent=context.user_agent,
                )
            )
            raise RefreshTokenReuseDetectedError()

        if token.status == RefreshTokenStatus.REVOKED or token.expires_at <= datetime.now(UTC):
            raise InvalidRefreshTokenError()

        user = await self._users.get_by_id(token.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise InvalidRefreshTokenError()

        membership = await self._memberships.get_by_tenant_and_user(token.tenant_id, user.id)
        if membership is None or membership.status != MembershipStatus.ACTIVE:
            raise InvalidRefreshTokenError()

        raw_secret = generate_secret(32)
        await self._refresh_tokens.rotate(
            token,
            self._build_refresh_token_create(
                token.tenant_id, user.id, token.family_id, raw_secret, context
            ),
        )

        return TokenPair(
            access_token=self._create_access_token(user, token.tenant_id, membership.role),
            refresh_token=raw_secret,
            expires_in=self._settings.jwt_access_token_expire_minutes * 60,
        )

    async def logout(
        self, user: User, tenant_id: uuid.UUID, raw_refresh_token: str, context: RequestContext
    ) -> None:
        token = await self._refresh_tokens.get_by_hashed_token(hash_secret(raw_refresh_token))
        if token is not None and token.user_id == user.id:
            await self._refresh_tokens.revoke_family(token.family_id)

        await self._audit_events.create(
            AuditEventCreate(
                tenant_id=tenant_id,
                actor_user_id=user.id,
                action="auth.logout",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
                source_ip=context.ip_address,
                user_agent=context.user_agent,
            )
        )

    async def switch_tenant(
        self, user: User, tenant_id: uuid.UUID, context: RequestContext
    ) -> TokenPair:
        membership = await self._memberships.get_by_tenant_and_user(tenant_id, user.id)
        if membership is None or membership.status != MembershipStatus.ACTIVE:
            raise TenantAccessDeniedError()

        token_pair = await self._issue_fresh_token_pair(user, tenant_id, membership.role, context)

        await self._audit_events.create(
            AuditEventCreate(
                tenant_id=tenant_id,
                actor_user_id=user.id,
                action="auth.switch_tenant",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
                source_ip=context.ip_address,
                user_agent=context.user_agent,
            )
        )
        return token_pair

    # ------------------------------------------------------------------
    # Password management
    # ------------------------------------------------------------------

    async def change_password(self, user: User, current_password: str, new_password: str) -> None:
        if user.hashed_password is None or not verify_password(
            current_password, user.hashed_password
        ):
            raise InvalidCredentialsError()

        await self._check_password_policy(new_password)

        await self._users.set_password(user, hash_password(new_password), invalidate_sessions=True)
        await self._refresh_tokens.revoke_all_for_user(user.id)

        await self._audit_events.create(
            AuditEventCreate(
                actor_user_id=user.id,
                action="auth.change_password",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
            )
        )

    async def request_password_reset(self, email: str, context: RequestContext) -> None:
        user = await self._users.get_by_email(email)
        if user is None or user.status != UserStatus.ACTIVE:
            # No audit event and no error here, by design: the endpoint
            # always returns the same generic response regardless of
            # whether the email is registered, so neither the HTTP
            # response nor the audit trail can be used to enumerate
            # accounts.
            return

        await self._issue_verification_token_and_email(
            user, VerificationTokenPurpose.PASSWORD_RESET
        )

        await self._audit_events.create(
            AuditEventCreate(
                actor_user_id=user.id,
                action="auth.password_reset_request",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
                source_ip=context.ip_address,
                user_agent=context.user_agent,
            )
        )

    async def confirm_password_reset(self, raw_token: str, new_password: str) -> None:
        token = await self._verification_tokens.get_valid_by_hashed_token(
            hash_secret(raw_token), VerificationTokenPurpose.PASSWORD_RESET
        )
        if token is None:
            await self._audit_events.create(
                AuditEventCreate(
                    action="auth.password_reset",
                    resource_type="verification_token",
                    outcome=AuditOutcome.FAILURE,
                )
            )
            raise InvalidVerificationTokenError()

        await self._check_password_policy(new_password)

        user = await self._users.get_by_id(token.user_id)
        if user is None:
            raise InvalidVerificationTokenError()

        await self._users.set_password(user, hash_password(new_password), invalidate_sessions=True)
        await self._refresh_tokens.revoke_all_for_user(user.id)
        await self._verification_tokens.consume(token)

        await self._audit_events.create(
            AuditEventCreate(
                actor_user_id=user.id,
                action="auth.password_reset",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
            )
        )

    # ------------------------------------------------------------------
    # Email verification
    # ------------------------------------------------------------------

    async def request_email_verification(self, user: User) -> None:
        if user.email_verified_at is not None:
            return
        await self._issue_verification_token_and_email(
            user, VerificationTokenPurpose.EMAIL_VERIFICATION
        )

    async def confirm_email_verification(self, raw_token: str) -> None:
        token = await self._verification_tokens.get_valid_by_hashed_token(
            hash_secret(raw_token), VerificationTokenPurpose.EMAIL_VERIFICATION
        )
        if token is None:
            await self._audit_events.create(
                AuditEventCreate(
                    action="auth.email_verification",
                    resource_type="verification_token",
                    outcome=AuditOutcome.FAILURE,
                )
            )
            raise InvalidVerificationTokenError()

        user = await self._users.get_by_id(token.user_id)
        if user is None:
            raise InvalidVerificationTokenError()

        await self._users.mark_email_verified(user)
        await self._verification_tokens.consume(token)

        await self._audit_events.create(
            AuditEventCreate(
                actor_user_id=user.id,
                action="auth.email_verification",
                resource_type="user",
                resource_id=str(user.id),
                outcome=AuditOutcome.SUCCESS,
            )
        )

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    async def list_sessions(self, user_id: uuid.UUID) -> list[RefreshToken]:
        return await self._refresh_tokens.list_active_sessions(user_id)

    async def revoke_session(self, user: User, token_id: uuid.UUID) -> None:
        token = await self._refresh_tokens.get_by_id(user.id, token_id)
        if token is None:
            raise InvalidRefreshTokenError()
        await self._refresh_tokens.revoke_family(token.family_id)

    async def revoke_all_sessions(self, user: User) -> None:
        """ "Log out everywhere": revoke every refresh token and bump
        `token_version` so already-issued access tokens stop working too,
        not just future refresh attempts."""
        await self._refresh_tokens.revoke_all_for_user(user.id)
        await self._users.bump_token_version(user)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _check_password_policy(self, password: str) -> None:
        validate_password_policy(password, self._settings)
        if await self._breached_password_checker.is_breached(password):
            raise PasswordPolicyError(["password has appeared in a known data breach"])

    async def _record_failed_login(
        self, user: User | None, attempted_email: str, context: RequestContext
    ) -> None:
        await self._audit_events.create(
            AuditEventCreate(
                actor_user_id=user.id if user else None,
                action="auth.login",
                resource_type="user",
                resource_id=str(user.id) if user else attempted_email,
                outcome=AuditOutcome.FAILURE,
                source_ip=context.ip_address,
                user_agent=context.user_agent,
            )
        )

    def _resolve_tenant(
        self, memberships: list[TenantMembership], requested_tenant_id: uuid.UUID | None
    ) -> tuple[uuid.UUID | None, MembershipRole | None]:
        if requested_tenant_id is not None:
            match = next((m for m in memberships if m.tenant_id == requested_tenant_id), None)
            if match is None:
                raise TenantAccessDeniedError()
            return match.tenant_id, match.role

        if len(memberships) == 1:
            return memberships[0].tenant_id, memberships[0].role

        return None, None

    def _create_access_token(self, user: User, tenant_id: uuid.UUID, role: MembershipRole) -> str:
        token, _ = create_token(
            subject=str(user.id),
            token_type="access",
            extra_claims={
                "tenant_id": str(tenant_id),
                "role": role.value,
                "ver": user.token_version,
            },
        )
        return token

    def _build_refresh_token_create(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        family_id: uuid.UUID,
        raw_secret: str,
        context: RequestContext,
    ) -> RefreshTokenCreate:
        return RefreshTokenCreate(
            tenant_id=tenant_id,
            user_id=user_id,
            family_id=family_id,
            hashed_token=hash_secret(raw_secret),
            expires_at=datetime.now(UTC)
            + timedelta(days=self._settings.jwt_refresh_token_expire_days),
            created_by_ip=context.ip_address,
            user_agent=context.user_agent,
        )

    async def _issue_fresh_token_pair(
        self, user: User, tenant_id: uuid.UUID, role: MembershipRole, context: RequestContext
    ) -> TokenPair:
        family_id = uuid.uuid4()
        raw_secret = generate_secret(32)
        await self._refresh_tokens.create(
            self._build_refresh_token_create(tenant_id, user.id, family_id, raw_secret, context),
            family_id=family_id,
        )
        return TokenPair(
            access_token=self._create_access_token(user, tenant_id, role),
            refresh_token=raw_secret,
            expires_in=self._settings.jwt_access_token_expire_minutes * 60,
        )

    async def _issue_verification_token_and_email(
        self, user: User, purpose: VerificationTokenPurpose
    ) -> None:
        await self._verification_tokens.invalidate_pending_for_user(user.id, purpose)

        raw_token = generate_secret(32)
        await self._verification_tokens.create(
            VerificationTokenCreate(
                user_id=user.id,
                purpose=purpose,
                hashed_token=hash_secret(raw_token),
                expires_at=datetime.now(UTC) + self._verification_token_ttl(purpose),
            )
        )

        if purpose == VerificationTokenPurpose.EMAIL_VERIFICATION:
            await self._email_service.send_verification_email(user.email, raw_token)
        else:
            await self._email_service.send_password_reset_email(user.email, raw_token)

    def _verification_token_ttl(self, purpose: VerificationTokenPurpose) -> timedelta:
        if purpose == VerificationTokenPurpose.EMAIL_VERIFICATION:
            return timedelta(hours=self._settings.email_verification_token_expire_hours)
        return timedelta(minutes=self._settings.password_reset_token_expire_minutes)
