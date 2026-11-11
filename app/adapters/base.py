"""适配器抽象基类（统一抽象层的核心）。

适配器只负责"协议翻译"，不关心鉴权以外的东西：
限流 / 重试 / 模板 / 观测 全部由 Gateway 统一编排。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..config import UpstreamSpec
from ..models import Message, ResponseFormat, Usage


@dataclass
class ResolvedRequest:
    """适配器收到的、已归一化的请求。"""
    model: str                       # 上游真实模型名
    system: str | None
    messages: list[Message]
    temperature: float
    max_output_tokens: int
    stream: bool
    response_format: ResponseFormat | None = None


@dataclass
class RawResult:
    text: str = ""
    structured: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    upstream_ms: float | None = None
    first_token_ms: float | None = None
    attempts: int = 1


class BaseAdapter(ABC):
    protocol: str = "base"

    def __init__(self, upstream: UpstreamSpec) -> None:
        self.upstream = upstream

    # ---- 子类必须实现 ----
    @abstractmethod
    def endpoint(self) -> str: ...

    @abstractmethod
    def headers(self) -> dict[str, str]: ...

    @abstractmethod
    def build_payload(self, req: ResolvedRequest) -> dict[str, Any]: ...

    @abstractmethod
    def parse_response(self, payload: dict) -> RawResult: ...

    @abstractmethod
    def parse_stream_chunk(self, payload: dict) -> tuple[str, Usage | None, str | None]:
        """解析一个 SSE data 块，返回 (text_delta, usage_or_None, finish_reason)。"""
        ...

    @abstractmethod
    async def call(self, req: ResolvedRequest) -> RawResult: ...

    @abstractmethod
    async def stream(self, req: ResolvedRequest) -> AsyncIterator[tuple[str, Usage | None, str | None, int]]:
        """产出 (text_delta, usage, finish_reason, attempts)。"""
        ...

    # ---- 公共小工具 ----
    def full_url(self) -> str:
        return self.endpoint()

    @staticmethod
    def _timer() -> float:
        return time.perf_counter()

    @staticmethod
    def _ms(start: float) -> float:
        return round((time.perf_counter() - start) * 1000, 2)
