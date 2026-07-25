"""Application configuration.

All runtime configuration must flow through `Settings` below; do not read
`os.environ` directly elsewhere in the codebase.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # General
    environment: Literal["local", "development", "staging", "production"] = "local"
    debug: bool = True
    project_name: str = "SentinelX"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # Backend
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    backend_cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://sentinelx:sentinelx@localhost:5432/sentinelx"
    database_url_sync: str = "postgresql+psycopg://sentinelx:sentinelx@localhost:5432/sentinelx"
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Celery
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    celery_task_always_eager: bool = False

    # JWT / OAuth2
    # Access and refresh tokens are signed with independent secrets, so a
    # refresh token can never be replayed as an access token even if a
    # future code path forgot to check the `type` claim.
    jwt_secret_key: str = "insecure-development-secret-change-me"
    jwt_refresh_secret_key: str = "insecure-development-refresh-secret-change-me"
    jwt_algorithm: str = "HS256"
    # `kid` header values, distinct per token type. Not used to select a
    # key today (single key per type) — carried so multi-key verification
    # (rotation, JWKS) can be added later without changing token shape.
    jwt_access_key_id: str = "sentinelx-access-1"
    jwt_refresh_key_id: str = "sentinelx-refresh-1"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_issuer: str = "sentinelx.io"

    # Password policy
    password_min_length: int = 12
    password_require_uppercase: bool = True
    password_require_lowercase: bool = True
    password_require_digit: bool = True
    password_require_symbol: bool = True

    # Argon2id parameters. Explicit, not left to library defaults, so a
    # passlib/argon2-cffi upgrade can't silently change the work factor.
    # Baseline follows current OWASP guidance; tune per deployment.
    argon2_time_cost: int = 3
    argon2_memory_cost_kib: int = 65536
    argon2_parallelism: int = 4

    # Email verification / password reset tokens
    email_verification_token_expire_hours: int = 24
    password_reset_token_expire_minutes: int = 30
    frontend_base_url: str = "http://localhost:3000"

    # Rate limiting — auth-sensitive endpoints (requests per 60s window per
    # client key; enforced via Redis, see app.core.rate_limit)
    rate_limit_login_per_minute: int = 5
    rate_limit_refresh_per_minute: int = 10
    rate_limit_password_reset_per_minute: int = 3
    rate_limit_api_key_create_per_minute: int = 5

    # Security response headers
    security_headers_enabled: bool = True
    security_hsts_max_age_seconds: int = 63072000  # 2 years
    security_csp_policy: str = "default-src 'self'"

    # Object storage (S3-compatible)
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key_id: str = "sentinelx"
    s3_secret_access_key: str = "sentinelx"
    s3_bucket_name: str = "sentinelx-artifacts"
    s3_use_ssl: bool = False

    # Scan engine — orchestration defaults (app.scan_engine); see
    # docs/scan-engine.md and docs/orchestrator.md.
    scan_default_timeout_seconds: int = 1800
    scan_default_max_attempts: int = 3
    scan_retry_backoff_base_seconds: float = 5.0
    scan_retry_backoff_max_seconds: float = 300.0
    scan_config_max_bytes: int = 16384
    scan_event_metadata_max_bytes: int = 8192
    scan_queue_name: str = "scans"
    scan_dead_letter_queue_name: str = "scans.dead_letter"
    # Outbox dispatcher (app.workers.tasks.outbox_dispatcher) — see
    # docs/decisions/0008-transaction-and-concurrency-model.md.
    scan_outbox_dispatch_interval_seconds: float = 2.0
    scan_outbox_dispatch_batch_size: int = 50

    # Scan artifact storage — see app.scan_engine.storage
    artifact_storage_backend: Literal["local"] = "local"
    artifact_storage_local_path: str = "./var/scan-artifacts"

    # Observability
    sentry_dsn: str | None = None
    otel_exporter_otlp_endpoint: str | None = None

    # Security
    rate_limit_per_minute: int = 60
    allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached `Settings` instance.

    Cached because `Settings()` re-reads and re-validates the environment
    on every instantiation; the process environment does not change at
    runtime.
    """
    return Settings()
