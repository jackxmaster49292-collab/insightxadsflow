"""Application rate limits and Telegram pacing buckets.

Two distinct things live here and must not be confused:

* :func:`check_rate_limit` protects *this application* (login brute force, sync
  spam). Documented in docs/SECURITY.md §8.
* :class:`PacingBucket` shapes *outbound Telegram traffic* toward the documented
  platform limits. It is orderly pacing, never randomized evasion, and a
  Telegram-provided flood wait always overrides it.

Both degrade open on Redis failure: a rate limiter that takes the product down
when Redis blips is worse than one that briefly allows extra requests.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import redis.asyncio as redis
import structlog

from app.config import get_settings

log = structlog.get_logger(__name__)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


@dataclass(frozen=True, slots=True)
class RateLimit:
    limit: int
    window_s: int


#: docs/SECURITY.md §8
LIMITS: dict[str, RateLimit] = {
    "login": RateLimit(5, 15 * 60),
    "register": RateLimit(3, 60 * 60),
    "connection_create": RateLimit(5, 60 * 60),
    "chat_sync": RateLimit(4, 60 * 60),
    "rule_create": RateLimit(30, 60 * 60),
    "control": RateLimit(60, 60),
    "default": RateLimit(600, 60),
}


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_s: int


async def check_rate_limit(bucket: str, identity: str) -> RateLimitResult:
    """Fixed-window counter. Called once per protected request."""
    limit = LIMITS.get(bucket, LIMITS["default"])
    key = f"rl:{bucket}:{identity}"
    try:
        client = get_redis()
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.ttl(key)
            count, ttl = await pipe.execute()
        if ttl is None or ttl < 0:
            await client.expire(key, limit.window_s)
            ttl = limit.window_s
    except Exception as exc:  # pragma: no cover - infrastructure path
        log.warning("rate_limit_unavailable", bucket=bucket, error=exc)
        return RateLimitResult(True, limit.limit, 0)

    if count > limit.limit:
        return RateLimitResult(False, 0, int(ttl))
    return RateLimitResult(True, limit.limit - int(count), int(ttl))


async def reset_rate_limit(bucket: str, identity: str) -> None:
    # Clearing a limiter is best-effort: if Redis is down the counter is already
    # unreachable, and failing here would be noise, not signal.
    with contextlib.suppress(Exception):  # pragma: no cover - infrastructure path
        await get_redis().delete(f"rl:{bucket}:{identity}")


class PacingBucket:
    """Token bucket for outbound Telegram traffic.

    Defaults come from Telegram's published bot guidance: roughly one message
    per second to a single chat, 20 messages per minute in a group, and about 30
    messages per second overall. These are *ceilings we stay under*, not limits
    we try to work around.
    """

    def __init__(self, key: str, *, capacity: int, refill_per_s: float) -> None:
        self.key = f"pace:{key}"
        self.capacity = capacity
        self.refill_per_s = refill_per_s

    _LUA = """
    local key = KEYS[1]
    local capacity = tonumber(ARGV[1])
    local refill = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local state = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = tonumber(state[1])
    local ts = tonumber(state[2])
    if tokens == nil then tokens = capacity; ts = now end
    tokens = math.min(capacity, tokens + (now - ts) * refill)
    local wait = 0
    if tokens >= 1 then
      tokens = tokens - 1
    else
      wait = (1 - tokens) / refill
    end
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', key, 3600)
    return tostring(wait)
    """

    async def acquire(self, *, now_s: float) -> float:
        """Returns the number of seconds the caller should wait before sending."""
        try:
            client = get_redis()
            result = await client.eval(
                self._LUA, 1, self.key, self.capacity, self.refill_per_s, now_s
            )
            return max(0.0, float(result))
        except Exception as exc:  # pragma: no cover - infrastructure path
            log.warning("pacing_unavailable", key=self.key, error=exc)
            return 0.0


def destination_bucket(connection_id: str, peer_key: str, *, is_group: bool) -> PacingBucket:
    if is_group:
        # 20 messages per minute in a group.
        return PacingBucket(f"{connection_id}:{peer_key}", capacity=20, refill_per_s=20 / 60)
    # ~1 message per second to a single chat.
    return PacingBucket(f"{connection_id}:{peer_key}", capacity=5, refill_per_s=1.0)


def connection_bucket(connection_id: str) -> PacingBucket:
    # ~30 messages per second overall for a bot; conservative for user accounts.
    return PacingBucket(f"conn:{connection_id}", capacity=30, refill_per_s=30.0)
