"""Scan schemas.

`ScanCreateRequest` is the API-facing input (no `tenant_id`/
`requested_by_user_id` — those come from `CurrentPrincipalDep`, never a
client-supplied value); `ScanCreate` is the repository-facing input
(`tenant_id` required, per the tenant-owned-repository rule). `job_id` is
deliberately absent from `ScanRead` — it's the underlying job-queue task
id, an internal operational detail, not part of the public contract.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import ScanStatus
from app.models.scan import SCAN_CONFIG_MAX_BYTES


def _check_config_size(value: dict[str, Any] | None) -> dict[str, Any] | None:
    size = len(json.dumps(value).encode()) if value is not None else 0
    if size > SCAN_CONFIG_MAX_BYTES:
        raise ValueError(f"config must not exceed {SCAN_CONFIG_MAX_BYTES} bytes")
    return value


class ScanCreateRequest(BaseModel):
    """Request body for `POST /scans`."""

    project_id: UUID
    asset_id: UUID
    scanner_type: str
    config: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    max_attempts: int | None = None

    @field_validator("config")
    @classmethod
    def _validate_config_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_config_size(value)

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("timeout_seconds must be positive")
        return value

    @field_validator("max_attempts")
    @classmethod
    def _validate_max_attempts(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("max_attempts must be at least 1")
        return value


class ScanCreate(BaseModel):
    tenant_id: UUID
    project_id: UUID
    asset_id: UUID
    scanner_type: str
    requested_by_user_id: UUID | None = None
    config: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    max_attempts: int | None = None
    retry_of_scan_id: UUID | None = None
    correlation_id: str | None = None

    @field_validator("config")
    @classmethod
    def _validate_config_size(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return _check_config_size(value)


class ScanUpdate(BaseModel):
    status: ScanStatus | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    cancelled_at: datetime | None = None
    timed_out_at: datetime | None = None
    error_code: str | None = None
    error_summary: str | None = None
    attempt: int | None = None
    worker_id: str | None = None
    job_id: str | None = None


class ScanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    project_id: UUID
    asset_id: UUID
    scanner_type: str
    status: ScanStatus
    requested_by_user_id: UUID | None
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    failed_at: datetime | None
    cancelled_at: datetime | None
    timed_out_at: datetime | None
    error_code: str | None
    error_summary: str | None
    attempt: int
    max_attempts: int
    worker_id: str | None
    timeout_seconds: int
    retry_of_scan_id: UUID | None
    config: dict[str, Any] | None
    correlation_id: str | None
    created_at: datetime
    updated_at: datetime


class ScanProgressRead(BaseModel):
    scan_id: UUID
    status: ScanStatus
    stage: str
    percent: int
    started_at: datetime | None
    estimated_completion_at: datetime | None
    worker_id: str | None
    updated_at: datetime
