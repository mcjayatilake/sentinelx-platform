"""Health and readiness endpoints, used for uptime checks and Kubernetes probes."""

from datetime import UTC, datetime
from typing import Literal

import redis.asyncio as redis
from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import DbSessionDep, SettingsDep
from app.core.logging import get_logger

router = APIRouter(tags=["health"])
logger = get_logger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str
    environment: str
    timestamp: datetime


class DependencyStatus(BaseModel):
    database: Literal["ok", "unavailable"]
    redis: Literal["ok", "unavailable"]


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded"]
    dependencies: DependencyStatus
    timestamp: datetime


@router.get("/health", response_model=HealthResponse, summary="Liveness / basic health check")
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.project_name,
        environment=settings.environment,
        timestamp=datetime.now(UTC),
    )


@router.get("/health/live", response_model=HealthResponse, summary="Kubernetes liveness probe")
async def liveness(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.project_name,
        environment=settings.environment,
        timestamp=datetime.now(UTC),
    )


@router.get("/health/ready", response_model=ReadinessResponse, summary="Kubernetes readiness probe")
async def readiness(settings: SettingsDep, db: DbSessionDep) -> ReadinessResponse:
    database_status: Literal["ok", "unavailable"] = "ok"
    redis_status: Literal["ok", "unavailable"] = "ok"

    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        logger.warning("readiness_check.database_unavailable", exc_info=True)
        database_status = "unavailable"

    redis_client = redis.from_url(settings.redis_url)
    try:
        await redis_client.ping()
    except Exception:
        logger.warning("readiness_check.redis_unavailable", exc_info=True)
        redis_status = "unavailable"
    finally:
        await redis_client.aclose()

    overall = "ok" if database_status == "ok" and redis_status == "ok" else "degraded"

    return ReadinessResponse(
        status=overall,
        dependencies=DependencyStatus(database=database_status, redis=redis_status),
        timestamp=datetime.now(UTC),
    )
