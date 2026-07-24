"""Scan repository: status persistence, timestamp consistency constraints."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, ScanStatus
from app.repositories.asset_repository import AssetRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.asset import AssetCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate, ScanUpdate
from app.schemas.tenant import TenantCreate


async def _make_asset(session: AsyncSession, slug: str):
    tenant = await TenantRepository(session).create(TenantCreate(name=slug, slug=slug))
    project = await ProjectRepository(session).create(
        ProjectCreate(tenant_id=tenant.id, name=slug, slug=slug)
    )
    asset = await AssetRepository(session).create(
        AssetCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_type=AssetType.WEBSITE,
            name=slug,
            locator="https://example.com",
        )
    )
    return tenant, project, asset


async def test_scan_defaults_to_pending(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_asset(db_session, "acme")
    repo = ScanRepository(db_session)
    scan = await repo.create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="zap"
        )
    )
    assert scan.status == ScanStatus.PENDING
    assert scan.queued_at is None


async def test_status_transition_persists(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_asset(db_session, "acme2")
    repo = ScanRepository(db_session)
    scan = await repo.create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="zap"
        )
    )

    now = datetime.now(UTC)
    updated = await repo.update(
        scan,
        ScanUpdate(
            status=ScanStatus.SUCCEEDED, started_at=now, completed_at=now + timedelta(seconds=5)
        ),
    )
    assert updated.status == ScanStatus.SUCCEEDED
    assert updated.completed_at is not None
    assert updated.completed_at > updated.started_at


async def test_completed_before_started_rejected(db_session: AsyncSession) -> None:
    """The `ck_scans_completed_after_started` CHECK constraint must reject
    a completed_at earlier than started_at."""
    tenant, project, asset = await _make_asset(db_session, "acme3")
    repo = ScanRepository(db_session)
    scan = await repo.create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="zap"
        )
    )

    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        await repo.update(scan, ScanUpdate(started_at=now, completed_at=now - timedelta(seconds=5)))


async def test_completed_and_failed_mutually_exclusive(db_session: AsyncSession) -> None:
    tenant, project, asset = await _make_asset(db_session, "acme4")
    repo = ScanRepository(db_session)
    scan = await repo.create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="zap"
        )
    )

    now = datetime.now(UTC)
    with pytest.raises(IntegrityError):
        await repo.update(scan, ScanUpdate(completed_at=now, failed_at=now))
