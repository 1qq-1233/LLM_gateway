"""配置加载：模型别名 -> 上游协议 -> 真实模型 -> 限流额度。

约定（``.env`` 或环境变量）：
    MODEL_A_*  模型 A，默认 OpenAI Responses 协议，别名 deepseek-v4-pro
    MODEL_B_*  模型 B，默认 Anthropic Messages 协议，别名 deepseek-v4-flash

protocol 可选：openai_responses / openai_chat_completions / anthropic_messages
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

SUPPORTED_PROTOCOLS = ("openai_responses", "openai_chat_completions", "anthropic_messages")


@dataclass
class RateLimitConfig:
    requests_per_minute: int = 60      # 持续速率
    burst: int = 10                    # 令牌桶容量（突发上限）


@dataclass
class UpstreamSpec:
    protocol: str
    base_url: str                      # Responses: https://api.openai.com/v1 ; Anthropic: https://api.anthropic.com
    api_key: str
    model: str                         # 上游真实模型名
    timeout_s: float = 60.0
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ModelRoute:
    alias: str
    upstream: UpstreamSpec
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass
class Settings:
    routes: dict[str, ModelRoute]
    gateway_api_key: str = ""          # 为空则不校验（本地开发）
    max_attempts: int = 3              # 最多 3 次尝试（1 次正常 + 2 次重试）
    retry_base_delay: float = 0.5
    retry_max_delay: float = 8.0
    prompts_dir: str = "prompts"
    log_dir: str = "logs"
    enable_admin: bool = True          # /admin 覆盖上游地址（验证脚本用）
    log_payloads: bool = False

    def route(self, alias: str) -> ModelRoute:
        try:
            return self.routes[alias]
        except KeyError:
            from .errors import GatewayError
            raise GatewayError(
                "model_not_found",
                f"未知模型别名 '{alias}'，可用：{sorted(self.routes)}",
            ) from None


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _build(letter: str, defaults: tuple[str, str, str, str]) -> ModelRoute:
    alias_d, proto_d, base_d, model_d = defaults
    protocol = _env(f"MODEL_{letter}_PROTOCOL", proto_d)
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"MODEL_{letter}_PROTOCOL={protocol} 不支持，可选 {SUPPORTED_PROTOCOLS}")
    return ModelRoute(
        alias=_env(f"MODEL_{letter}_ALIAS", alias_d),
        upstream=UpstreamSpec(
            protocol=protocol,
            base_url=_env(f"MODEL_{letter}_BASE_URL", base_d),
            api_key=_env(f"MODEL_{letter}_API_KEY", ""),
            model=_env(f"MODEL_{letter}_UPSTREAM_MODEL", model_d),
            timeout_s=float(_env(f"MODEL_{letter}_TIMEOUT_S", "60") or 60),
        ),
        rate_limit=RateLimitConfig(
            requests_per_minute=int(_env(f"MODEL_{letter}_RPM", "60") or 60),
            burst=int(_env(f"MODEL_{letter}_BURST", "10") or 10),
        ),
    )


def load_settings() -> Settings:
    a = _build("A", ("deepseek-v4-pro", "openai_responses", "https://api.openai.com/v1", "deepseek-v4-pro"))
    b = _build("B", ("deepseek-v4-flash", "anthropic_messages", "https://api.anthropic.com", "deepseek-v4-flash"))
    return Settings(
        routes={a.alias: a, b.alias: b},
        gateway_api_key=_env("GATEWAY_API_KEY", ""),
        max_attempts=int(_env("MAX_ATTEMPTS", "3") or 3),
        retry_base_delay=float(_env("RETRY_BASE_DELAY", "0.5") or 0.5),
        retry_max_delay=float(_env("RETRY_MAX_DELAY", "8") or 8),
        prompts_dir=_env("PROMPTS_DIR", "prompts"),
        log_dir=_env("LOG_DIR", "logs"),
        enable_admin=_env("ENABLE_ADMIN", "true").lower() in ("1", "true", "yes"),
        log_payloads=_env("LOG_PAYLOADS", "false").lower() in ("1", "true", "yes"),
    )
