"""Role -> permission matrix and the `require_permission` dependency.

One place defines which `MembershipRole` may do what. Every
permission-gated endpoint depends on `require_permission(Permission.X)` —
no endpoint hand-rolls a role check. See docs/rbac.md for the full matrix
and the reasoning behind it.
"""

from collections.abc import Callable
from enum import StrEnum

from fastapi import HTTPException, status

from app.api.deps import CurrentPrincipalDep, Principal
from app.models.enums import MembershipRole


class Permission(StrEnum):
    MEMBERS_VIEW = "members:view"
    MEMBERS_MANAGE = "members:manage"
    API_KEYS_VIEW = "api_keys:view"
    API_KEYS_MANAGE = "api_keys:manage"
    AUDIT_VIEW = "audit:view"
    SCANS_VIEW = "scans:view"
    # Covers create/cancel/retry — every scan-lifecycle write. Scanning is
    # this platform's core operational activity, not an admin-only action,
    # so it's a role-granted permission of its own rather than folded into
    # MEMBERS_MANAGE/API_KEYS_MANAGE.
    SCANS_MANAGE = "scans:manage"


ROLE_PERMISSIONS: dict[MembershipRole, frozenset[Permission]] = {
    # Owner and Administrator are functionally equivalent in this phase —
    # see docs/rbac.md for why a finer Owner-only distinction (e.g. only
    # an Owner may remove another Owner) is deferred rather than guessed at.
    MembershipRole.OWNER: frozenset(Permission),
    MembershipRole.ADMINISTRATOR: frozenset(Permission),
    MembershipRole.SECURITY_ANALYST: frozenset(
        {
            Permission.MEMBERS_VIEW,
            Permission.API_KEYS_VIEW,
            Permission.AUDIT_VIEW,
            Permission.SCANS_VIEW,
            Permission.SCANS_MANAGE,
        }
    ),
    MembershipRole.DEVELOPER: frozenset(
        {
            Permission.MEMBERS_VIEW,
            Permission.API_KEYS_VIEW,
            Permission.SCANS_VIEW,
            Permission.SCANS_MANAGE,
        }
    ),
    MembershipRole.VIEWER: frozenset({Permission.MEMBERS_VIEW, Permission.SCANS_VIEW}),
}


def has_permission(role: MembershipRole, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def principal_has_permission(principal: Principal, permission: Permission) -> bool:
    """The single dispatch point every permission check goes through
    (both `require_permission` below and anywhere else that needs to
    check ad hoc). `scopes` and `role` are mutually exclusive on a
    `Principal` by construction (see its docstring) — an API-key
    principal's `scopes` are checked on their own and never fall back to
    `has_permission`'s role-based grant, so a key structurally cannot
    exceed the scopes it was actually issued with."""
    if principal.scopes is not None:
        return permission.value in principal.scopes
    if principal.role is not None:
        return has_permission(principal.role, permission)
    return False


def require_permission(
    permission: Permission,
) -> Callable[[CurrentPrincipalDep], CurrentPrincipalDep]:
    """FastAPI dependency factory: `Depends(require_permission(Permission.X))`.

    Returns the caller's `Principal` unchanged on success (so it can be
    used as a drop-in replacement for `CurrentPrincipalDep` in an endpoint
    signature), or raises 403.
    """

    def _dependency(principal: CurrentPrincipalDep) -> CurrentPrincipalDep:
        if not principal_has_permission(principal, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return principal

    return _dependency
