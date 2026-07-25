"""Shared enumerations for persistence models.

All enums are mapped via SQLAlchemy's `Enum(..., native_enum=False, create_constraint=True)`, which
renders as `VARCHAR` plus an auto-generated `CHECK` constraint rather than a
native PostgreSQL `ENUM` type. This keeps adding a new value a plain,
transactional `ALTER TABLE ... DROP/ADD CONSTRAINT` migration instead of the
non-transactional `ALTER TYPE ... ADD VALUE` that native enums require.
"""

from enum import StrEnum


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class MembershipRole(StrEnum):
    OWNER = "owner"
    ADMINISTRATOR = "administrator"
    SECURITY_ANALYST = "security_analyst"
    DEVELOPER = "developer"
    VIEWER = "viewer"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"
    REMOVED = "removed"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AssetType(StrEnum):
    WEBSITE = "website"
    API = "api"
    REPOSITORY = "repository"
    CONTAINER_IMAGE = "container_image"
    INFRASTRUCTURE = "infrastructure"
    DOMAIN = "domain"
    IP_RANGE = "ip_range"


class AssetEnvironment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    TEST = "test"
    UNKNOWN = "unknown"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    DECOMMISSIONED = "decommissioned"


class AuthorizationStatus(StrEnum):
    UNAUTHORIZED = "unauthorized"
    PENDING = "pending"
    AUTHORIZED = "authorized"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ScanStatus(StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FindingSeverity(StrEnum):
    INFORMATIONAL = "informational"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    ACCEPTED_RISK = "accepted_risk"
    FALSE_POSITIVE = "false_positive"


class FindingConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CONFIRMED = "confirmed"


class FindingReferenceType(StrEnum):
    CVE = "cve"
    CWE = "cwe"
    OWASP = "owasp"
    ADVISORY_URL = "advisory_url"
    VENDOR_ID = "vendor_id"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


class APIKeyStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class RefreshTokenStatus(StrEnum):
    ACTIVE = "active"
    ROTATED = "rotated"
    REVOKED = "revoked"


class VerificationTokenPurpose(StrEnum):
    EMAIL_VERIFICATION = "email_verification"
    PASSWORD_RESET = "password_reset"
