"""API key management endpoints — tenant-scoped, permission-gated.

Issuance and rotation are the only two calls that ever see a plaintext
key; both return it exactly once, in the response body of this request.

Permission checks run as router-level `dependencies=[...]` rather than as
the value bound to the `principal` parameter — `require_permission`
returns a fresh dependency callable per call, and mixing that with the
`Annotated[Principal, Depends(get_current_principal)]` already baked into
`CurrentPrincipalDep` would give the parameter two conflicting `Depends`.
Both dependencies still resolve the same cached `get_current_principal`
per request either way.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentPrincipalDep, DbSessionDep
from app.core.config import get_settings
from app.core.permissions import Permission, require_permission
from app.core.rate_limit import rate_limit
from app.models.api_key import APIKeyMetadata
from app.models.enums import AuditOutcome
from app.repositories.api_key_repository import APIKeyMetadataRepository
from app.repositories.audit_event_repository import AuditEventRepository
from app.schemas.api_key import APIKeyIssueRequest, APIKeyIssueResponse, APIKeyMetadataRead
from app.schemas.audit_event import AuditEventCreate
from app.schemas.common import Page, PaginationParams
from app.services.api_key_service import APIKeyService

router = APIRouter(prefix="/api-keys", tags=["api-keys"])

_settings = get_settings()


@router.post(
    "",
    response_model=APIKeyIssueResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(require_permission(Permission.API_KEYS_MANAGE)),
        Depends(
            rate_limit(
                "api_key_create", max_requests=_settings.rate_limit_api_key_create_per_minute
            )
        ),
    ],
)
async def create_api_key(
    data: APIKeyIssueRequest, session: DbSessionDep, principal: CurrentPrincipalDep
) -> APIKeyIssueResponse:
    response = await APIKeyService(APIKeyMetadataRepository(session)).issue(
        tenant_id=principal.tenant_id, user_id=principal.user.id, data=data
    )
    await AuditEventRepository(session).create(
        AuditEventCreate(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user.id,
            action="api_key.create",
            resource_type="api_key",
            resource_id=str(response.id),
            outcome=AuditOutcome.SUCCESS,
        )
    )
    return response


@router.get(
    "",
    response_model=Page[APIKeyMetadataRead],
    dependencies=[Depends(require_permission(Permission.API_KEYS_VIEW))],
)
async def list_api_keys(
    session: DbSessionDep,
    principal: CurrentPrincipalDep,
    limit: int = 50,
    offset: int = 0,
) -> Page[APIKeyMetadataRead]:
    page = await APIKeyMetadataRepository(session).list_by_tenant(
        principal.tenant_id, PaginationParams(limit=limit, offset=offset)
    )
    return Page[APIKeyMetadataRead](
        items=[APIKeyMetadataRead.model_validate(item) for item in page.items],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


async def _get_owned_api_key(
    session: DbSessionDep, principal: CurrentPrincipalDep, api_key_id: uuid.UUID
) -> APIKeyMetadata:
    record = await APIKeyMetadataRepository(session).get_by_id(principal.tenant_id, api_key_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found.")
    return record


@router.post(
    "/{api_key_id}/revoke",
    response_model=APIKeyMetadataRead,
    dependencies=[Depends(require_permission(Permission.API_KEYS_MANAGE))],
)
async def revoke_api_key(
    api_key_id: uuid.UUID, session: DbSessionDep, principal: CurrentPrincipalDep
) -> APIKeyMetadataRead:
    record = await _get_owned_api_key(session, principal, api_key_id)
    record = await APIKeyService(APIKeyMetadataRepository(session)).revoke(record)

    await AuditEventRepository(session).create(
        AuditEventCreate(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user.id,
            action="api_key.revoke",
            resource_type="api_key",
            resource_id=str(record.id),
            outcome=AuditOutcome.SUCCESS,
        )
    )
    return APIKeyMetadataRead.model_validate(record)


@router.post(
    "/{api_key_id}/rotate",
    response_model=APIKeyIssueResponse,
    dependencies=[Depends(require_permission(Permission.API_KEYS_MANAGE))],
)
async def rotate_api_key(
    api_key_id: uuid.UUID, session: DbSessionDep, principal: CurrentPrincipalDep
) -> APIKeyIssueResponse:
    record = await _get_owned_api_key(session, principal, api_key_id)
    response = await APIKeyService(APIKeyMetadataRepository(session)).rotate(
        tenant_id=principal.tenant_id, user_id=principal.user.id, record=record
    )

    audit_events = AuditEventRepository(session)
    await audit_events.create(
        AuditEventCreate(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user.id,
            action="api_key.revoke",
            resource_type="api_key",
            resource_id=str(record.id),
            outcome=AuditOutcome.SUCCESS,
            event_metadata={"reason": "rotated"},
        )
    )
    await audit_events.create(
        AuditEventCreate(
            tenant_id=principal.tenant_id,
            actor_user_id=principal.user.id,
            action="api_key.create",
            resource_type="api_key",
            resource_id=str(response.id),
            outcome=AuditOutcome.SUCCESS,
            event_metadata={"reason": "rotated", "replaces": str(record.id)},
        )
    )
    return response
