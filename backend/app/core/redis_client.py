"""Shared async Redis client.

One module-level connection, created lazily and reused — mirrors the
module-level SQLAlchemy `engine` in `app.db.session`. Used by rate
limiting (`app.core.rate_limit`) and the access-token logout denylist
(`app.core.token_denylist`).
"""

import redis.asyncio as redis

from app.core.config import get_settings

_client: redis.Redis | None = None


def get_redis_client() -> redis.Redis:
    global _client
    if _client is None:
        settings = get_settings()
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


async def close_redis_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
