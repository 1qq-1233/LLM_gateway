"""可观测性：每次调用的 Token 分类统计 + 延迟（含首 Token 延迟）+ 错误/重试/限流计数。

- 明细落盘 logs/calls.jsonl（可接 ELK / Prometheus 抓取）
- 内存聚合供 /v1/metrics 查询
- 同时暴露 Prometheus 文本格式
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, asdict, field
from typing import Any

from .models import Latency, Usage


@dataclass
class CallRecord:
    request_id: str
    ts: float
    model: str
    upstream_model: str
    protocol: str
    stream: bool
    status: str                 # ok / error
    error_code: str | None = None
    attempts: int = 1
    usage: dict[str, int] = field(default_factory=dict)
    latency: dict[str, float | None] = field(default_factory=dict)
    template: dict | None = None
    finish_reason: str | None = None


class MetricsStore:
    def __init__(self, log_dir: str = "logs", capacity: int = 2000) -> None:
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, "calls.jsonl")
        self._lock = threading.Lock()
        self._records: deque[CallRecord] = deque(maxlen=capacity)

        self.calls_total: dict[tuple[str, str, str], int] = defaultdict(int)   # (model, protocol, status)
        self.errors_total: dict[str, int] = defaultdict(int)                   # error_code
        self.retries_total: dict[str, int] = defaultdict(int)                  # model -> 重试次数
        self.rate_limited_total: dict[str, int] = defaultdict(int)             # model -> 被限流次数
        self.tokens_total: dict[tuple[str, str], int] = defaultdict(int)       # (model, category)
        self.latencies_ms: dict[str, list[float]] = defaultdict(list)          # model -> total_ms
        self.first_token_ms: dict[str, list[float]] = defaultdict(list)         # model -> 首 token 延迟
        self.stream_first_token_ms: dict[str, list[float]] = defaultdict(list)

    # ---------------- 记录 ----------------
    def record(self, rec: CallRecord) -> None:
        with self._lock:
            self._records.append(rec)
            self.calls_total[(rec.model, rec.protocol, rec.status)] += 1
            if rec.error_code:
                self.errors_total[rec.error_code] += 1
            if rec.attempts > 1:
                self.retries_total[rec.model] += rec.attempts - 1
            if rec.error_code == "rate_limited":
                self.rate_limited_total[rec.model] += 1
            for k, v in (rec.usage or {}).items():
                if v:
                    self.tokens_total[(rec.model, k)] += int(v)
            if rec.latency.get("total_ms"):
                self.latencies_ms[rec.model].append(rec.latency["total_ms"])
            ft = rec.latency.get("first_token_ms")
            if ft is not None:
                self.first_token_ms[rec.model].append(ft)
                if rec.stream:
                    self.stream_first_token_ms[rec.model].append(ft)
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
            except OSError:
                pass

    def record_usage(self, model: str, usage: Usage) -> None:
        pass  # usage 随 CallRecord 一起记录，避免重复计数

    def bump_rate_limited(self, model: str) -> None:
        with self._lock:
            self.rate_limited_total[model] += 1
            self.errors_total["rate_limited"] += 1

    # ---------------- 查询 ----------------
    @staticmethod
    def _pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        idx = min(len(s) - 1, max(0, int(round((p / 100) * (len(s) - 1)))))
        return round(s[idx], 2)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            models: dict[str, Any] = {}
            for (model, protocol) in {(m, p) for (m, p, _) in self.calls_total}:
                ok = self.calls_total.get((model, protocol, "ok"), 0)
                err = self.calls_total.get((model, protocol, "error"), 0)
                lat = self.latencies_ms.get(model, [])
                ft = self.first_token_ms.get(model, [])
                models[model] = {
                    "protocol": protocol,
                    "calls_total": ok + err,
                    "calls_ok": ok,
                    "calls_error": err,
                    "tokens": {
                        cat: self.tokens_total.get((model, cat), 0)
                        for cat in ("input_tokens", "output_tokens", "cached_tokens",
                                    "reasoning_tokens", "tool_tokens", "total_tokens")
                    },
                    "latency_total_ms": {
                        "avg": round(sum(lat) / len(lat), 2) if lat else None,
                        "p50": self._pct(lat, 50), "p95": self._pct(lat, 95), "p99": self._pct(lat, 99),
                        "max": round(max(lat), 2) if lat else None,
                    },
                    "first_token_ms": {
                        "samples": len(ft),
                        "avg": round(sum(ft) / len(ft), 2) if ft else None,
                        "p50": self._pct(ft, 50), "p95": self._pct(ft, 95),
                        "min": round(min(ft), 2) if ft else None,
                        "max": round(max(ft), 2) if ft else None,
                    },
                    "retries": self.retries_total.get(model, 0),
                    "rate_limited": self.rate_limited_total.get(model, 0),
                }
            total_tokens = defaultdict(int)
            for (_m, cat), v in self.tokens_total.items():
                total_tokens[cat] += v
            all_lat = [x for v in self.latencies_ms.values() for x in v]
            all_ft = [x for v in self.first_token_ms.values() for x in v]
            return {
                "uptime_s": None,
                "summary": {
                    "calls_total": sum(self.calls_total.values()),
                    "calls_ok": sum(v for (m, p, s), v in self.calls_total.items() if s == "ok"),
                    "calls_error": sum(v for (m, p, s), v in self.calls_total.items() if s == "error"),
                    "tokens_by_category": dict(total_tokens),
                    "latency_total_ms": {"avg": round(sum(all_lat) / len(all_lat), 2) if all_lat else None,
                                         "p95": self._pct(all_lat, 95)},
                    "first_token_ms": {"avg": round(sum(all_ft) / len(all_ft), 2) if all_ft else None,
                                       "p95": self._pct(all_ft, 95)},
                },
                "errors_by_code": dict(self.errors_total),
                "models": models,
                "log_file": self.log_path,
            }

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [asdict(r) for r in list(self._records)[-limit:]]

    def prometheus(self) -> str:
        snap = self.snapshot()
        lines: list[str] = []
        lines.append("# HELP llm_gateway_calls_total 网关调用总数")
        lines.append("# TYPE llm_gateway_calls_total counter")
        for (model, protocol, status), v in self.calls_total.items():
            lines.append(f'llm_gateway_calls_total{{model="{model}",protocol="{protocol}",status="{status}"}} {v}')
        lines.append("# HELP llm_gateway_tokens_total Token 分类累计")
        lines.append("# TYPE llm_gateway_tokens_total counter")
        for (model, cat), v in self.tokens_total.items():
            lines.append(f'llm_gateway_tokens_total{{model="{model}",category="{cat}"}} {v}')
        for name, mapping in (("llm_gateway_retries_total", self.retries_total),
                              ("llm_gateway_rate_limited_total", self.rate_limited_total)):
            lines.append(f"# TYPE {name} counter")
            for model, v in mapping.items():
                lines.append(f'{name}{{model="{model}"}} {v}')
        lines.append("# TYPE llm_gateway_latency_ms gauge")
        for model, info in snap["models"].items():
            for key in ("avg", "p95"):
                val = info["latency_total_ms"][key]
                if val is not None:
                    lines.append(f'llm_gateway_latency_ms{{model="{model}",quantile="{key}"}} {val}')
            ft = info["first_token_ms"]["avg"]
            if ft is not None:
                lines.append(f'llm_gateway_first_token_ms{{model="{model}",quantile="avg"}} {ft}')
        return "\n".join(lines) + "\n"


def usage_to_dict(u: Usage) -> dict[str, int]:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "total_tokens": u.total_tokens,
        "cached_tokens": u.cached_tokens,
        "reasoning_tokens": u.reasoning_tokens,
        "tool_tokens": u.tool_tokens,
    }


def latency_to_dict(l: Latency) -> dict[str, float | None]:
    return {"first_token_ms": l.first_token_ms, "upstream_ms": l.upstream_ms, "total_ms": l.total_ms}
