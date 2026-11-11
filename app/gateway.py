"""网关编排层：统一抽象层的"粘合剂"。

一次调用的顺序：
  1. 路由        alias -> ModelRoute -> 适配器（按 model 字段动态分发）
  2. 限流        按模型独立令牌桶，超限 -> 429
  3. 模板        渲染（name / name@version / name@latest）
  4. 调用        非流式 or 流式（SSE），内部带指数退避重试
  5. 结构化      按 response_format 解析 + JSON Schema 校验
  6. 观测        Token 分类统计 + 延迟（含首 Token）
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

import jsonschema

from .adapters.anthropic_messages import AnthropicMessagesAdapter
from .adapters.base import BaseAdapter, RawResult, ResolvedRequest
from .adapters.openai_chat import OpenAIChatAdapter
from .adapters.openai_responses import OpenAIResponsesAdapter
from .config import Settings
from .errors import GatewayError
from .models import (
    Latency, Message, ResponseFormat, StreamEvent, TemplateRef, UnifiedRequest,
    UnifiedResponse, Usage,
)
from .observability import CallRecord, MetricsStore, latency_to_dict, usage_to_dict
from .ratelimit import RateLimiter
from .templates import TemplateStore

logger = logging.getLogger("gateway")


def build_adapter(protocol: str, upstream, settings: Settings) -> BaseAdapter:
    kw = dict(max_attempts=settings.max_attempts,
              base_delay=settings.retry_base_delay,
              max_delay=settings.retry_max_delay)
    if protocol == "openai_responses":
        return OpenAIResponsesAdapter(upstream, **kw)
    if protocol == "openai_chat_completions":
        return OpenAIChatAdapter(upstream, **kw)
    if protocol == "anthropic_messages":
        return AnthropicMessagesAdapter(upstream, **kw)
    raise GatewayError("invalid_request", f"不支持的协议：{protocol}")


class Gateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.adapters: dict[str, BaseAdapter] = {
            alias: build_adapter(route.upstream.protocol, route.upstream, settings)
            for alias, route in settings.routes.items()
        }
        self.limiter = RateLimiter()
        for alias, route in settings.routes.items():
            self.limiter.register(alias, route.rate_limit.requests_per_minute, route.rate_limit.burst)
        self.templates = TemplateStore(settings.prompts_dir)
        self.metrics = MetricsStore(settings.log_dir)

    # ---------------- 请求归一化 ----------------
    def _resolve_prompt(self, req: UnifiedRequest) -> tuple[list[Message], str | None, TemplateRef | None]:
        system = req.system
        messages: list[Message] = []
        if req.input is None:
            pass
        elif isinstance(req.input, str):
            messages = [Message(role="user", content=req.input)]
        else:
            messages = list(req.input)

        tref: TemplateRef | None = None
        if req.template:
            ver_doc, version, content, tpl_system = self.templates.resolve(req.template, req.variables)
            if tpl_system:
                system = f"{tpl_system}\n{system}" if system else tpl_system
            # 模板渲染出的正文作为首条 user 消息，用户自带 input 追加在后
            messages = [Message(role="user", content=content)] + messages
            tref = TemplateRef(name=req.template.split("@")[0],
                               version=version, rendered_chars=len(content))
        return messages, system, tref

    def _to_resolved(self, req: UnifiedRequest, route, messages: list[Message],
                     system: str | None, stream: bool) -> ResolvedRequest:
        return ResolvedRequest(
            model=route.upstream.model,
            system=system,
            messages=messages,
            temperature=req.temperature,
            max_output_tokens=req.max_output_tokens,
            stream=stream,
            response_format=req.response_format,
        )

    # ---------------- 结构化输出校验 ----------------
    @staticmethod
    def _apply_structured(text: str, rf: ResponseFormat | None,
                          preset: dict | None = None) -> dict[str, Any] | None:
        if rf is None:
            return preset
        data = preset
        if data is None:
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise GatewayError("structured_output_invalid",
                                   f"模型输出不是合法 JSON：{exc}",
                                   details={"raw_head": text[:300]}) from None
        if rf.type == "json_schema" and rf.json_schema:
            try:
                jsonschema.validate(instance=data, schema=rf.json_schema)
            except jsonschema.ValidationError as exc:
                raise GatewayError("structured_output_invalid", f"输出不满足 JSON Schema：{exc.message}",
                                   details={"path": list(exc.absolute_path)}) from None
        return data

    # ---------------- 非流式 ----------------
    async def chat(self, req: UnifiedRequest, request_id: str) -> UnifiedResponse:
        t0 = time.perf_counter()
        route = self.settings.route(req.model)
        adapter = self.adapters[route.alias]
        tref: TemplateRef | None = None

        try:
            await self.limiter.acquire(route.alias)
            messages, system, tref = self._resolve_prompt(req)
            resolved = self._to_resolved(req, route, messages, system, stream=False)

            raw: RawResult = await adapter.call(resolved)
            structured = self._apply_structured(raw.text, req.response_format, raw.structured)
        except GatewayError as exc:
            if exc.code == "rate_limited":
                self.metrics.bump_rate_limited(route.alias)
            self.metrics.record(CallRecord(
                request_id=request_id, ts=time.time(), model=route.alias,
                upstream_model=route.upstream.model, protocol=adapter.protocol, stream=False,
                status="error", error_code=exc.code, attempts=1,
                latency={"total_ms": round((time.perf_counter() - t0) * 1000, 2)},
                template=tref.dict() if tref else None,
            ))
            raise

        latency = Latency(
            first_token_ms=raw.first_token_ms,
            upstream_ms=raw.upstream_ms,
            total_ms=round((time.perf_counter() - t0) * 1000, 2),
        )
        resp = UnifiedResponse(
            id=request_id,
            model=route.alias,
            upstream_model=route.upstream.model,
            protocol=adapter.protocol,
            output_text=raw.text,
            structured=structured,
            finish_reason=raw.finish_reason,
            usage=raw.usage,
            latency=latency,
            attempts=raw.attempts,
            template=tref,
        )
        self.metrics.record(CallRecord(
            request_id=request_id, ts=time.time(), model=route.alias,
            upstream_model=route.upstream.model, protocol=adapter.protocol, stream=False,
            status="ok", attempts=raw.attempts, usage=usage_to_dict(raw.usage),
            latency=latency_to_dict(latency), template=tref.dict() if tref else None,
            finish_reason=raw.finish_reason,
        ))
        return resp

    # ---------------- 流式 ----------------
    async def chat_stream(self, req: UnifiedRequest, request_id: str) -> AsyncIterator[StreamEvent]:
        t0 = time.perf_counter()
        route = self.settings.route(req.model)
        adapter = self.adapters[route.alias]
        try:
            await self.limiter.acquire(route.alias)
        except GatewayError as exc:
            self.metrics.bump_rate_limited(route.alias)
            yield StreamEvent(event="error", data=exc.to_dict(request_id)["error"])
            return

        messages, system, tref = self._resolve_prompt(req)
        resolved = self._to_resolved(req, route, messages, system, stream=True)

        yield StreamEvent(event="response.start", data={
            "id": request_id, "object": "llm.response.chunk", "model": route.alias,
            "upstream_model": route.upstream.model, "protocol": adapter.protocol,
            "stream": True, "template": tref.dict() if tref else None,
        })

        buffer: list[str] = []
        usage = Usage()
        first_token_ms: float | None = None
        finish: str | None = None
        attempts = 1
        structured: dict | None = None
        try:
            async for delta, chunk_usage, chunk_finish, att in adapter.stream(resolved):
                attempts = max(attempts, att)
                if delta:
                    if first_token_ms is None:
                        first_token_ms = round((time.perf_counter() - t0) * 1000, 2)
                    buffer.append(delta)
                    yield StreamEvent(event="response.delta", data={"delta": delta})
                if chunk_usage is not None:
                    usage = chunk_usage
                if chunk_finish:
                    finish = chunk_finish
            text = "".join(buffer)
            structured = self._apply_structured(text, req.response_format)
        except GatewayError as exc:
            self.metrics.record(CallRecord(
                request_id=request_id, ts=time.time(), model=route.alias,
                upstream_model=route.upstream.model, protocol=adapter.protocol, stream=True,
                status="error", error_code=exc.code, attempts=attempts,
                latency={"total_ms": round((time.perf_counter() - t0) * 1000, 2),
                         "first_token_ms": first_token_ms},
                template=tref.dict() if tref else None,
            ))
            yield StreamEvent(event="error", data=exc.to_dict(request_id)["error"])
            return
        except Exception as exc:  # 兜底
            err = GatewayError("internal_error", f"{type(exc).__name__}: {exc}")
            yield StreamEvent(event="error", data=err.to_dict(request_id)["error"])
            return

        latency = Latency(
            first_token_ms=first_token_ms,
            upstream_ms=round((time.perf_counter() - t0) * 1000, 2),
            total_ms=round((time.perf_counter() - t0) * 1000, 2),
        )
        self.metrics.record(CallRecord(
            request_id=request_id, ts=time.time(), model=route.alias,
            upstream_model=route.upstream.model, protocol=adapter.protocol, stream=True,
            status="ok", attempts=attempts, usage=usage_to_dict(usage),
            latency=latency_to_dict(latency), template=tref.dict() if tref else None,
            finish_reason=finish,
        ))
        yield StreamEvent(event="response.stop", data={
            "id": request_id, "model": route.alias, "protocol": adapter.protocol,
            "finish_reason": finish, "text_chars": len("".join(buffer)),
            "structured": structured, "usage": usage.dict(),
            "latency": latency.dict(), "attempts": attempts,
        })

    # ---------------- 管理 ----------------
    def override_upstream(self, alias: str, base_url: str | None = None, api_key: str | None = None,
                          model: str | None = None) -> dict:
        route = self.settings.route(alias)
        if base_url:
            route.upstream.base_url = base_url
        if api_key:
            route.upstream.api_key = api_key
        if model:
            route.upstream.model = model
        return {"alias": alias, "protocol": route.upstream.protocol,
                "base_url": route.upstream.base_url, "model": route.upstream.model}

    def list_models(self) -> list[dict]:
        out = []
        for alias, route in self.settings.routes.items():
            out.append({
                "id": alias,
                "object": "model",
                "protocol": route.upstream.protocol,
                "upstream_model": route.upstream.model,
                "base_url": route.upstream.base_url,
                "configured": bool(route.upstream.api_key),
                "rate_limit": {"rpm": route.rate_limit.requests_per_minute, "burst": route.rate_limit.burst},
            })
        return out
