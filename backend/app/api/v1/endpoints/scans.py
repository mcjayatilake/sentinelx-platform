"""Scan orchestration endpoints — create, get, list, cancel, retry,
progress. This phase implements orchestration only: no scanner actually
executes here or is invoked synchronously from a request. Business logic
lives in `app.services.scan_service.ScanService`; domain errors it raises
are handled globally (see `app.core.error_handlers`).

Every endpoint is `require_permission`-gated and every operation is
scoped to `principal.tenant_id` (verified server-side at token issuance/
refresh, never a client-supplied value) — no scan can be created, read,
or mutated outside the caller's tenant boundary.
"""

import uuid

from fastapi import APIRouter, Depends
from fastapi import status as http_status

from app.api.deps import CurrentPrincipalDep, DbSessionDep
from app.core.permissions import Permission, require_permission
from app.models.enums import ScanStatus
from app.schemas.common import Page, PaginationParams
from app.schemas.scan import ScanCreateRequest, ScanProgressRead, ScanRead
from app.services.scan_service import ScanService
from app.workers.celery_app import celery_app
from app.workers.job_queue_celery import CeleryJobQueue

router = APIRouter(prefix="/scans", tags=["scans"])


def _service(session: DbSessionDep) -> ScanService:
    return ScanService(session, CeleryJobQueue(celery_app))


@router.post(
    "",
    response_model=ScanRead,
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.SCANS_MANAGE))],
)
async def create_scan(
    data: ScanCreateRequest, session: DbSessionDep, principal: CurrentPrincipalDep
) -> ScanRead:
    requested_by = principal.user.id if principal.user is not None else None
    return await _service(session).create_scan(principal.tenant_id, requested_by, data)


@router.get(
    "",
    response_model=Page[ScanRead],
    dependencies=[Depends(require_permission(Permission.SCANS_VIEW))],
)
async def list_scans(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    limit: int = 50,
    offset: int = 0,
    asset_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    status: ScanStatus | None = None,
) -> Page[ScanRead]:
    return await _service(session).list_scans(
        principal.tenant_id,
        PaginationParams(limit=limit, offset=offset),
        asset_id=asset_id,
        project_id=project_id,
        status=status,
    )


@router.get(
    "/{scan_id}",
    response_model=ScanRead,
    dependencies=[Depends(require_permission(Permission.SCANS_VIEW))],
)
async def get_scan(
    scan_id: uuid.UUID, session: DbSessionDep, principal: CurrentPrincipalDep
) -> ScanRead:
    return await _service(session).get_scan(principal.tenant_id, scan_id)


@router.get(
    "/{scan_id}/progress",
    response_model=ScanProgressRead,
    dependencies=[Depends(require_permission(Permission.SCANS_VIEW))],
)
async def get_scan_progress(
    scan_id: uuid.UUID, session: DbSessionDep, principal: CurrentPrincipalDep
) -> ScanProgressRead:
    return await _service(session).get_progress(principal.tenant_id, scan_id)


@router.post(
    "/{scan_id}/cancel",
    response_model=ScanRead,
    dependencies=[Depends(require_permission(Permission.SCANS_MANAGE))],
)
async def cancel_scan(
    scan_id: uuid.UUID, session: DbSessionDep, principal: CurrentPrincipalDep
) -> ScanRead:
    return await _service(session).cancel_scan(principal.tenant_id, scan_id)


@router.post(
    "/{scan_id}/retry",
    response_model=ScanRead,
    status_code=http_status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.SCANS_MANAGE))],
)
async def retry_scan(
    scan_id: uuid.UUID, session: DbSessionDep, principal: CurrentPrincipalDep
) -> ScanRead:
    requested_by = principal.user.id if principal.user is not None else None
    return await _service(session).retry_scan(principal.tenant_id, scan_id, requested_by)
