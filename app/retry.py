"""指数退避重试：最多 3 次尝试，仅对可重试错误生效。

delay = min(base * 2^(n-1), max_delay) + jitter
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

from .errors import GatewayError

logger = logging.getLogger("gateway.retry")
T = TypeVar("T")


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    label: str = "",
) -> tuple[T, int, list[dict]]:
    """返回 (结果, 尝试次数, 重试明细)。"""
    history: list[dict] = []
    last: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await fn()
            return result, attempt, history
        except GatewayError as exc:
            last = exc
            if not exc.retryable or attempt == max_attempts:
                exc.details = exc.details if isinstance(exc.details, dict) else {"raw": exc.details}
                exc.details["attempts"] = attempt
                exc.details["retry_history"] = history
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0, base_delay * 0.25)
            history.append({
                "attempt": attempt,
                "code": exc.code,
                "upstream_status": exc.upstream_status,
                "sleep_s": round(delay, 3),
            })
            logger.warning("%s 第 %d 次失败(%s)，%.2fs 后退避重试", label, attempt, exc.code, delay)
            await asyncio.sleep(delay)
        except Exception as exc:  # 网络异常等：视为可重试
            last = exc
            if attempt == max_attempts:
                raise GatewayError("upstream_unavailable", f"{type(exc).__name__}: {exc}") from exc
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay) + random.uniform(0, base_delay * 0.25)
            history.append({"attempt": attempt, "code": type(exc).__name__, "sleep_s": round(delay, 3)})
            await asyncio.sleep(delay)

    raise GatewayError("upstream_unavailable", str(last))
