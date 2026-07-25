"""Redis-backed rate limiting on auth-sensitive endpoints."""

import redis.asyncio as redis
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rate_limit import rate_limit
from tests.auth_helpers import create_tenant_user_membership


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in for `starlette.requests.Request` — `rate_limit`'s
    dependency only reads `.client.host` and `.headers`."""

    def __init__(self, host: str) -> None:
        self.client = _FakeClient(host)
        self.headers: dict[str, str] = {}


PASSWORD = "Correct-Horse-Battery-Staple-9!"


async def test_login_is_rate_limited_after_configured_max_requests(
    auth_client: AsyncClient, db_session: AsyncSession, redis_client: redis.Redis
) -> None:
    await create_tenant_user_membership(
        db_session,
        email="ratelimited@example.com",
        password=PASSWORD,
        tenant_slug="rate-limit-tenant",
    )
    max_requests = get_settings().rate_limit_login_per_minute

    statuses = []
    for _ in range(max_requests + 2):
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "ratelimited@example.com", "password": "wrong-password"},
        )
        statuses.append(response.status_code)

    assert statuses[:max_requests] == [401] * max_requests
    assert statuses[max_requests] == 429
    assert statuses[max_requests + 1] == 429


async def test_rate_limit_key_is_scoped_per_endpoint_name(redis_client: redis.Redis) -> None:
    login_limit = rate_limit("login", max_requests=1)
    refresh_limit = rate_limit("refresh", max_requests=1)

    await login_limit(_FakeRequest("1.2.3.4"))  # type: ignore[arg-type]
    # A different endpoint name must not share the same budget, even for
    # the same client — otherwise one endpoint's traffic could exhaust
    # another's limit.
    await refresh_limit(_FakeRequest("1.2.3.4"))  # type: ignore[arg-type]

    keys = [key async for key in redis_client.scan_iter(match="auth:ratelimit:*")]
    assert any("login" in key for key in keys)
    assert any("refresh" in key for key in keys)


async def test_rate_limit_exceeded_raises_429(redis_client: redis.Redis) -> None:
    limited = rate_limit("unit-test-endpoint", max_requests=2)
    await limited(_FakeRequest("9.9.9.9"))  # type: ignore[arg-type]
    await limited(_FakeRequest("9.9.9.9"))  # type: ignore[arg-type]

    try:
        await limited(_FakeRequest("9.9.9.9"))  # type: ignore[arg-type]
        raise AssertionError("expected HTTPException(429)")
    except HTTPException as exc:
        assert exc.status_code == 429


async def test_rate_limit_key_is_scoped_per_client_ip(redis_client: redis.Redis) -> None:
    limited = rate_limit("per-ip-endpoint", max_requests=1)
    await limited(_FakeRequest("10.0.0.1"))  # type: ignore[arg-type]
    # A different client must get its own budget.
    await limited(_FakeRequest("10.0.0.2"))  # type: ignore[arg-type]
