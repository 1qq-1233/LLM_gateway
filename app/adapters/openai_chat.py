"""OpenAI Chat Completions 适配器（POST /v1/chat/completions）。

作为 openai_responses 的兼容备选：很多厂商（含 DeepSeek 官方）只提供该协议。
差异点：system 走 messages[0]；长度字段 max_tokens；结构化输出 response_format。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import UpstreamSpec
from ..errors import GatewayError, from_upstream_status
from ..http import get_client, translate_transport_error
from ..models import ResponseFormat, Usage
from ..retry import call_with_retry
from .base import BaseAdapter, RawResult, ResolvedRequest


def _usage_from_chat(u: dict) -> Usage:
    inp = u.get("prompt_tokens", 0) or 0
    out = u.get("completion_tokens", 0) or 0
    return Usage(
        input_tokens=inp,
        output_tokens=out,
        total_tokens=u.get("total_tokens") or (inp + out),
        cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        or u.get("cached_tokens", 0) or 0,
        reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
    )


class OpenAIChatAdapter(BaseAdapter):
    protocol = "openai_chat_completions"

    def __init__(self, upstream: UpstreamSpec, *, max_attempts: int = 3,
                 base_delay: float = 0.5, max_delay: float = 8.0) -> None:
        super().__init__(upstream)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    def endpoint(self) -> str:
        return self.upstream.base_url.rstrip("/") + "/chat/completions"

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.upstream.api_key:
            h["Authorization"] = f"Bearer {self.upstream.api_key}"
        h.update(self.upstream.extra_headers)
        return h

    def build_payload(self, req: ResolvedRequest) -> dict[str, Any]:
        messages: list[dict] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages += [{"role": m.role, "content": m.content} for m in req.messages if m.role != "system"]
        payload: dict[str, Any] = {
            "model": req.model,
            "messages": messages,
            "temperature": req.temperature,
            "max_tokens": req.max_output_tokens,
            "stream": bool(req.stream),
        }
        if req.stream:
            payload["stream_options"] = {"include_usage": True}
        if req.response_format:
            rf: ResponseFormat = req.response_format
            if rf.type == "json_schema":
                payload["response_format"] = {"type": "json_schema",
                                              "json_schema": {"name": rf.name, "schema": rf.json_schema,
                                                              "strict": bool(rf.strict)}}
            else:
                payload["response_format"] = {"type": "json_object"}
        return payload

    def parse_response(self, payload: dict) -> RawResult:
        if payload.get("error"):
            raise GatewayError("upstream_error", str(payload["error"])[:300], retryable=True)
        choices = payload.get("choices") or []
        text = ""
        finish = None
        if choices:
            text = (choices[0].get("message") or {}).get("content") or ""
            finish = choices[0].get("finish_reason")
        return RawResult(text=text, usage=_usage_from_chat(payload.get("usage") or {}), finish_reason=finish)

    def parse_stream_chunk(self, payload: dict) -> tuple[str, Usage | None, str | None]:
        if payload.get("error"):
            raise GatewayError("upstream_error", str(payload["error"])[:300], retryable=True)
        choices = payload.get("choices") or []
        delta = ""
        finish = None
        if choices:
            delta = (choices[0].get("delta") or {}).get("content") or ""
            finish = choices[0].get("finish_reason")
        usage = _usage_from_chat(payload.get("usage") or {}) if payload.get("usage") else None
        return delta, usage, finish

    async def call(self, req: ResolvedRequest) -> RawResult:
        payload = self.build_payload(req)
        payload["stream"] = False
        payload.pop("stream_options", None)
        url, headers = self.endpoint(), self.headers()

        async def once() -> dict:
            try:
                r = await get_client(self.upstream.timeout_s).post(url, json=payload, headers=headers)
            except Exception as exc:
                raise translate_transport_error(exc) from exc
            if r.status_code >= 400:
                raise from_upstream_status(r.status_code, r.text)
            try:
                return r.json()
            except ValueError:
                raise GatewayError("upstream_bad_response", f"非 JSON 响应：{r.text[:200]}") from None

        start = self._timer()
        data, attempts, _ = await call_with_retry(once, max_attempts=self.max_attempts,
                                                  base_delay=self.base_delay, max_delay=self.max_delay,
                                                  label="openai_chat")
        res = self.parse_response(data)
        res.upstream_ms = self._ms(start)
        res.attempts = attempts
        return res

    async def stream(self, req: ResolvedRequest) -> AsyncIterator[tuple[str, Usage | None, str | None, int]]:
        payload = self.build_payload(req)
        payload["stream"] = True
        url, headers = self.endpoint(), self.headers()
        headers["Accept"] = "text/event-stream"

        async def open_stream() -> tuple[Any, httpx.Response]:
            cm = get_client(self.upstream.timeout_s).stream("POST", url, json=payload, headers=headers)
            try:
                r = await cm.__aenter__()
            except Exception as exc:
                raise translate_transport_error(exc) from exc
            if r.status_code >= 400:
                body = (await r.aread()).decode("utf-8", "ignore")
                await cm.__aexit__(None, None, None)
                raise from_upstream_status(r.status_code, body)
            return cm, r

        (cm, resp), attempts, _ = await call_with_retry(open_stream, max_attempts=self.max_attempts,
                                                        base_delay=self.base_delay, max_delay=self.max_delay,
                                                        label="openai_chat.stream")
        try:
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw in ("[DONE]", ""):
                    continue
                try:
                    chunk = json.loads(raw)
                except ValueError:
                    continue
                delta, usage, finish = self.parse_stream_chunk(chunk)
                if delta or usage is not None or finish:
                    yield delta, usage, finish, attempts
        finally:
            await cm.__aexit__(None, None, None)
