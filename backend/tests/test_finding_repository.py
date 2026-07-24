"""Finding repository: severity/status DB-level validation, fingerprint dedup."""

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, FindingSeverity
from app.repositories.asset_repository import AssetRepository
from app.repositories.finding_repository import FindingRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.asset import AssetCreate
from app.schemas.finding import FindingCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.tenant import TenantCreate


async def _make_scan(session: AsyncSession, slug: str):
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
    scan = await ScanRepository(session).create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="zap"
        )
    )
    return tenant, project, asset, scan


async def test_severity_check_constraint_enforced_at_db_level(db_session: AsyncSession) -> None:
    """Pydantic's FindingSeverity enum already rejects bad input at the
    schema boundary; this proves the DB CHECK constraint backing the
    `native_enum=False` column enforces it independently, by inserting raw
    SQL that bypasses the ORM/schema layer entirely."""
    tenant, project, asset, scan = await _make_scan(db_session, "acme")

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text(
                """
                INSERT INTO findings
                    (tenant_id, project_id, asset_id, scan_id, title, severity,
                     source_tool, fingerprint, id)
                VALUES
                    (:tenant_id, :project_id, :asset_id, :scan_id, 'bad', 'not_a_real_severity',
                     'zap', 'fp-bad', gen_random_uuid())
                """
            ),
            {
                "tenant_id": tenant.id,
                "project_id": project.id,
                "asset_id": asset.id,
                "scan_id": scan.id,
            },
        )


async def test_fingerprint_deduplication_updates_existing_row(db_session: AsyncSession) -> None:
    tenant, project, asset, scan1 = await _make_scan(db_session, "acme2")
    repo = FindingRepository(db_session)

    first = await repo.record_detection(
        FindingCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scan_id=scan1.id,
            title="Reflected XSS",
            severity=FindingSeverity.HIGH,
            source_tool="zap",
            fingerprint="fp-xss-1",
        )
    )
    first_seen = first.first_seen_at

    scan2 = await ScanRepository(db_session).create(
        ScanCreate(
            tenant_id=tenant.id, project_id=project.id, asset_id=asset.id, scanner_type="zap"
        )
    )
    second = await repo.record_detection(
        FindingCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scan_id=scan2.id,
            title="Reflected XSS",
            severity=FindingSeverity.HIGH,
            source_tool="zap",
            fingerprint="fp-xss-1",
        )
    )

    assert second.id == first.id, "re-detection must update the same row, not insert a new one"
    assert second.first_seen_at == first_seen, "first_seen_at must never change"
    assert second.scan_id == scan2.id, "scan_id should track the most recent detecting scan"
    assert second.last_seen_at >= first_seen


async def test_different_fingerprint_creates_separate_finding(db_session: AsyncSession) -> None:
    tenant, project, asset, scan = await _make_scan(db_session, "acme3")
    repo = FindingRepository(db_session)

    a = await repo.record_detection(
        FindingCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scan_id=scan.id,
            title="A",
            severity=FindingSeverity.LOW,
            source_tool="zap",
            fingerprint="fp-a",
        )
    )
    b = await repo.record_detection(
        FindingCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scan_id=scan.id,
            title="B",
            severity=FindingSeverity.LOW,
            source_tool="zap",
            fingerprint="fp-b",
        )
    )
    assert a.id != b.id


async def test_last_seen_before_first_seen_rejected(db_session: AsyncSession) -> None:
    tenant, project, asset, scan = await _make_scan(db_session, "acme4")
    repo = FindingRepository(db_session)
    finding = await repo.record_detection(
        FindingCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scan_id=scan.id,
            title="A",
            severity=FindingSeverity.LOW,
            source_tool="zap",
            fingerprint="fp-c",
        )
    )

    finding.last_seen_at = finding.first_seen_at - timedelta(days=1)
    with pytest.raises(IntegrityError):
        await db_session.flush()
