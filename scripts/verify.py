#!/usr/bin/env python3
"""功能验收脚本：一次性跑完 15 项检查并输出可验证证据。

用法：
    python scripts/verify.py                 # 默认 real：用 .env 里配置的上游
    python scripts/verify.py --mode sim      # 全量走本地协议仿真上游（离线可跑）
    python scripts/verify.py --base-url http://127.0.0.1:8000 --mode real

说明：
  - 重试与限流两项需要"注入故障 / 打满配额"，会把模型 A 的 base_url 临时指向
    本地仿真上游（scripts/faulty_upstream.py），结束后自动还原，不污染真实配置。
  - 结果同时写入 logs/verify_report.json。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

import httpx

if sys.stdout.isatty():
    GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
else:
    GREEN = RED = YELLOW = RESET = ""  # 重定向到 evidence/preflight.log 时不写入 ANSI 控制符
DEFAULT_SIM_PORT = 8101

PERSON_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["name", "age", "skills"],
    "additionalProperties": False,
}


# ---------------- 基础设施 ----------------
class Runner:
    def __init__(self, base_url: str, mode: str, sim_port: int = DEFAULT_SIM_PORT) -> None:
        self.base = base_url.rstrip("/")
        self.mode = mode
        self.sim_port = sim_port
        self.client = httpx.Client(base_url=self.base, timeout=120.0)
        self.results: list[dict] = []
        self.models: dict[str, dict] = {}
        self.sim_proc: subprocess.Popen | None = None

    # ---- 报告 ----
    def check(self, idx: str, name: str, ok: bool, evidence: Any = "", hint: str = "") -> None:
        self.results.append({"id": idx, "name": name, "passed": bool(ok),
                             "evidence": evidence, "hint": hint})
        mark = f"{GREEN}[PASS]{RESET}" if ok else f"{RED}[FAIL]{RESET}"
        ev = json.dumps(evidence, ensure_ascii=False) if not isinstance(evidence, str) else evidence
        print(f"{mark} {idx} {name:<28} {ev[:160]}")
        if not ok and hint:
            print(f"       {YELLOW}-> {hint}{RESET}")

    # ---- HTTP 助手 ----
    def post(self, path: str, **kw) -> httpx.Response:
        return self.client.post(path, **kw)

    def get(self, path: str, **kw) -> httpx.Response:
        return self.client.get(path, **kw)

    def chat(self, payload: dict) -> httpx.Response:
        return self.post("/v1/chat", json=payload)

    def chat_sse(self, payload: dict) -> list[tuple[str, dict]]:
        events: list[tuple[str, dict]] = []
        name = None
        with self.client.stream("POST", "/v1/chat", json=payload,
                                headers={"Accept": "text/event-stream"}) as resp:
            for line in resp.iter_lines():
                if line.startswith("event: "):
                    name = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except ValueError:
                        data = {"raw": line[6:]}
                    events.append((name or "message", data))
        return events

    # ---- 仿真上游 ----
    def ensure_sim(self) -> str:
        sim_base = f"http://127.0.0.1:{self.sim_port}"
        try:
            probe = httpx.get(f"{sim_base}/stats", timeout=2.0)
            # 只有确认是自家仿真上游才复用，避免撞上同端口的其他服务
            if probe.status_code == 200 and "fail_times" in probe.json():
                return sim_base
        except Exception:
            pass
        here = os.path.dirname(os.path.abspath(__file__))
        env = {**os.environ, "SIM_PORT": str(self.sim_port)}
        self.sim_proc = subprocess.Popen(
            [sys.executable, os.path.join(here, "faulty_upstream.py")],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                httpx.get(f"{sim_base}/stats", timeout=1.0)
                return sim_base
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("仿真上游启动失败")

    def sim_control(self, **params) -> None:
        httpx.post(f"http://127.0.0.1:{self.sim_port}/control/fail", params=params, timeout=5.0)

    def sim_reset(self) -> None:
        httpx.post(f"http://127.0.0.1:{self.sim_port}/control/reset", timeout=5.0)

    def stop_sim(self) -> None:
        if self.sim_proc:
            self.sim_proc.terminate()
            try:
                self.sim_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.sim_proc.kill()

    def override(self, alias: str, base_url: str | None = None, upstream_model: str | None = None) -> None:
        params = {k: v for k, v in {"base_url": base_url, "upstream_model": upstream_model}.items() if v}
        r = self.post(f"/admin/models/{alias}/upstream", params=params)
        r.raise_for_status()

    def set_ratelimit(self, alias: str, rpm: int, burst: int) -> None:
        r = self.post(f"/admin/ratelimit/{alias}", params={"rpm": rpm, "burst": burst})
        r.raise_for_status()


# ---------------- 检查项 ----------------
def run_checks(r: Runner) -> None:
    # 00 健康检查
    try:
        h = r.get("/healthz", timeout=5.0)
        h.raise_for_status()
        hb = h.json()
        r.check("00", "网关健康检查", hb.get("status") == "ok", hb)
    except Exception as exc:
        r.check("00", "网关健康检查", False, str(exc), "先启动服务：uvicorn app.main:app --port 8000")
        return

    # 01 模型注册 & 协议差异
    models = r.get("/v1/models").json()["data"]
    r.models = {m["id"]: m for m in models}
    protocols = {m["id"]: m["protocol"] for m in models}
    distinct = len(set(protocols.values())) >= 2
    r.check("01", "双协议模型注册", distinct and len(models) >= 2, protocols,
            "至少配置两个不同协议的模型")

    # 选出 A / B（A = openai 系，B = anthropic 系）
    model_a = next((m["id"] for m in models if m["protocol"].startswith("openai")), None)
    model_b = next((m["id"] for m in models if m["protocol"] == "anthropic_messages"), None)
    if not model_a or not model_b:
        r.check("02", "模型选取", False, protocols, "需要一个 openai 系 + 一个 anthropic 系模型")
        return

    # --- sim 模式：把两个模型都指向仿真上游 ---
    if r.mode == "sim":
        sim = r.ensure_sim()
        r.override(model_a, f"{sim}/openai/v1", "sim-openai")
        r.override(model_b, f"{sim}/anthropic", "sim-anthropic")
        r.sim_reset()

    original_base = r.models[model_a]["base_url"]

    # 02/03 非流式调用（两个协议各一次）
    for idx, model, label in (("02", model_a, "OpenAI系非流式"), ("03", model_b, "Anthropic系非流式")):
        try:
            resp = r.chat({"model": model, "input": "用一句话介绍网关的作用",
                           "max_output_tokens": 200, "temperature": 0.2})
            body = resp.json()
            ok = resp.status_code == 200 and (body.get("output_text") or "").strip() != ""
            r.check(idx, label, ok, {"protocol": body.get("protocol"),
                                     "chars": len(body.get("output_text", "")),
                                     "usage": body.get("usage"),
                                     "latency_ms": (body.get("latency") or {}).get("total_ms")},
                    "检查 API Key / base_url 是否正确" if not ok else "")
        except Exception as exc:
            r.check(idx, label, False, str(exc))

    # 04/05 流式调用
    for idx, model, label in (("04", model_a, "OpenAI系流式SSE"), ("05", model_b, "Anthropic系流式SSE")):
        try:
            evs = r.chat_sse({"model": model, "input": "请流式输出：统一网关的三个能力",
                              "stream": True, "max_output_tokens": 200})
            kinds = [e[0] for e in evs]
            deltas = [d["delta"] for k, d in evs if k == "response.delta" and d.get("delta")]
            stop = next((d for k, d in evs if k == "response.stop"), {})
            ok = kinds.count("response.start") == 1 and len(deltas) >= 2 and "response.stop" in kinds
            r.check(idx, label, ok, {"events": kinds[:3] + ["..."] + kinds[-2:],
                                     "delta_chunks": len(deltas),
                                     "first_token_ms": (stop.get("latency") or {}).get("first_token_ms"),
                                     "usage": stop.get("usage"),
                                     "text_sample": "".join(deltas)[:40]})
        except Exception as exc:
            r.check(idx, label, False, str(exc))

    # 06/07 结构化输出
    for idx, model, label in (("06", model_a, "OpenAI系结构化输出"), ("07", model_b, "Anthropic系结构化输出")):
        try:
            resp = r.chat({
                "model": model,
                "input": "抽取人物信息：张三，28岁，擅长 Python 和 Go，资深网关工程师。",
                "response_format": {"type": "json_schema", "name": "person", "json_schema": PERSON_SCHEMA},
                "max_output_tokens": 300, "temperature": 0.2,
            })
            body = resp.json()
            ok = resp.status_code == 200 and isinstance(body.get("structured"), dict)
            r.check(idx, label, ok, {"protocol": body.get("protocol"),
                                     "structured": body.get("structured"),
                                     "raw_is_json": (body.get("output_text", "").strip().startswith("{"))},
                    "模型未严格遵循 schema，可降低 temperature 或换更强模型" if not ok else "")
        except Exception as exc:
            r.check(idx, label, False, str(exc))

    # 08 模板创建 + 版本管理
    tpl = "verify_demo"
    try:
        r.client.delete(f"/v1/templates/{tpl}")
        c1 = r.post("/v1/templates", json={"name": tpl, "content": "V1回答：{{question}}",
                                           "system": "你是助手", "changelog": "初版"})
        c2 = r.post(f"/v1/templates/{tpl}/versions",
                    json={"content": "V2回答：{{question}}（语气：{{tone}}）", "changelog": "加语气变量"})
        doc = r.get(f"/v1/templates/{tpl}").json()
        ok = c1.status_code == 200 and c2.status_code == 200 and doc["latest"] == 2
        r.check("08", "模板创建与版本迭代", ok, {"latest": doc.get("latest"),
                                                "versions": [v["version"] for v in doc["versions"]],
                                                "v2_variables": doc["versions"][1].get("variables")})
    except Exception as exc:
        r.check("08", "模板创建与版本迭代", False, str(exc))

    # 09 版本引用 + 变量替换
    try:
        v1 = r.post(f"/v1/templates/{tpl}/render", json={"version": 1,
                                                         "variables": {"question": "什么是限流？"}}).json()
        v2 = r.post(f"/v1/templates/{tpl}/render",
                    json={"variables": {"question": "什么是限流？", "tone": "幽默"}}).json()
        ok = ("V1回答：什么是限流？" == v1["content"] and "{{" not in v2["content"]
              and "幽默" in v2["content"] and v1["version"] == 1 and v2["version"] == 2)
        r.check("09", "版本引用与变量替换", ok, {"v1": v1["content"], "v2": v2["content"],
                                                "v1_version": v1["version"], "v2_version": v2["version"]})
    except Exception as exc:
        r.check("09", "版本引用与变量替换", False, str(exc))

    # 10 通过 /v1/chat 引用模板
    try:
        resp = r.chat({"model": model_a, "template": f"{tpl}@2",
                       "variables": {"question": "网关为什么要做统一抽象？", "tone": "专业"},
                       "max_output_tokens": 200})
        body = resp.json()
        tinfo = body.get("template") or {}
        ok = resp.status_code == 200 and tinfo.get("version") == 2 and tinfo.get("name") == tpl
        r.check("10", "对话中引用模板版本", ok, {"template": tinfo, "output_head": body.get("output_text", "")[:40]})
    except Exception as exc:
        r.check("10", "对话中引用模板版本", False, str(exc))

    # 11 模板缺变量 -> 统一错误码
    try:
        resp = r.chat({"model": model_a, "template": f"{tpl}@2", "variables": {"question": "X"}})
        body = resp.json()
        err = body.get("error") or {}
        ok = resp.status_code == 400 and err.get("code") == "template_render_error"
        r.check("11", "错误码统一封装", ok, {"http": resp.status_code, "code": err.get("code"),
                                             "message": err.get("message")})
    except Exception as exc:
        r.check("11", "错误码统一封装", False, str(exc))

    # 12 可观测性
    try:
        m = r.get("/v1/metrics").json()
        summary = m["summary"]
        model_stats = m["models"].get(model_a, {})
        ok = (summary["tokens_by_category"].get("input_tokens", 0) > 0
              and summary["tokens_by_category"].get("output_tokens", 0) > 0
              and (model_stats.get("first_token_ms") or {}).get("samples", 0) > 0)
        r.check("12", "Token与延迟可观测", ok, {
            "tokens_by_category": summary["tokens_by_category"],
            f"{model_a}.first_token_ms": model_stats.get("first_token_ms"),
            f"{model_a}.latency_total_ms": model_stats.get("latency_total_ms"),
            "calls_total": summary["calls_total"]})
    except Exception as exc:
        r.check("12", "Token与延迟可观测", False, str(exc))

    # 13 重试（切到仿真上游注入 2 次 500）
    try:
        sim = r.ensure_sim()
        r.override(model_a, f"{sim}/openai/v1", "sim-openai")
        r.sim_reset()
        r.sim_control(times=2)
        resp = r.chat({"model": model_a, "input": "重试测试", "max_output_tokens": 100})
        body = resp.json()
        ok = resp.status_code == 200 and body.get("attempts", 1) == 3
        r.check("13a", "指数退避重试后成功", ok, {"attempts": body.get("attempts"),
                                                 "http": resp.status_code,
                                                 "output_head": body.get("output_text", "")[:30]})

        r.sim_control(times=5)  # 超过最大重试次数
        resp2 = r.chat({"model": model_a, "input": "重试测试", "max_output_tokens": 100})
        body2 = resp2.json()
        err2 = body2.get("error") or {}
        ok2 = resp2.status_code >= 500 and err2.get("code") == "upstream_error"
        r.check("13b", "重试耗尽后统一报错", ok2, {"http": resp2.status_code, "code": err2.get("code"),
                                                  "attempts": (err2.get("details") or {}).get("attempts")})
        r.sim_reset()
    except Exception as exc:
        r.check("13a", "指数退避重试后成功", False, str(exc))
        r.check("13b", "重试耗尽后统一报错", False, str(exc))

    # 14 按模型独立限流
    try:
        r.set_ratelimit(model_a, rpm=6, burst=2)
        codes: list[int] = []
        for _ in range(6):
            resp = r.chat({"model": model_a, "input": "hi", "max_output_tokens": 32})
            codes.append(resp.status_code)
            if resp.status_code == 429:
                codes.append(429)
                break
        m = r.get("/v1/metrics").json()
        limited = m["models"].get(model_a, {}).get("rate_limited", 0)
        ok = 429 in codes and limited > 0
        r.check("14", "模型级限流返回429", ok, {"status_codes": codes,
                                               "rate_limited_total": limited,
                                               "bucket": m.get("rate_limit_buckets", {}).get(model_a)})
        r.set_ratelimit(model_a, rpm=60, burst=10)
    except Exception as exc:
        r.check("14", "模型级限流返回429", False, str(exc))

    # 15 未知模型
    try:
        resp = r.chat({"model": "not-exist-model", "input": "x"})
        err = (resp.json().get("error") or {})
        ok = resp.status_code == 404 and err.get("code") == "model_not_found"
        r.check("15", "未知模型统一404", ok, {"http": resp.status_code, "code": err.get("code")})
    except Exception as exc:
        r.check("15", "未知模型统一404", False, str(exc))

    # 恢复真实上游
    if r.mode != "sim":
        try:
            r.override(model_a, original_base)
        except Exception:
            pass
    try:
        r.client.delete(f"/v1/templates/{tpl}")
    except Exception:
        pass


# ---------------- 证据脱敏 ----------------
SECRET_RULES = [
    # Bearer 放在最前，避免 "Bearer sk-xxx" 被拆成两段、标签不准确
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted:token>"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{6,}"), "<redacted:api_key>"),
    (re.compile(r"(?i)((?:x-api-key|api[_-]?key)[\"'\s:=]+)[^\s\"',}]+"), r"\1<redacted>"),
    (re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"), r"\1<redacted:credentials>@"),
]


def scrub(obj: Any) -> Any:
    """递归清洗报告里可能混入的凭据；不修改原始结果对象。"""
    if isinstance(obj, str):
        out = obj
        for pattern, repl in SECRET_RULES:
            out = pattern.sub(repl, out)
        return out
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    return obj


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--mode", default="real", choices=["real", "sim"])
    ap.add_argument("--sim-port", type=int, default=DEFAULT_SIM_PORT,
                    help="本地仿真上游端口（干净环境复现时可换端口避免冲突）")
    ap.add_argument("--report", default="evidence/verify_report.json",
                    help="脱敏证据输出路径（默认 evidence/verify_report.json，该目录需提交）")
    args = ap.parse_args()

    r = Runner(args.base_url, args.mode, sim_port=args.sim_port)
    print(f"\n=== LLM 统一网关验收 | 模式={args.mode} | 网关={args.base_url} ===\n")
    try:
        run_checks(r)
    finally:
        r.stop_sim()

    passed = sum(1 for x in r.results if x["passed"])
    total = len(r.results)
    report = {
        "source": "scripts/verify.py",
        "command": f"python scripts/verify.py --mode {args.mode} --base-url {args.base_url} "
                   f"--sim-port {args.sim_port} --report {args.report}",
        "mode": args.mode,
        "gateway": args.base_url,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "passed": passed,
        "total": total,
        "results": r.results,
        "redaction": {
            "applied": True,
            "method": "写盘前对报告全文递归正则清洗",
            "rules": ["OpenAI / Anthropic 形态的 API Key", "Bearer token 值",
                      "x-api-key / api_key 赋值", "URL 中的 user:pass@ 凭据"],
            "never_logged": ["提示词原文", "模型回复全文（仅保留前 30 字样本）",
                             "上游请求头与 Authorization 值", ".env 内容"],
            "upstream_mode": args.mode,
            "note": "sim 模式使用本地协议仿真上游，不含任何真实凭据；real 模式需自行配置 .env，报告同样不含 Key。",
        },
    }
    report = scrub(report)

    out_dir = os.path.dirname(args.report) or "."
    os.makedirs(out_dir, exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n=== 结果：{passed}/{total} 通过 ===")
    print(f"脱敏证据：{args.report}")
    failed = [x for x in r.results if not x["passed"]]
    if failed:
        print("未通过项：")
        for x in failed:
            print(f"  - {x['id']} {x['name']}: {x['hint'] or x['evidence']}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
