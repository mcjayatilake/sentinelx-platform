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
    jwt_secret_key: str = "insecure-development-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_issuer: str = "sentinelx.io"

    # Object storage (S3-compatible)
    s3_endpoint_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key_id: str = "sentinelx"
    s3_secret_access_key: str = "sentinelx"
    s3_bucket_name: str = "sentinelx-artifacts"
    s3_use_ssl: bool = False

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
