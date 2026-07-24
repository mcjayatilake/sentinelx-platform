"""AuditEvent: append-only by design, survives referenced-entity deletion."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.enums import AuditOutcome
from app.repositories.audit_event_repository import AuditEventRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.audit_event import AuditEventCreate
from app.schemas.common import PaginationParams
from app.schemas.tenant import TenantCreate


def test_audit_event_has_no_updated_at_column() -> None:
    """CreatedAtMixin (not TimestampMixin) — append-only is enforced at the
    schema level, not just by convention."""
    assert "updated_at" not in AuditEvent.__table__.columns


def test_repository_exposes_no_update_or_delete_method() -> None:
    assert not hasattr(AuditEventRepository, "update")
    assert not hasattr(AuditEventRepository, "delete")


async def test_create_and_list_by_tenant(db_session: AsyncSession) -> None:
    tenant = await TenantRepository(db_session).create(TenantCreate(name="Acme", slug="acme"))
    repo = AuditEventRepository(db_session)
    await repo.create(
        AuditEventCreate(
            tenant_id=tenant.id,
            action="project.create",
            resource_type="project",
            outcome=AuditOutcome.SUCCESS,
        )
    )

    page = await repo.list_by_tenant(tenant.id, PaginationParams(limit=10, offset=0))
    assert page.total == 1
    assert page.items[0].action == "project.create"


async def test_audit_trail_survives_tenant_deletion(db_session: AsyncSession) -> None:
    """AuditEvent.tenant_id is ON DELETE SET NULL: hard-deleting the tenant
    must not delete (or block deleting) the audit record — it survives
    with tenant_id nulled out."""
    tenant = await TenantRepository(db_session).create(
        TenantCreate(name="Ephemeral", slug="ephemeral")
    )
    repo = AuditEventRepository(db_session)
    event = await repo.create(
        AuditEventCreate(
            tenant_id=tenant.id,
            action="tenant.create",
            resource_type="tenant",
            outcome=AuditOutcome.SUCCESS,
        )
    )

    # No repository exposes tenant deletion (soft-delete is the intended
    # path); this raw DELETE exists only to prove the DB-level FK behavior.
    await db_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
    await db_session.flush()
    await db_session.refresh(event)

    assert event.tenant_id is None
    assert event.action == "tenant.create"
