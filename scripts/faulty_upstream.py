"""协议仿真 + 故障注入上游（仅用于验证，非生产组件）。

提供三种真实协议的响应格式，供验证脚本离线跑通全链路；
并可通过 /control/* 注入 500 故障与延迟，用于验证重试和限流。

  POST /openai/v1/responses          OpenAI Responses 协议（含 SSE）
  POST /openai/v1/chat/completions   OpenAI Chat Completions 协议（含 SSE）
  POST /anthropic/v1/messages        Anthropic Messages 协议（含 SSE）
  POST /control/fail?times=2&status=500
  POST /control/delay?seconds=0.3
  POST /control/reset
  GET  /stats
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Simulated Upstream")

STATE = {"fail_times": 0, "delay_s": 0.0, "requests": 0, "failed": 0}


# ---------- 工具 ----------
def sample_from_schema(schema: dict) -> Any:
    """按 JSON Schema 生成一个能通过校验的示例对象。"""
    if not isinstance(schema, dict):
        return {}
    if "properties" in schema:
        out: dict[str, Any] = {}
        required = set(schema.get("required", list(schema["properties"].keys())))
        for key, sub in schema["properties"].items():
            if key in required or random.random() < 0.8:
                out[key] = sample_from_schema(sub)
        return out
    t = schema.get("type")
    if isinstance(t, list):
        t = t[0] if t else "string"
    if t == "object":
        return {}
    if t == "array":
        return [sample_from_schema(schema.get("items", {"type": "string"}))]
    if t == "integer":
        return int(schema.get("minimum", 1))
    if t == "number":
        return 1.5
    if t == "boolean":
        return True
    if "enum" in schema:
        return schema["enum"][0]
    return "示例文本"


def usage_openai() -> dict:
    inp, out = random.randint(60, 120), random.randint(40, 90)
    return {
        "input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
        "input_tokens_details": {"cached_tokens": random.randint(0, 20)},
        "output_tokens_details": {"reasoning_tokens": random.randint(0, 30)},
    }


def usage_anthropic() -> dict:
    inp, out = random.randint(60, 120), random.randint(40, 90)
    return {"input_tokens": inp, "output_tokens": out,
            "cache_read_input_tokens": random.randint(0, 20), "cache_creation_input_tokens": 0}


def extract_user_text(payload: dict) -> str:
    inp = payload.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        return " ".join(str(i.get("content", "")) for i in inp if isinstance(i, dict))
    msgs = payload.get("messages") or []
    return " ".join(str(m.get("content", "")) for m in msgs if isinstance(m, dict) and m.get("role") == "user")


def wants_json_schema(payload: dict) -> dict | None:
    fmt = (payload.get("text") or {}).get("format")
    if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
        return fmt.get("schema")
    rf = payload.get("response_format")
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        return (rf.get("json_schema") or {}).get("schema")
    return None


def forced_tool(payload: dict) -> dict | None:
    tc = payload.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "tool":
        for t in payload.get("tools", []) or []:
            if t.get("name") == tc.get("name"):
                return t.get("input_schema")
    return None


def make_text(payload: dict) -> str:
    return "【仿真回复】已收到你的输入：" + (extract_user_text(payload) or "")[:80]


def chunks(text: str, n: int = 6) -> list[str]:
    size = max(1, (len(text) + n - 1) // n)
    return [text[i:i + size] for i in range(0, len(text), size)] or [text]


async def maybe_fail() -> JSONResponse | None:
    STATE["requests"] += 1
    if STATE["delay_s"]:
        await asyncio.sleep(STATE["delay_s"])
    if STATE["fail_times"] > 0:
        STATE["fail_times"] -= 1
        STATE["failed"] += 1
        return JSONResponse(status_code=500, content={"error": {"message": "模拟上游 500 故障"}})
    return None


# ---------- OpenAI Responses ----------
def responses_completed(text: str) -> dict:
    return {
        "id": "resp_sim", "object": "response", "status": "completed", "model": "sim",
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text}]}],
        "output_text": text, "usage": usage_openai(),
    }


@app.post("/openai/v1/responses")
async def openai_responses(request: Request):
    payload = await request.json()
    bad = await maybe_fail()
    if bad:
        return bad
    schema = wants_json_schema(payload)
    text = json.dumps(sample_from_schema(schema), ensure_ascii=False) if schema else make_text(payload)
    if not payload.get("stream"):
        return responses_completed(text)

    async def gen():
        yield f"event: response.created\ndata: {json.dumps({'type':'response.created','response':{'id':'resp_sim'}})}\n\n"
        for c in chunks(text):
            await asyncio.sleep(0.05)
            ev = {"type": "response.output_text.delta", "delta": c}
            yield f"event: response.output_text.delta\ndata: {json.dumps(ev)}\n\n"
        done = responses_completed(text)
        done["type"] = "response.completed"
        yield f"event: response.completed\ndata: {json.dumps({'type':'response.completed','response':done})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- OpenAI Chat Completions ----------
@app.post("/openai/v1/chat/completions")
async def openai_chat(request: Request):
    payload = await request.json()
    bad = await maybe_fail()
    if bad:
        return bad
    schema = wants_json_schema(payload)
    text = json.dumps(sample_from_schema(schema), ensure_ascii=False) if schema else make_text(payload)
    if not payload.get("stream"):
        return {"id": "chatcmpl_sim", "object": "chat.completion", "model": payload.get("model", "sim"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                             "finish_reason": "stop"}],
                "usage": usage_openai()}

    async def gen():
        for c in chunks(text):
            await asyncio.sleep(0.05)
            ev = {"id": "chatcmpl_sim", "object": "chat.completion.chunk", "model": "sim",
                  "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}]}
            yield f"data: {json.dumps(ev)}\n\n"
        ev = {"id": "chatcmpl_sim", "object": "chat.completion.chunk", "model": "sim",
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage_openai()}
        yield f"data: {json.dumps(ev)}\n\ndata: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- Anthropic Messages ----------
def anthropic_message(text: str, tool_input: dict | None = None) -> dict:
    content: list[dict] = []
    if tool_input is not None:
        content.append({"type": "tool_use", "id": "toolu_sim", "name": "emit_structured_output",
                        "input": tool_input})
    elif text:
        content.append({"type": "text", "text": text})
    return {"id": "msg_sim", "type": "message", "role": "assistant", "model": "sim",
            "content": content, "stop_reason": "tool_use" if tool_input is not None else "end_turn",
            "usage": usage_anthropic()}


@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    payload = await request.json()
    bad = await maybe_fail()
    if bad:
        return bad
    schema = forced_tool(payload)
    if schema:
        tool_input = sample_from_schema(schema)
        text = json.dumps(tool_input, ensure_ascii=False)
    else:
        tool_input, text = None, make_text(payload)

    if not payload.get("stream"):
        return anthropic_message(text, tool_input)

    async def gen():
        yield f"event: message_start\ndata: {json.dumps({'type':'message_start','message':{'id':'msg_sim','usage':usage_anthropic()}})}\n\n"
        if tool_input is not None:
            yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'toolu_sim','name':'emit_structured_output'}})}\n\n"
            for c in chunks(text, 4):
                await asyncio.sleep(0.05)
                yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':c}})}\n\n"
            yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
        else:
            yield f"event: content_block_start\ndata: {json.dumps({'type':'content_block_start','index':0,'content_block':{'type':'text'}})}\n\n"
            for c in chunks(text):
                await asyncio.sleep(0.05)
                yield f"event: content_block_delta\ndata: {json.dumps({'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':c}})}\n\n"
            yield f"event: content_block_stop\ndata: {json.dumps({'type':'content_block_stop','index':0})}\n\n"
        md = {"type": "message_delta", "delta": {"stop_reason": "tool_use" if tool_input else "end_turn"},
              "usage": {"output_tokens": random.randint(40, 90)}}
        yield f"event: message_delta\ndata: {json.dumps(md)}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type':'message_stop'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------- 控制面 ----------
@app.post("/control/fail")
async def control_fail(times: int = 1, status: int = 500):
    STATE["fail_times"] = times
    return {"fail_times": STATE["fail_times"], "status": status}


@app.post("/control/delay")
async def control_delay(seconds: float = 0.0):
    STATE["delay_s"] = seconds
    return {"delay_s": seconds}


@app.post("/control/reset")
async def control_reset():
    STATE.update({"fail_times": 0, "delay_s": 0.0, "requests": 0, "failed": 0})
    return STATE


@app.get("/stats")
async def stats():
    return {**STATE, "ts": time.time()}


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("SIM_PORT", "8101")),
                log_level="warning")
