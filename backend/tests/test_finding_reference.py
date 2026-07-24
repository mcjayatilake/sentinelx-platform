"""FindingReference repository: cross-tenant protection, per-finding uniqueness."""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, FindingReferenceType, FindingSeverity
from app.repositories.asset_repository import AssetRepository
from app.repositories.finding_reference_repository import (
    FindingNotFoundError,
    FindingReferenceRepository,
)
from app.repositories.finding_repository import FindingRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.scan_repository import ScanRepository
from app.repositories.tenant_repository import TenantRepository
from app.schemas.asset import AssetCreate
from app.schemas.finding import FindingCreate
from app.schemas.finding_reference import FindingReferenceCreate
from app.schemas.project import ProjectCreate
from app.schemas.scan import ScanCreate
from app.schemas.tenant import TenantCreate


async def _make_finding(session: AsyncSession, slug: str):
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
    finding = await FindingRepository(session).record_detection(
        FindingCreate(
            tenant_id=tenant.id,
            project_id=project.id,
            asset_id=asset.id,
            scan_id=scan.id,
            title="SQLi",
            severity=FindingSeverity.CRITICAL,
            source_tool="zap",
            fingerprint=f"fp-{slug}",
        )
    )
    return tenant, finding


async def test_create_reference(db_session: AsyncSession) -> None:
    tenant, finding = await _make_finding(db_session, "acme")
    repo = FindingReferenceRepository(db_session)

    ref = await repo.create(
        tenant.id,
        FindingReferenceCreate(
            finding_id=finding.id, reference_type=FindingReferenceType.CVE, value="CVE-2024-1234"
        ),
    )
    assert ref.finding_id == finding.id
    assert ref.tenant_id == tenant.id


async def test_create_rejects_wrong_tenant(db_session: AsyncSession) -> None:
    _, finding = await _make_finding(db_session, "acme2")
    other_tenant = await TenantRepository(db_session).create(
        TenantCreate(name="Other", slug="other-tenant")
    )
    repo = FindingReferenceRepository(db_session)

    with pytest.raises(FindingNotFoundError):
        await repo.create(
            other_tenant.id,
            FindingReferenceCreate(
                finding_id=finding.id,
                reference_type=FindingReferenceType.CVE,
                value="CVE-2024-9999",
            ),
        )


async def test_duplicate_reference_rejected(db_session: AsyncSession) -> None:
    tenant, finding = await _make_finding(db_session, "acme3")
    repo = FindingReferenceRepository(db_session)
    await repo.create(
        tenant.id,
        FindingReferenceCreate(
            finding_id=finding.id, reference_type=FindingReferenceType.CWE, value="CWE-79"
        ),
    )

    with pytest.raises(IntegrityError):
        await repo.create(
            tenant.id,
            FindingReferenceCreate(
                finding_id=finding.id, reference_type=FindingReferenceType.CWE, value="CWE-79"
            ),
        )
