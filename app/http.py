"""共享 httpx 客户端（连接池复用 + 统一异常翻译）。"""
from __future__ import annotations

import httpx

from .errors import GatewayError

_CLIENTS: dict[float, httpx.AsyncClient] = {}


def get_client(timeout: float) -> httpx.AsyncClient:
    key = round(float(timeout), 1)
    client = _CLIENTS.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            follow_redirects=True,
        )
        _CLIENTS[key] = client
    return client


async def aclose_all() -> None:
    for c in _CLIENTS.values():
        if not c.is_closed:
            await c.aclose()
    _CLIENTS.clear()


def translate_transport_error(exc: Exception) -> GatewayError:
    if isinstance(exc, httpx.LocalProtocolError):
        # 典型：API Key 为空导致 "Bearer " 非法 header —— 属于配置问题，重试无意义
        return GatewayError("invalid_request", f"构造上游请求失败（多为 API Key 为空）：{exc}", retryable=False,
                            http_status=400)
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError("upstream_timeout", f"上游超时：{exc}", retryable=True)
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return GatewayError("upstream_unavailable", f"上游连接失败：{exc}", retryable=True)
    return GatewayError("upstream_error", f"{type(exc).__name__}: {exc}", retryable=True)
