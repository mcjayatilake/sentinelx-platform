"""Read-only, tenant-scoped audit trail endpoint."""

from fastapi import APIRouter, Depends

from app.api.deps import CurrentPrincipalDep, DbSessionDep
from app.core.permissions import Permission, require_permission
from app.repositories.audit_event_repository import AuditEventRepository
from app.schemas.audit_event import AuditEventRead
from app.schemas.common import Page, PaginationParams

router = APIRouter(prefix="/audit-events", tags=["audit-events"])


@router.get(
    "",
    response_model=Page[AuditEventRead],
    dependencies=[Depends(require_permission(Permission.AUDIT_VIEW))],
)
async def list_audit_events(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    limit: int = 50,
    offset: int = 0,
) -> Page[AuditEventRead]:
    page = await AuditEventRepository(session).list_by_tenant(
        principal.tenant_id, PaginationParams(limit=limit, offset=offset)
    )
    return Page[AuditEventRead](
        items=[AuditEventRead.model_validate(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )
