"""Redis-backed fixed-window rate limiting for auth-sensitive endpoints.

Usage: `Depends(rate_limit("login", max_requests=settings.rate_limit_login_per_minute))`.
Keyed by client IP + a caller-supplied name, so different endpoints don't
share a budget and two endpoints named the same can't collide with
different limits by accident (the name is part of the Redis key).
"""

from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from app.core.redis_client import get_redis_client

_KEY_PREFIX = "auth:ratelimit:"


def get_client_ip(request: Request) -> str | None:
    """The caller's IP, honoring the first `X-Forwarded-For` entry for
    deployments behind a reverse proxy. Shared by rate limiting (as the
    bucket key) and `app.services.auth_service.RequestContext` (as a
    forensic field on audit events and refresh-token rows) so the two
    never disagree about which address a request came from."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def rate_limit(
    name: str, max_requests: int, window_seconds: int = 60
) -> Callable[[Request], Awaitable[None]]:
    """Returns a FastAPI dependency enforcing `max_requests` per
    `window_seconds` per client, identified by IP (or the first
    `X-Forwarded-For` entry, for deployments behind a reverse proxy)."""

    async def _dependency(request: Request) -> None:
        client = get_redis_client()
        key = f"{_KEY_PREFIX}{name}:{get_client_ip(request) or 'unknown'}"

        current = await client.incr(key)
        if current == 1:
            await client.expire(key, window_seconds)

        if current > max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Please try again later.",
            )

    return _dependency
