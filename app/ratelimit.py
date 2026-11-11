"""按模型独立的令牌桶限流（异步安全）。

每个模型别名一个桶，互不影响：A 打满不会拖垮 B。
超限不排队等待，直接抛 rate_limited -> HTTP 429（带 Retry-After）。
"""
from __future__ import annotations

import asyncio
import time

from .errors import GatewayError


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int):
        self.rate = max(rate_per_sec, 1e-9)
        self.capacity = max(int(capacity), 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    def retry_after(self) -> float:
        """距下一个令牌可用还有多少秒。"""
        need = 1.0 - self._tokens
        return max(need / self.rate, 0.01)

    async def try_acquire(self, cost: int = 1) -> None:
        async with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self._tokens < cost:
                raise GatewayError(
                    "rate_limited",
                    f"模型级限流触发（容量 {self.capacity}，速率 {self.rate*60:.0f}/min）",
                    details={"retry_after_s": round(self.retry_after(), 2)},
                )
            self._tokens -= cost


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}

    def register(self, alias: str, rpm: int, burst: int) -> None:
        self._buckets[alias] = TokenBucket(rate_per_sec=rpm / 60.0, capacity=burst)

    async def acquire(self, alias: str, cost: int = 1) -> None:
        bucket = self._buckets.get(alias)
        if bucket is None:
            return
        await bucket.try_acquire(cost)

    def stats(self) -> dict[str, dict]:
        out = {}
        for alias, b in self._buckets.items():
            b._refill(time.monotonic())
            out[alias] = {
                "capacity": b.capacity,
                "rate_per_min": round(b.rate * 60, 2),
                "available_tokens": round(b._tokens, 2),
            }
        return out
