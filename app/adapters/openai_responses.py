"""OpenAI Responses API 适配器（POST /v1/responses）。

差异点（相对 Anthropic）：
  - 鉴权：Authorization: Bearer
  - 系统提示字段：instructions（不是 system）
  - 长度字段：max_output_tokens（不是 max_tokens）
  - 结构化输出：text.format = {type: json_schema, name, schema, strict}
  - 流式事件：response.output_text.delta / response.completed
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


def _usage_from_responses(u: dict) -> Usage:
    inp = u.get("input_tokens", 0) or 0
    out = u.get("output_tokens", 0) or 0
    return Usage(
        input_tokens=inp,
        output_tokens=out,
        total_tokens=u.get("total_tokens") or (inp + out),
        cached_tokens=(u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0,
        reasoning_tokens=(u.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
    )


class OpenAIResponsesAdapter(BaseAdapter):
    protocol = "openai_responses"

    def __init__(self, upstream: UpstreamSpec, *, max_attempts: int = 3,
                 base_delay: float = 0.5, max_delay: float = 8.0) -> None:
        super().__init__(upstream)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    # ---------- 协议翻译：请求 ----------
    def endpoint(self) -> str:
        return self.upstream.base_url.rstrip("/") + "/responses"

    def headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.upstream.api_key:
            h["Authorization"] = f"Bearer {self.upstream.api_key}"
        h.update(self.upstream.extra_headers)
        return h

    def build_payload(self, req: ResolvedRequest) -> dict[str, Any]:
        messages = [{"role": m.role, "content": m.content} for m in req.messages if m.role != "system"]
        # 单轮纯文本用字符串 input；多轮用数组（Responses API 两种都支持）
        if len(messages) == 1 and messages[0]["role"] == "user":
            input_field: Any = messages[0]["content"]
        else:
            input_field = messages

        payload: dict[str, Any] = {
            "model": req.model,
            "input": input_field,
            "max_output_tokens": req.max_output_tokens,
            "temperature": req.temperature,
            "stream": bool(req.stream),
        }
        if req.system:
            payload["instructions"] = req.system
        if req.response_format:
            payload["text"] = self._text_format(req.response_format)
        return payload

    @staticmethod
    def _text_format(rf: ResponseFormat) -> dict:
        if rf.type == "json_schema":
            fmt: dict[str, Any] = {"type": "json_schema", "name": rf.name,
                                   "schema": rf.json_schema, "strict": bool(rf.strict)}
            return {"format": fmt}
        return {"format": {"type": "json_object"}}

    # ---------- 协议翻译：响应 ----------
    def parse_response(self, payload: dict) -> RawResult:
        if payload.get("error"):
            raise GatewayError("upstream_error", str(payload["error"])[:300], retryable=True)
        text_parts: list[str] = []
        for item in payload.get("output", []) or []:
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    text_parts.append(c["text"])
        text = "".join(text_parts) or payload.get("output_text", "") or ""
        usage = _usage_from_responses(payload.get("usage") or {})
        return RawResult(
            text=text,
            usage=usage,
            finish_reason=payload.get("status") or "completed",
        )

    def parse_stream_chunk(self, payload: dict) -> tuple[str, Usage | None, str | None]:
        kind = payload.get("type", "")
        if kind == "response.output_text.delta":
            return payload.get("delta", "") or "", None, None
        if kind in ("response.completed", "response.incomplete", "response.failed"):
            usage = _usage_from_responses(payload.get("response", {}).get("usage") or {})
            finish = "stop" if kind == "response.completed" else kind
            if kind == "response.failed":
                err = (payload.get("response") or {}).get("error") or {}
                raise GatewayError("upstream_error", f"上游流失败：{err}", retryable=True)
            return "", usage, finish
        if kind == "error":
            raise GatewayError("upstream_error", f"上游流错误：{payload.get('message') or payload}", retryable=True)
        return "", None, None

    # ---------- 调用 ----------
    async def _post(self, payload: dict) -> dict:
        url, headers = self.endpoint(), self.headers()

        async def once() -> dict:
            try:
                resp = await get_client(self.upstream.timeout_s).post(url, json=payload, headers=headers)
            except Exception as exc:
                raise translate_transport_error(exc) from exc
            if resp.status_code >= 400:
                raise from_upstream_status(resp.status_code, resp.text)
            try:
                return resp.json()
            except ValueError:
                raise GatewayError("upstream_bad_response", f"非 JSON 响应：{resp.text[:200]}") from None

        data, attempts, _ = await call_with_retry(
            once, max_attempts=self.max_attempts, base_delay=self.base_delay,
            max_delay=self.max_delay, label="openai_responses",
        )
        return {"data": data, "attempts": attempts}

    async def call(self, req: ResolvedRequest) -> RawResult:
        payload = self.build_payload(req)
        payload["stream"] = False
        start = self._timer()
        out = await self._post(payload)
        result = self.parse_response(out["data"])
        result.upstream_ms = self._ms(start)
        result.attempts = out["attempts"]
        return result

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

        (cm, resp), attempts, _ = await call_with_retry(
            open_stream, max_attempts=self.max_attempts, base_delay=self.base_delay,
            max_delay=self.max_delay, label="openai_responses.stream",
        )
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
