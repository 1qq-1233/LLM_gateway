"""统一错误码体系。

所有上游差异（OpenAI / Anthropic / 网络层）在适配器内被翻译成 GatewayError，
由 main.py 的异常处理器统一输出，保证调用方看到的错误结构恒定。
"""
from __future__ import annotations

from typing import Any

# code -> (http_status, 默认消息, 是否可重试)
ERROR_SPEC: dict[str, tuple[int, str, bool]] = {
    # ---- 4xx 请求侧 ----
    "invalid_request":            (400, "请求体不合法", False),
    "invalid_json_schema":        (400, "response_format.json_schema 不是合法 JSON Schema", False),
    "template_not_found":         (404, "提示词模板不存在", False),
    "template_version_not_found": (404, "提示词模板版本不存在", False),
    "template_render_error":      (400, "模板变量渲染失败（缺少必填变量）", False),
    "model_not_found":            (404, "未知模型别名", False),
    "unauthorized":               (401, "网关鉴权失败", False),
    # ---- 限流 ----
    "rate_limited":               (429, "模型级限流触发", False),
    "upstream_rate_limited":      (429, "上游返回限流", True),
    # ---- 上游 ----
    "upstream_auth_failed":       (401, "上游鉴权失败（检查 API Key / base_url）", False),
    "upstream_timeout":           (408, "上游请求超时", True),
    "upstream_error":             (502, "上游返回错误", True),
    "upstream_unavailable":       (503, "上游暂时不可用", True),
    "upstream_bad_response":      (502, "上游返回结构无法解析", False),
    "structured_output_invalid":  (502, "结构化输出不符合给定 JSON Schema", False),
    # ---- 网关 ----
    "internal_error":             (500, "网关内部错误", False),
}


class GatewayError(Exception):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retryable: bool | None = None,
        http_status: int | None = None,
        details: Any = None,
        upstream_status: int | None = None,
    ):
        spec = ERROR_SPEC.get(code)
        self.code = code
        self.message = message or (spec[1] if spec else "未知错误")
        self.http_status = http_status or (spec[0] if spec else 500)
        self.retryable = retryable if retryable is not None else (spec[2] if spec else False)
        self.details = details
        self.upstream_status = upstream_status
        super().__init__(f"[{code}] {self.message}")

    def to_dict(self, request_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "type": self.__class__.__name__,
                "http_status": self.http_status,
                "retryable": self.retryable,
                "upstream_status": self.upstream_status,
                "details": self.details,
                "request_id": request_id,
            }
        }


def from_upstream_status(status: int, body: str = "", code_hint: str | None = None) -> GatewayError:
    """把上游 HTTP 状态码映射成统一错误码。"""
    snippet = (body or "")[:300]
    if status in (401, 403):
        return GatewayError("upstream_auth_failed", details=snippet, upstream_status=status)
    if status == 429:
        return GatewayError("upstream_rate_limited", details=snippet, upstream_status=status)
    if status == 408 or status == 409 and code_hint == "timeout":
        return GatewayError("upstream_timeout", details=snippet, upstream_status=status)
    if 500 <= status < 600:
        return GatewayError("upstream_error", details=f"HTTP {status}: {snippet}", upstream_status=status)
    return GatewayError("upstream_bad_response", details=f"HTTP {status}: {snippet}", upstream_status=status)
