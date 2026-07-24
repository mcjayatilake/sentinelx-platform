"""SQLAlchemy ORM models.

Every model is imported here so `Base.metadata` (and therefore Alembic
autogeneration) discovers all tables from a single entry point.
"""

from app.models.api_key import APIKeyMetadata
from app.models.asset import Asset
from app.models.audit_event import AuditEvent
from app.models.finding import Finding
from app.models.finding_reference import FindingReference
from app.models.membership import TenantMembership
from app.models.project import Project
from app.models.scan import Scan
from app.models.tenant import Tenant
from app.models.user import User

__all__ = [
    "APIKeyMetadata",
    "Asset",
    "AuditEvent",
    "Finding",
    "FindingReference",
    "TenantMembership",
    "Project",
    "Scan",
    "Tenant",
    "User",
]
