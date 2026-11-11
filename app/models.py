"""统一对外 API 契约（与底层协议无关）。"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ResponseFormat(BaseModel):
    """结构化输出约束。

    type=json_schema 时提供 json_schema（标准 JSON Schema 对象）；
    type=json_object 时仅保证返回合法 JSON（Anthropic 侧由 prefill 兜底）。
    """
    type: Literal["json_schema", "json_object"] = "json_schema"
    name: str = "structured_output"
    json_schema: dict[str, Any] | None = None
    strict: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.type == "json_schema" and not self.json_schema:
            raise ValueError("type=json_schema 时必须提供 json_schema")
        return self


class UnifiedRequest(BaseModel):
    """统一调用请求：唯一入口，屏蔽协议差异。"""
    model: str = Field(..., description="模型别名，如 deepseek-v4-pro / deepseek-v4-flash")
    input: str | list[Message] | None = Field(None, description="用户输入；字符串或多轮消息数组")
    system: str | None = None
    temperature: float = 0.7
    max_output_tokens: int = 1024

    stream: bool = False
    response_format: ResponseFormat | None = None

    template: str | None = Field(None, description="模板引用：name / name@2 / name@latest")
    variables: dict[str, Any] = Field(default_factory=dict, description="模板变量")

    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self):
        if not self.input and not self.template:
            raise ValueError("input 与 template 至少提供一个")
        return self


class Usage(BaseModel):
    """Token 分类统计。"""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0        # 命中缓存的输入 token
    reasoning_tokens: int = 0     # 思考/推理 token
    tool_tokens: int = 0          # 结构化输出（tool）占用


class Latency(BaseModel):
    first_token_ms: float | None = None   # 首 Token 延迟
    upstream_ms: float | None = None      # 上游耗时
    total_ms: float = 0.0                 # 网关端到端耗时


class TemplateRef(BaseModel):
    name: str
    version: int
    rendered_chars: int


class UnifiedResponse(BaseModel):
    id: str
    object: Literal["llm.response"] = "llm.response"
    model: str                      # 对外别名
    upstream_model: str             # 上游真实模型
    protocol: str                   # 实际使用的协议
    created: int = Field(default_factory=lambda: int(time.time()))
    output_text: str = ""
    structured: dict[str, Any] | None = None
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    latency: Latency = Field(default_factory=Latency)
    attempts: int = 1               # 实际尝试次数（重试可观测）
    template: TemplateRef | None = None


# ---------- 流式事件（SSE） ----------
class StreamEvent(BaseModel):
    """网关对外 SSE 事件统一结构，三种协议输出完全一致。"""
    event: Literal["response.start", "response.delta", "response.stop", "error"]
    data: dict[str, Any]


class TemplateCreate(BaseModel):
    name: str
    description: str = ""
    system: str | None = None
    content: str                       # 支持 {{variable}}
    changelog: str = "initial"


class TemplateVersionAdd(BaseModel):
    system: str | None = None
    content: str
    changelog: str = "update"


class RenderRequest(BaseModel):
    variables: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
