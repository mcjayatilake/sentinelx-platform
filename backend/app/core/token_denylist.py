"""Access-token logout denylist.

Access tokens are short-lived, stateless JWTs — "logout" can't invalidate
one by deleting a database row, because there isn't one. Instead, logout
adds the token's `jti` to Redis with a TTL equal to the token's remaining
lifetime; `app.api.deps.get_current_user` checks it on every request.
This is deliberately separate from refresh-token revocation (DB-backed,
see `app.repositories.refresh_token_repository`) and from
`User.token_version` (bulk invalidation) — three mechanisms for three
different revocation needs, see docs/decisions/0003-jwt-strategy.md.
"""

from app.core.redis_client import get_redis_client

_KEY_PREFIX = "auth:denylist:jti:"


async def deny_token(jti: str, ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    client = get_redis_client()
    await client.set(f"{_KEY_PREFIX}{jti}", "1", ex=ttl_seconds)


async def is_token_denied(jti: str) -> bool:
    client = get_redis_client()
    return bool(await client.exists(f"{_KEY_PREFIX}{jti}"))
