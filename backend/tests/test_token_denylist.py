"""Access-token logout denylist (app.core.token_denylist)."""

import redis.asyncio as redis

from app.core.token_denylist import deny_token, is_token_denied


async def test_deny_and_check_round_trip(redis_client: redis.Redis) -> None:
    jti = "test-jti-1"
    assert await is_token_denied(jti) is False

    await deny_token(jti, ttl_seconds=60)
    assert await is_token_denied(jti) is True


async def test_deny_token_with_non_positive_ttl_is_a_no_op(redis_client: redis.Redis) -> None:
    jti = "test-jti-already-expired"
    await deny_token(jti, ttl_seconds=0)
    assert await is_token_denied(jti) is False

    await deny_token(jti, ttl_seconds=-5)
    assert await is_token_denied(jti) is False
