"""Tenant membership management — list, add-by-email, role change, remove.

Every endpoint is `require_permission`-gated (no hand-rolled role checks)
and every mutation writes an `AuditEvent`, including a dedicated
`membership.role_change` event whenever `role` actually changes.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentPrincipalDep, DbSessionDep, Principal
from app.core.permissions import Permission, require_permission
from app.models.enums import AuditOutcome, MembershipStatus
from app.repositories.audit_event_repository import AuditEventRepository
from app.repositories.membership_repository import TenantMembershipRepository
from app.repositories.user_repository import UserRepository
from app.schemas.audit_event import AuditEventCreate
from app.schemas.common import Page, PaginationParams
from app.schemas.membership import (
    TenantMembershipAddByEmail,
    TenantMembershipCreate,
    TenantMembershipRead,
    TenantMembershipUpdate,
)

router = APIRouter(prefix="/memberships", tags=["memberships"])


def _actor_user_id(principal: Principal) -> uuid.UUID | None:
    """An API-key-authenticated `Principal` may have no bound user (a
    tenant-level service-account key) — `AuditEvent.actor_user_id` is
    nullable for exactly this reason."""
    return principal.user.id if principal.user is not None else None


@router.get(
    "",
    response_model=Page[TenantMembershipRead],
    dependencies=[Depends(require_permission(Permission.MEMBERS_VIEW))],
)
async def list_memberships(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    limit: int = 50,
    offset: int = 0,
) -> Page[TenantMembershipRead]:
    page = await TenantMembershipRepository(session).list_by_tenant(
        principal.tenant_id, PaginationParams(limit=limit, offset=offset)
    )
    return Page[TenantMembershipRead](
        items=[TenantMembershipRead.model_validate(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "",
    response_model=TenantMembershipRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.MEMBERS_MANAGE))],
)
async def add_member(
    data: TenantMembershipAddByEmail, session: DbSessionDep, principal: CurrentPrincipalDep
) -> TenantMembershipRead:
    user = await UserRepository(session).get_by_email(data.email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No user with that email exists."
        )

    memberships = TenantMembershipRepository(session)
    existing = await memberships.get_by_tenant_and_user(principal.tenant_id, user.id)
    if existing is not None:
        if existing.status != MembershipStatus.REMOVED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member of this tenant.",
            )
        # Re-adding a previously removed member reactivates the existing
        # row rather than inserting a new one — `(tenant_id, user_id)` is
        # unique, so a second row is not possible.
        membership = await memberships.update(
            existing, TenantMembershipUpdate(role=data.role, status=MembershipStatus.ACTIVE)
        )
    else:
        membership = await memberships.create(
            TenantMembershipCreate(tenant_id=principal.tenant_id, user_id=user.id, role=data.role)
        )

    await AuditEventRepository(session).create(
        AuditEventCreate(
            tenant_id=principal.tenant_id,
            actor_user_id=_actor_user_id(principal),
            action="membership.add",
            resource_type="tenant_membership",
            resource_id=str(membership.id),
            outcome=AuditOutcome.SUCCESS,
            event_metadata={"member_user_id": str(user.id), "role": data.role.value},
        )
    )
    return TenantMembershipRead.model_validate(membership)


@router.patch(
    "/{membership_id}",
    response_model=TenantMembershipRead,
    dependencies=[Depends(require_permission(Permission.MEMBERS_MANAGE))],
)
async def update_membership(
    membership_id: uuid.UUID,
    data: TenantMembershipUpdate,
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
) -> TenantMembershipRead:
    memberships = TenantMembershipRepository(session)
    membership = await memberships.get_by_id(principal.tenant_id, membership_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found.")

    previous_role = membership.role
    membership = await memberships.update(membership, data)

    if data.role is not None and data.role != previous_role:
        await AuditEventRepository(session).create(
            AuditEventCreate(
                tenant_id=principal.tenant_id,
                actor_user_id=_actor_user_id(principal),
                action="membership.role_change",
                resource_type="tenant_membership",
                resource_id=str(membership.id),
                outcome=AuditOutcome.SUCCESS,
                event_metadata={
                    "member_user_id": str(membership.user_id),
                    "old_role": previous_role.value,
                    "new_role": data.role.value,
                },
            )
        )
    return TenantMembershipRead.model_validate(membership)


@router.delete(
    "/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_permission(Permission.MEMBERS_MANAGE))],
)
async def remove_member(
    membership_id: uuid.UUID, session: DbSessionDep, principal: CurrentPrincipalDep
) -> None:
    memberships = TenantMembershipRepository(session)
    membership = await memberships.get_by_id(principal.tenant_id, membership_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found.")

    await memberships.update(membership, TenantMembershipUpdate(status=MembershipStatus.REMOVED))

    await AuditEventRepository(session).create(
        AuditEventCreate(
            tenant_id=principal.tenant_id,
            actor_user_id=_actor_user_id(principal),
            action="membership.remove",
            resource_type="tenant_membership",
            resource_id=str(membership.id),
            outcome=AuditOutcome.SUCCESS,
            event_metadata={"member_user_id": str(membership.user_id)},
        )
    )
