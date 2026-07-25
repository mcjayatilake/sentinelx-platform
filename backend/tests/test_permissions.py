"""Role -> permission matrix behavior."""

import pytest

from app.core.permissions import ROLE_PERMISSIONS, Permission, has_permission
from app.models.enums import MembershipRole


@pytest.mark.parametrize("permission", list(Permission))
def test_owner_has_every_permission(permission: Permission) -> None:
    assert has_permission(MembershipRole.OWNER, permission)


@pytest.mark.parametrize("permission", list(Permission))
def test_administrator_has_every_permission(permission: Permission) -> None:
    assert has_permission(MembershipRole.ADMINISTRATOR, permission)


def test_viewer_can_only_view_members() -> None:
    assert has_permission(MembershipRole.VIEWER, Permission.MEMBERS_VIEW)
    assert not has_permission(MembershipRole.VIEWER, Permission.MEMBERS_MANAGE)
    assert not has_permission(MembershipRole.VIEWER, Permission.API_KEYS_VIEW)
    assert not has_permission(MembershipRole.VIEWER, Permission.API_KEYS_MANAGE)
    assert not has_permission(MembershipRole.VIEWER, Permission.AUDIT_VIEW)


def test_security_analyst_has_view_but_not_manage_permissions() -> None:
    assert has_permission(MembershipRole.SECURITY_ANALYST, Permission.MEMBERS_VIEW)
    assert has_permission(MembershipRole.SECURITY_ANALYST, Permission.API_KEYS_VIEW)
    assert has_permission(MembershipRole.SECURITY_ANALYST, Permission.AUDIT_VIEW)
    assert not has_permission(MembershipRole.SECURITY_ANALYST, Permission.MEMBERS_MANAGE)
    assert not has_permission(MembershipRole.SECURITY_ANALYST, Permission.API_KEYS_MANAGE)


def test_developer_has_narrower_access_than_security_analyst() -> None:
    assert has_permission(MembershipRole.DEVELOPER, Permission.MEMBERS_VIEW)
    assert has_permission(MembershipRole.DEVELOPER, Permission.API_KEYS_VIEW)
    assert not has_permission(MembershipRole.DEVELOPER, Permission.AUDIT_VIEW)
    assert not has_permission(MembershipRole.DEVELOPER, Permission.MEMBERS_MANAGE)


def test_every_membership_role_has_a_matrix_entry() -> None:
    assert set(ROLE_PERMISSIONS.keys()) == set(MembershipRole)


def test_manage_permission_implies_view_across_all_roles() -> None:
    for role, permissions in ROLE_PERMISSIONS.items():
        if Permission.MEMBERS_MANAGE in permissions:
            assert Permission.MEMBERS_VIEW in permissions, role
        if Permission.API_KEYS_MANAGE in permissions:
            assert Permission.API_KEYS_VIEW in permissions, role
