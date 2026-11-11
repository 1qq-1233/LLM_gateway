"""Anthropic Messages API 适配器（POST /v1/messages）。

差异点（相对 OpenAI Responses）：
  - 鉴权：x-api-key + anthropic-version（不是 Authorization: Bearer）
  - 系统提示：顶层 system 字段
  - 长度字段：max_tokens（必填）
  - 结构化输出：没有 response_format，用「强制 tool_use」实现；
                json_object 模式退化为 assistant prefill("{")
  - 流式事件：message_start / content_block_delta(text_delta|input_json_delta)
              / message_delta(usage) / message_stop
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..config import UpstreamSpec
from ..errors import GatewayError, from_upstream_status
from ..http import get_client, translate_transport_error
from ..models import Message, ResponseFormat, Usage
from ..retry import call_with_retry
from .base import BaseAdapter, RawResult, ResolvedRequest

ANTHROPIC_VERSION = "2023-06-01"
STRUCTURED_TOOL_NAME = "emit_structured_output"


class AnthropicMessagesAdapter(BaseAdapter):
    protocol = "anthropic_messages"

    def __init__(self, upstream: UpstreamSpec, *, max_attempts: int = 3,
                 base_delay: float = 0.5, max_delay: float = 8.0) -> None:
        super().__init__(upstream)
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay

    # ---------- 协议翻译：请求 ----------
    def endpoint(self) -> str:
        base = self.upstream.base_url.rstrip("/")
        return base + "/v1/messages" if not base.endswith("/v1") else base + "/messages"

    def headers(self) -> dict[str, str]:
        h = {
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
        if self.upstream.api_key:
            h["x-api-key"] = self.upstream.api_key
        h.update(self.upstream.extra_headers)
        return h

    @staticmethod
    def _normalize_messages(messages: list[Message]) -> list[dict]:
        """合并连续相同角色（Anthropic 要求严格 user/assistant 交替），并剔除非法的空内容。"""
        out: list[dict] = []
        for m in messages:
            if m.role == "system":
                continue
            if not m.content:
                continue
            if out and out[-1]["role"] == m.role:
                out[-1]["content"] += "\n" + m.content
            else:
                out.append({"role": m.role, "content": m.content})
        if not out:
            out.append({"role": "user", "content": "（空输入）"})
        if out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": "继续"})
        return out

    def build_payload(self, req: ResolvedRequest) -> dict[str, Any]:
        messages = self._normalize_messages(req.messages)
        payload: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_output_tokens,
            "temperature": req.temperature,
            "messages": messages,
            "stream": bool(req.stream),
        }
        if req.system:
            payload["system"] = req.system
        rf = req.response_format
        if rf:
            if rf.type == "json_schema":
                # 强制调用工具 -> 工具入参即结构化结果
                payload["tools"] = [{
                    "name": STRUCTURED_TOOL_NAME,
                    "description": "按给定 JSON Schema 输出结果。",
                    "input_schema": rf.json_schema,
                }]
                payload["tool_choice"] = {"type": "tool", "name": STRUCTURED_TOOL_NAME}
            else:
                # json_object：Anthropic 无原生支持，用 assistant prefill 引导
                messages.append({"role": "assistant", "content": "{"})
                payload["messages"] = messages
        return payload

    # ---------- 协议翻译：响应 ----------
    def parse_response(self, payload: dict, req: ResolvedRequest | None = None) -> RawResult:
        if payload.get("type") == "error":
            raise GatewayError("upstream_error",
                               str((payload.get("error") or {}).get("message", payload))[:300], retryable=True)
        texts: list[str] = []
        structured: dict | None = None
        for block in payload.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "tool_use":
                structured = block.get("input") or {}
                texts.append(json.dumps(structured, ensure_ascii=False))
        text = "".join(texts)
        if req is not None and req.response_format and req.response_format.type == "json_object" and structured is None:
            text = "{" + text
        return RawResult(
            text=text,
            structured=structured,
            usage=self._usage(payload.get("usage") or {}),
            finish_reason=payload.get("stop_reason") or "stop",
        )

    @staticmethod
    def _usage(u: dict) -> Usage:
        inp = u.get("input_tokens", 0) or 0
        out = u.get("output_tokens", 0) or 0
        cached = (u.get("cache_read_input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0)
        return Usage(
            input_tokens=inp,
            output_tokens=out,
            total_tokens=u.get("total_tokens") or (inp + out),
            cached_tokens=cached,
            reasoning_tokens=u.get("reasoning_tokens", 0) or 0,
        )

    def parse_stream_chunk(self, payload: dict, state: dict | None = None) -> tuple[str, Usage | None, str | None]:
        """state 用于跨事件累积（input_json_delta / usage 合并），调用方传入同一个 dict。"""
        state = state if state is not None else {}
        kind = payload.get("type", "")

        if kind == "error":
            raise GatewayError("upstream_error",
                               str((payload.get("error") or {}).get("message", payload))[:300], retryable=True)
        if kind == "message_start":
            state["input_usage"] = self._usage((payload.get("message") or {}).get("usage") or {})
            return "", None, None
        if kind == "content_block_start":
            state["block_type"] = (payload.get("content_block") or {}).get("type")
            return "", None, None
        if kind == "content_block_delta":
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                return delta.get("text", "") or "", None, None
            if delta.get("type") == "input_json_delta":
                state["tool_json"] = state.get("tool_json", "") + (delta.get("partial_json") or "")
            return "", None, None
        if kind == "content_block_stop":
            btype = state.get("block_type")
            state["block_type"] = None
            # 工具 JSON 在此一次性吐出，保证下游拿到完整合法 JSON
            if btype == "tool_use" and state.get("tool_json"):
                return state.pop("tool_json"), None, None
            return "", None, None
        if kind == "message_delta":
            out_usage = self._usage({"output_tokens": (payload.get("usage") or {}).get("output_tokens", 0)})
            merged = state.get("input_usage") or Usage()
            merged.output_tokens = out_usage.output_tokens
            merged.total_tokens = merged.input_tokens + merged.output_tokens
            finish = (payload.get("delta") or {}).get("stop_reason") or "stop"
            return "", merged, finish
        if kind == "message_stop":
            return "", None, state.get("finish") or "stop"
        return "", None, None

    # ---------- 调用 ----------
    async def call(self, req: ResolvedRequest) -> RawResult:
        payload = self.build_payload(req)
        payload["stream"] = False
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
                                                  label="anthropic_messages")
        res = self.parse_response(data, req)
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
                                                        label="anthropic_messages.stream")
        state: dict = {}
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
                delta, usage, finish = self.parse_stream_chunk(chunk, state)
                if finish:
                    state["finish"] = finish
                if delta or usage is not None or finish:
                    yield delta, usage, finish, attempts
        finally:
            await cm.__aexit__(None, None, None)
