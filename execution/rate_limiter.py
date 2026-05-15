"""
execution/rate_limiter.py

Token-bucket rate limiter with exponential backoff on HTTP 429 responses.

Kalshi uses separate buckets for read and write operations.
This module tracks both and blocks callers until a token is available,
rather than silently dropping requests.
"""

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Awaitable

import config
from logging_.structured_logger import logger


class BucketType(str, Enum):
    READ  = "read"
    WRITE = "write"


@dataclass
class _TokenBucket:
    tokens_per_second: float
    max_tokens: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens      = self.max_tokens
        self._last_refill = time.monotonic()

    def consume(self) -> float:
        """
        Consume one token. Returns 0.0 immediately if a token is available,
        or the number of seconds the caller must wait before retrying.
        """
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(
            self.max_tokens,
            self._tokens + elapsed * self.tokens_per_second,
        )
        self._last_refill = now

        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return 0.0

        # Time until next token arrives
        return (1.0 - self._tokens) / self.tokens_per_second


class RateLimiter:
    """
    Async-safe rate limiter wrapping two token buckets (read / write).

    Usage:
        limiter = RateLimiter()

        async with limiter.throttle(BucketType.WRITE):
            response = await http_client.post(...)
            if response.status == 429:
                await limiter.on_429(BucketType.WRITE)
    """

    def __init__(self) -> None:
        self._buckets: dict[BucketType, _TokenBucket] = {
            BucketType.READ: _TokenBucket(
                tokens_per_second=config.RATE_LIMIT_READ_TOKENS_PER_SECOND,
                max_tokens=config.RATE_LIMIT_READ_TOKENS_PER_SECOND * 2,
            ),
            BucketType.WRITE: _TokenBucket(
                tokens_per_second=config.RATE_LIMIT_WRITE_TOKENS_PER_SECOND,
                max_tokens=config.RATE_LIMIT_WRITE_TOKENS_PER_SECOND * 2,
            ),
        }
        self._backoff_state: dict[BucketType, float] = {
            BucketType.READ:  config.RATE_LIMIT_INITIAL_BACKOFF_SECONDS,
            BucketType.WRITE: config.RATE_LIMIT_INITIAL_BACKOFF_SECONDS,
        }
        self._lock = asyncio.Lock()

    async def acquire(self, bucket: BucketType) -> None:
        """
        Block until a token is available in the specified bucket.
        Should be called before every outbound API request.
        """
        while True:
            async with self._lock:
                wait = self._buckets[bucket].consume()

            if wait <= 0:
                return

            logger.debug(
                f"rate limiter: waiting {wait:.3f}s for {bucket} token",
                bucket=bucket,
                wait_seconds=round(wait, 3),
            )
            await asyncio.sleep(wait)

    async def on_429(self, bucket: BucketType) -> None:
        """
        Call this when the exchange returns HTTP 429.
        Applies exponential backoff with full jitter and resets once cleared.
        """
        delay = self._backoff_state[bucket]
        jitter = random.uniform(0, delay * 0.2)
        total_wait = min(delay + jitter, config.RATE_LIMIT_MAX_BACKOFF_SECONDS)

        logger.warning(
            f"429 received on {bucket} bucket — backing off {total_wait:.1f}s",
            bucket=bucket,
            backoff_seconds=round(total_wait, 2),
        )
        await asyncio.sleep(total_wait)

        # Double the backoff for next time (exponential), capped at max
        self._backoff_state[bucket] = min(
            delay * 2,
            config.RATE_LIMIT_MAX_BACKOFF_SECONDS,
        )

    def reset_backoff(self, bucket: BucketType) -> None:
        """Call after a successful response to reset the backoff counter."""
        self._backoff_state[bucket] = config.RATE_LIMIT_INITIAL_BACKOFF_SECONDS

    class _ThrottleContext:
        """Async context manager returned by RateLimiter.throttle()."""
        def __init__(self, limiter: "RateLimiter", bucket: BucketType) -> None:
            self._limiter = limiter
            self._bucket  = bucket

        async def __aenter__(self) -> None:
            await self._limiter.acquire(self._bucket)

        async def __aexit__(self, *_) -> None:
            pass

    def throttle(self, bucket: BucketType) -> "_ThrottleContext":
        """
        Async context manager that acquires a token before the block runs.

        Example:
            async with limiter.throttle(BucketType.WRITE):
                resp = await session.post(url, json=payload, headers=headers)
        """
        return self._ThrottleContext(self, bucket)
