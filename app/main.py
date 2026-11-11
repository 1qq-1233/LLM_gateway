"""LLM 统一模型调用服务 —— FastAPI 入口。

统一入口：POST /v1/chat（stream=false -> JSON；stream=true -> SSE）
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .config import load_settings
from .errors import GatewayError
from .gateway import Gateway
from .http import aclose_all
from .models import (
    RenderRequest, StreamEvent, TemplateCreate, TemplateVersionAdd, UnifiedRequest, UnifiedResponse,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("gateway.api")

settings = load_settings()
gw = Gateway(settings)
START_TS = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("已加载模型：%s", [(m["id"], m["protocol"]) for m in gw.list_models()])
    for m in gw.list_models():
        if not m["configured"]:
            log.warning("模型 %s 未配置 API Key（%s_*_API_KEY 为空），调用将直接报配置错误",
                        m["id"], "MODEL_A" if m["protocol"].startswith("openai") else "MODEL_B")
    yield
    await aclose_all()


app = FastAPI(title="LLM Unified Gateway", version="1.0.0",
              description="统一抽象层：多协议（OpenAI Responses / Chat Completions / Anthropic Messages）"
                          "下的流式、结构化输出、提示词版本管理、可观测性与韧性。",
              lifespan=lifespan)


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:16]


async def auth(authorization: str | None = Header(default=None),
               x_api_key: str | None = Header(default=None)) -> None:
    if not settings.gateway_api_key:
        return
    token = (authorization or "").removeprefix("Bearer ").strip() or (x_api_key or "").strip()
    if token != settings.gateway_api_key:
        raise GatewayError("unauthorized", "缺少或错误的 GATEWAY_API_KEY")


@app.exception_handler(GatewayError)
async def gateway_error_handler(request: Request, exc: GatewayError):
    rid = getattr(request.state, "request_id", new_request_id())
    body = exc.to_dict(rid)
    if exc.code == "rate_limited":
        retry_after = (exc.details or {}).get("retry_after_s", 1)
        return JSONResponse(status_code=429, content=body, headers={"Retry-After": str(retry_after)})
    return JSONResponse(status_code=exc.http_status, content=body)


# ============================ 统一调用 ============================
@app.post("/v1/chat", response_model=UnifiedResponse, dependencies=[Depends(auth)])
async def chat(req: UnifiedRequest, request: Request):
    rid = new_request_id()
    request.state.request_id = rid
    if req.stream:
        generator = _sse(req, rid)
        return StreamingResponse(generator, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Request-Id": rid,
                                          "X-Accel-Buffering": "no"})
    resp = await gw.chat(req, rid)
    return JSONResponse(content=json.loads(resp.model_dump_json()),
                        headers={"X-Request-Id": rid})


async def _sse(req: UnifiedRequest, rid: str):
    try:
        async for event in gw.chat_stream(req, rid):
            payload = json.dumps(event.data, ensure_ascii=False)
            yield f"event: {event.event}\ndata: {payload}\n\n"
    except Exception as exc:  # 生成器内的兜底，保证 SSE 不静默断流
        err = exc if isinstance(exc, GatewayError) else GatewayError("internal_error", str(exc))
        yield f"event: error\ndata: {json.dumps(err.to_dict(rid)['error'], ensure_ascii=False)}\n\n"


# OpenAI 兼容别名，方便直接用 openai SDK / curl 迁移
@app.post("/v1/chat/completions", dependencies=[Depends(auth)])
async def chat_alias(req: UnifiedRequest, request: Request):
    return await chat(req, request)


# ============================ 模型 / 健康 ============================
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "uptime_s": round(time.time() - START_TS, 1),
            "models": {m["id"]: m["protocol"] for m in gw.list_models()}}


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": gw.list_models()}


# ============================ 提示词模板 ============================
@app.get("/v1/templates")
async def list_templates():
    return {"object": "list", "data": gw.templates.list()}


@app.post("/v1/templates")
async def create_template(body: TemplateCreate):
    return gw.templates.create(body.name, body.description, body.system, body.content, body.changelog)


@app.get("/v1/templates/{name}")
async def get_template(name: str):
    return gw.templates.get(name)


@app.delete("/v1/templates/{name}")
async def delete_template(name: str):
    gw.templates.delete(name)
    return {"deleted": name}


@app.post("/v1/templates/{name}/versions")
async def add_version(name: str, body: TemplateVersionAdd):
    return gw.templates.add_version(name, body.system, body.content, body.changelog)


@app.post("/v1/templates/{name}/render")
async def render_template(name: str, body: RenderRequest):
    ref = f"{name}@{body.version}" if body.version is not None else name
    version_doc, version, content, system = gw.templates.resolve(ref, body.variables)
    return {"template": name.split("@")[0], "version": version,
            "system": system, "content": content, "variables": version_doc.get("variables", [])}


# ============================ 可观测性 ============================
@app.get("/v1/metrics")
async def metrics():
    snap = gw.metrics.snapshot()
    snap["uptime_s"] = round(time.time() - START_TS, 1)
    snap["rate_limit_buckets"] = gw.limiter.stats()
    return snap


@app.get("/v1/metrics/prometheus")
async def metrics_prometheus():
    return PlainTextResponse(gw.metrics.prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/v1/calls")
async def recent_calls(limit: int = 20):
    return {"object": "list", "data": gw.metrics.recent(limit)}


# ============================ 运维调试（可关闭） ============================
@app.post("/admin/models/{alias}/upstream")
async def override_upstream(alias: str, base_url: str | None = None, api_key: str | None = None,
                            upstream_model: str | None = None):
    if not settings.enable_admin:
        raise GatewayError("invalid_request", "admin 接口已关闭（ENABLE_ADMIN=false）")
    return gw.override_upstream(alias, base_url, api_key, upstream_model)


@app.post("/admin/ratelimit/{alias}")
async def set_ratelimit(alias: str, rpm: int = 60, burst: int = 10):
    if not settings.enable_admin:
        raise GatewayError("invalid_request", "admin 接口已关闭")
    route = settings.route(alias)
    route.rate_limit.requests_per_minute = rpm
    route.rate_limit.burst = burst
    gw.limiter.register(alias, rpm, burst)
    return {"alias": alias, "rpm": rpm, "burst": burst}


def main() -> None:  # pragma: no cover
    import uvicorn
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "0.0.0.0"),
                port=int(os.environ.get("PORT", "8000")), reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
