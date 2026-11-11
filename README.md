# LLM 统一模型调用服务

一个模型网关：上层调用方只面对**一套 API**，下层可以是 **OpenAI Responses**、**OpenAI Chat Completions**、**Anthropic Messages** 三种互不相容的协议。按请求里的 `model` 字段动态路由到对应适配器，把鉴权、请求体结构、流式事件、返回格式、Token 统计口径的差异全部屏蔽掉。

```
                    POST /v1/chat  {model, input, stream, response_format, template}
                              │
        ┌─────────────────────┼─────────────────────┐
        │  统一抽象层 Gateway  │                     │
        │  ① 路由（model→协议）② 限流（按模型独立桶）│
        │  ③ 模板渲染+版本     ④ 重试（指数退避×3)  │
        │  ⑤ 结构化校验       ⑥ 观测（Token/延迟）  │
        └─────────────────────┬─────────────────────┘
                              │  适配器模式 Adapter
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
 OpenAIResponses      OpenAIChatCompletions   AnthropicMessages
 /v1/responses        /v1/chat/completions    /v1/messages
 Bearer xxx           Bearer xxx              x-api-key + anthropic-version
 instructions         messages[system]        system
 max_output_tokens    max_tokens              max_tokens
 text.format=schema   response_format         tools + tool_choice（强制）
```

---

## 0. 提交前自检（先跑这一步）

> **上一版被判待评的原因**：提交物只有 README、requirements、`.env.example`、模板 JSON 四个文档/配置文件，缺少 `app/` 与 `scripts/`，因此两种启动入口都因找不到模块失败、验证脚本无从复现。本节就是针对这一条的修复与防呆。

**所有命令一律在仓库根目录执行**——即能同时看见 `app/`、`scripts/`、`requirements.txt` 的那一层。若仓库根目录本身名为 `llm-gateway/`，进入一次即可；README 不再假设外层还套一层同名目录。

```bash
python scripts/check_submission.py     # 校验 27 个必需文件是否齐全
git add -A && git status --short       # 应看到 app/、scripts/、evidence/ 下全部文件，而不是只有 4 个
```

只有输出 `[PASS] 文件树完整，远端包含实际源码` 才提交；缺文件会逐条列清单。**不要逐个挑文件提交**，直接整包 `git add -A`（`.env`、`logs/`、`__pycache__` 已由 `.gitignore` 排除）。必须提交的实现文件：

| 分组 | 文件 | 数量 |
|---|---|---|
| 应用主体 | `app/{__init__,main,gateway,config,models,errors,http,retry,ratelimit,observability,templates}.py` | 11 |
| 适配器 | `app/adapters/{__init__,base,openai_responses,openai_chat,anthropic_messages}.py` | 5 |
| 脚本 | `scripts/{run.sh,verify.py,faulty_upstream.py,check_submission.py,preflight.sh}` | 5 |
| 根文件与模板 | `README.md`、`requirements.txt`、`.env.example`、`.gitignore`、`prompts/summarize.json` | 5 |
| 证据索引 | `evidence/README.md` | 1 |
| **合计** | | **27** |

`evidence/verify_report.json`、`evidence/preflight.log`、`evidence/gateway_startup.log` 由 `preflight.sh` 生成，也需一并提交。

`run.sh` 启动前也会做一次最小自检：缺 `app/main.py` 时直接打印可读报错，而不是让 uvicorn 抛 `ModuleNotFoundError`。

一键在干净环境复跑安装、两种启动入口与 17 项验证，并生成可提交的脱敏证据：

```bash
bash scripts/preflight.sh
```

---

## 1. 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env      # 填入真实 endpoint / key；离线仿真可不填
bash scripts/run.sh       # 入口 A
# 或等价的入口 B：uvicorn app.main:app --port 8000
```

两种入口都在仓库根目录执行，均已由 `scripts/preflight.sh` 在临时干净目录里实测通过。服务默认监听 `http://127.0.0.1:8000`，交互文档 `http://127.0.0.1:8000/docs`。

### 最小配置（.env）

```bash
# 模型 A：OpenAI Responses 协议
MODEL_A_ALIAS=deepseek-v4-pro
MODEL_A_PROTOCOL=openai_responses
MODEL_A_BASE_URL=https://api.openai.com/v1      # 网关自动拼 /responses
MODEL_A_API_KEY=sk-xxx
MODEL_A_UPSTREAM_MODEL=gpt-4o-mini

# 模型 B：Anthropic Messages 协议
MODEL_B_ALIAS=deepseek-v4-flash
MODEL_B_PROTOCOL=anthropic_messages
MODEL_B_BASE_URL=https://api.anthropic.com      # 网关自动拼 /v1/messages
MODEL_B_API_KEY=sk-ant-xxx
MODEL_B_UPSTREAM_MODEL=claude-sonnet-4-5
```

> **关于模型名的现实约束**：DeepSeek 官方目前只提供 **OpenAI Chat Completions 兼容**协议，不提供 Responses API，也不提供 Anthropic Messages 协议。因此：
> - 若你的 "DeepSeek v4 pro" 走 DeepSeek 官方，请把 `MODEL_A_PROTOCOL` 改成 `openai_chat_completions`、`MODEL_A_BASE_URL=https://api.deepseek.com/v1`；
> - "DeepSeek V4 flash → Anthropic Messages" 这一路需要指向**真正提供 Anthropic 协议**的服务（Anthropic 官方 Claude 系列，或兼容该协议的中转）；
> - 无论如何，**两个模型的 protocol 必须不同**才能达成训练目标。三种协议可任意两两组合。

---

## 2. 协议差异对照（适配器里被抹平的部分）

| 能力 | OpenAI Responses | OpenAI Chat Completions | Anthropic Messages |
|---|---|---|---|
| 鉴权 | `Authorization: Bearer` | `Authorization: Bearer` | `x-api-key` + `anthropic-version` |
| 系统提示 | `instructions` | `messages[0].role=system` | `system` |
| 长度字段 | `max_output_tokens` | `max_tokens` | `max_tokens`（必填） |
| 结构化输出 | `text.format={json_schema}` | `response_format` | 强制 `tool_choice`（json_schema→工具入参）；`json_object` 退化成 assistant prefill `{` |
| 流式事件 | `response.output_text.delta` / `response.completed` | `choices[0].delta.content` + `stream_options.include_usage` | `content_block_delta(text_delta / input_json_delta)` + `message_delta(usage)` |
| Token 字段 | `input_tokens/output_tokens`、`*.details.cached_tokens`、`reasoning_tokens` | `prompt_tokens/completion_tokens` + details | `input_tokens/output_tokens`、`cache_read_input_tokens` |

**统一输出**：无论走哪条协议，`/v1/chat` 的响应结构与 SSE 事件结构完全一致。

---

## 3. curl 示例

### 3.1 非流式调用（两种协议各一次）

```bash
# 模型 A（OpenAI Responses）
curl -s http://127.0.0.1:8000/v1/chat -H 'Content-Type: application/json' -d '{
  "model": "deepseek-v4-pro",
  "system": "你是一个简洁的助手",
  "input": "用一句话解释什么是令牌桶限流",
  "temperature": 0.3,
  "max_output_tokens": 300
}'

# 模型 B（Anthropic Messages）
curl -s http://127.0.0.1:8000/v1/chat -H 'Content-Type: application/json' -d '{
  "model": "deepseek-v4-flash",
  "input": "用一句话解释什么是指数退避",
  "max_output_tokens": 300
}'
```

响应（两个协议的字段名完全一致）：

```json
{
  "id": "req_xxx", "object": "llm.response",
  "model": "deepseek-v4-pro", "upstream_model": "gpt-4o-mini",
  "protocol": "openai_responses",
  "output_text": "令牌桶……",
  "structured": null,
  "finish_reason": "completed",
  "usage": {"input_tokens": 106, "output_tokens": 53, "total_tokens": 159,
            "cached_tokens": 11, "reasoning_tokens": 0, "tool_tokens": 0},
  "latency": {"first_token_ms": null, "upstream_ms": 812.5, "total_ms": 815.2},
  "attempts": 1,
  "template": null
}
```

### 3.2 流式输出（SSE，`stream=true`）

```bash
curl -N http://127.0.0.1:8000/v1/chat -H 'Content-Type: application/json' -d '{
  "model": "deepseek-v4-flash",
  "input": "分三点说明网关的价值",
  "stream": true,
  "max_output_tokens": 400
}'
```

```text
event: response.start
data: {"id":"req_xxx","model":"deepseek-v4-flash","protocol":"anthropic_messages",...}

event: response.delta
data: {"delta":"1."}

event: response.delta
data: {"delta":" 统一协议……"}

event: response.stop
data: {"finish_reason":"stop","usage":{"input_tokens":106,"output_tokens":66,...},
       "latency":{"first_token_ms":54.18,"total_ms":352.4},"attempts":1}
```

三种协议的流式输出都是这三类事件（`start` / `delta` / `stop`），Anthropic 的 `input_json_delta` 会在块结束时合并成一次完整 JSON 再下发，保证下游拿到的永远是合法 JSON。

### 3.3 结构化输出

```bash
curl -s http://127.0.0.1:8000/v1/chat -H 'Content-Type: application/json' -d '{
  "model": "deepseek-v4-pro",
  "input": "抽取人物信息：张三，28岁，擅长 Python 和 Go，资深网关工程师。",
  "response_format": {
    "type": "json_schema",
    "name": "person",
    "json_schema": {
      "type": "object",
      "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
        "skills": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"}
      },
      "required": ["name", "age", "skills"],
      "additionalProperties": false
    }
  },
  "temperature": 0.2
}'
```

响应里 `structured` 是**已通过 jsonschema 校验**的对象；不满足 schema 或不是合法 JSON 时返回 `structured_output_invalid`（502），不会出现"看着像 JSON 但字段缺失"的静默失败。

### 3.4 提示词模板与版本引用

```bash
# 创建模板（自动成为 v1）
curl -s -X POST http://127.0.0.1:8000/v1/templates -H 'Content-Type: application/json' -d '{
  "name": "summarize",
  "description": "文本摘要",
  "system": "你是内容分析助手",
  "content": "请总结：{{text}}",
  "changelog": "初版"
}'

# 迭代新版本（自动成为 v2，v1 永久保留可回退）
curl -s -X POST http://127.0.0.1:8000/v1/templates/summarize/versions -H 'Content-Type: application/json' -d '{
  "content": "请总结：{{text}}\n要求：三个要点，语气{{tone}}",
  "changelog": "加语气变量"
}'

# 渲染指定版本（不消耗 token，纯本地）
curl -s -X POST http://127.0.0.1:8000/v1/templates/summarize/render -H 'Content-Type: application/json' -d '{
  "version": 1, "variables": {"text": "网关要做统一抽象层"}
}'

# 调用时引用版本：name / name@2 / name@latest
curl -s http://127.0.0.1:8000/v1/chat -H 'Content-Type: application/json' -d '{
  "model": "deepseek-v4-pro",
  "template": "summarize@2",
  "variables": {"text": "网关要做统一抽象层", "tone": "专业"},
  "max_output_tokens": 400
}'
```

响应中的 `template` 字段回显 `{name, version, rendered_chars}`，方便追溯"这次到底用的哪一版提示词"。缺少变量时返回 `400 template_render_error` 并列出缺失变量名。

### 3.5 可观测性

```bash
curl -s http://127.0.0.1:8000/v1/metrics          # JSON：分类 Token、延迟分位、错误/重试/限流计数
curl -s http://127.0.0.1:8000/v1/metrics/prometheus   # Prometheus 文本格式
curl -s "http://127.0.0.1:8000/v1/calls?limit=10"     # 最近 N 次调用明细
```

每次调用的明细同时以 JSONL 追加落盘到 `logs/calls.jsonl`，含：

- `usage`：`input_tokens / output_tokens / total_tokens / cached_tokens / reasoning_tokens / tool_tokens`
- `latency`：`first_token_ms`（首 Token 延迟，流式才有）、`upstream_ms`、`total_ms`
- `attempts`：实际尝试次数（>1 说明发生了重试）
- `error_code`、`model`、`protocol`、`stream`、`template`

### 3.6 错误码、重试、限流

```bash
# 触发限流（连续快速请求打满令牌桶）
for i in $(seq 1 8); do
  curl -s -o /dev/null -w "%{http_code} " http://127.0.0.1:8000/v1/chat \
    -H 'Content-Type: application/json' -d '{"model":"deepseek-v4-pro","input":"hi","max_output_tokens":16}'
done
# -> 200 200 429 429 ... ，429 响应带 Retry-After 头
```

统一错误结构：

```json
{"error": {"code": "rate_limited", "message": "模型级限流触发", "type": "GatewayError",
           "http_status": 429, "retryable": false, "upstream_status": null,
           "details": {"retry_after_s": 1.83}, "request_id": "req_xxx"}}
```

| code | HTTP | 可重试 | 含义 |
|---|---|---|---|
| `invalid_request` / `invalid_json_schema` | 400 | 否 | 请求体或 schema 非法 |
| `template_not_found` / `template_version_not_found` | 404 | 否 | 模板或版本不存在 |
| `template_render_error` | 400 | 否 | 缺少模板变量 |
| `model_not_found` | 404 | 否 | 未知模型别名 |
| `unauthorized` | 401 | 否 | 网关鉴权失败 |
| `rate_limited` | 429 | 否 | 模型级限流（带 Retry-After） |
| `upstream_rate_limited` | 429 | 是 | 上游限流 |
| `upstream_auth_failed` | 401 | 否 | 上游 Key/base_url 配错 |
| `upstream_timeout` | 408 | 是 | 上游超时 |
| `upstream_error` / `upstream_unavailable` | 502/503 | 是 | 上游 5xx / 连接失败 |
| `upstream_bad_response` | 502 | 否 | 上游返回无法解析 |
| `structured_output_invalid` | 502 | 否 | 输出不满足 JSON Schema |

重试策略：`max_attempts=3`，`delay = min(0.5 × 2^(n-1), 8) + jitter`，**只对可重试错误生效**（5xx/429/超时/连接失败；401/400 立即失败）。响应中的 `attempts` 与 `error.details.retry_history` 是可核对的证据。
限流：每个模型别名一个独立令牌桶（`MODEL_*_RPM` + `MODEL_*_BURST`），A 打满不影响 B。

---

## 4. 验收脚本与证据

```bash
# 前置：网关已在 8000 端口运行（bash scripts/run.sh）
python scripts/verify.py --mode sim     # 全量走本地协议仿真上游，无需任何 Key
python scripts/verify.py --mode real    # 走 .env 里配置的真实上游
```

`--mode sim` 用 `scripts/faulty_upstream.py`，它真实模拟三种协议的请求体、SSE 事件和返回结构，并能注入 500 故障，因此**离线即可验证全部链路**。`--mode real` 下，重试与限流两项仍会临时把模型 A 指向本地仿真上游（无法对真实服务注入 500），结束后自动还原。

可用参数：`--base-url`（默认 `http://127.0.0.1:8000`）、`--sim-port`（默认 8101，干净环境复现可换端口）、`--report`（默认 `evidence/verify_report.json`）。

> **证据路径修复**：上一版报告写在 `logs/verify_report.json`，而 `.gitignore` 忽略了整个 `logs/`，导致报告根本进不了提交物——这也是"17/17 无法复现"的一个直接原因。现在报告默认写入 `evidence/`，该目录明确不忽略，必须随源码一起提交。

覆盖的 17 项检查：

| 编号 | 检查项 | 证据 |
|---|---|---|
| 00-01 | 健康检查、双协议模型注册 | 两个 alias 的 protocol 不同 |
| 02-03 | 两协议非流式调用 | `protocol` 字段 + `usage` + `latency` |
| 04-05 | 两协议流式 SSE | delta 分块数、`first_token_ms`、`response.stop.usage` |
| 06-07 | 两协议结构化输出 | `structured` 通过 jsonschema 校验 |
| 08-09 | 模板创建/迭代、版本引用与变量替换 | v1 与 v2 渲染结果差异 |
| 10-11 | 对话中引用模板版本、缺变量统一报错 | `template.version`、400 `template_render_error` |
| 12 | Token 分类 + 首 Token 延迟 | `tokens_by_category`、`first_token_ms.samples` |
| 13a/13b | 注入 2 次 500 → 重试成功；注入 5 次 → 重试耗尽 | `attempts=3` / 502 `upstream_error` |
| 14 | 模型级限流 | 状态码序列出现 429 + `rate_limited_total>0` |
| 15 | 未知模型 | 404 `model_not_found` |

退出码非 0 表示有未通过项。当前提交的 `evidence/verify_report.json` 为干净环境实测结果：**17/17 通过**。

### 4.1 干净环境复现（对应"确认入口路径与仓库布局一致"）

```bash
bash scripts/preflight.sh
```

该脚本会：① `check_submission.py` 校验文件树 → ② `mktemp` 建临时目录，只拷贝应提交文件（剔除 `__pycache__`、`.env`、`logs/`，模拟一次 fresh clone）→ ③ 复跑 `pip install -r requirements.txt` → ④ 分别用 `bash scripts/run.sh` 与 `uvicorn app.main:app` 两种入口启动并打 `/healthz` → ⑤ 跑 `verify.py --mode sim` 17 项 → ⑥ 把脱敏报告和日志回写 `evidence/`。全流程日志在 `evidence/preflight.log`，退出码即结论。

### 4.2 关于"真实调用证据"（如实说明边界）

当前提交的证据是 **sim 模式**：它验证的是网关自身的路由、协议翻译、流式、结构化、模板、观测、重试、限流全部链路，不含任何真实凭据，可 100% 离线复现。**它没有、也不会冒充双模型真实调用证据**——本次未提供可用的真实 endpoint 与 API Key。

补齐真实证据只需在 `.env` 填好两个上游后执行：

```bash
python scripts/verify.py --mode real --report evidence/verify_report_real.json
```

报告在写盘前会递归脱敏：`sk-*` / `sk-ant-*` Key、`Bearer` token、`x-api-key` / `api_key` 赋值、URL 中的 `user:pass@` 凭据一律替换为 `<redacted>`；**从不记录**提示词原文、模型回复全文（只留前 30 字样本）、上游请求头与 `.env` 内容。运行时明细 `logs/calls.jsonl` 同样只记 `model / protocol / usage / latency / attempts / template.version / rendered_chars`，不含任何正文或 Key（`logs/` 已被忽略，不提交）。报告里的 `redaction` 字段写明了清洗规则，可直接核对。

---

## 5. 目录结构（仓库根目录 = 以下命令的执行目录）

```
./
├── app/                             ← 必须整包提交（上一版缺失）
│   ├── main.py                     FastAPI 入口、SSE 输出、统一异常处理
│   ├── gateway.py                  编排：路由/限流/模板/重试/结构化/观测
│   ├── config.py                   模型路由表与限流额度（环境变量驱动）
│   ├── models.py                   统一请求/响应/SSE 契约
│   ├── errors.py                   统一错误码体系
│   ├── http.py                     共享 httpx 客户端 + 传输异常翻译
│   ├── retry.py                    指数退避重试
│   ├── ratelimit.py                按模型独立令牌桶
│   ├── observability.py            Token/延迟聚合、JSONL 落盘、Prometheus 输出
│   ├── templates.py                模板存储、版本、{{变量}} 渲染
│   └── adapters/
│       ├── base.py                 适配器抽象基类
│       ├── openai_responses.py     OpenAI Responses 协议
│       ├── openai_chat.py          OpenAI Chat Completions 协议（兼容备选）
│       └── anthropic_messages.py   Anthropic Messages 协议
├── scripts/                         ← 必须整包提交（上一版缺失）
│   ├── run.sh                      一键启动（含启动前文件树自检）
│   ├── verify.py                   17 项验收脚本 + 报告脱敏
│   ├── faulty_upstream.py          协议仿真 + 故障注入上游（仅验证用）
│   ├── check_submission.py         提交前文件树完整性自检
│   └── preflight.sh                干净环境复现 + 生成证据
├── prompts/                        提示词模板（每个模板一个 JSON，含全部版本）
├── evidence/                        ← 必须提交；.gitignore 明确不忽略
│   ├── verify_report.json          17 项脱敏验收报告
│   ├── preflight.log               干净环境全流程日志
│   ├── gateway_startup.log         网关启动日志
│   └── README.md                   证据索引与脱敏规则说明
├── logs/                           运行时 JSONL，.gitignore 忽略，不提交
├── .env.example
├── .gitignore
├── README.md
└── requirements.txt
```

## 6. 扩展新协议

1. 在 `app/adapters/` 下新增适配器，继承 `BaseAdapter`，实现 `endpoint/headers/build_payload/parse_response/parse_stream_chunk/call/stream`；
2. 在 `app/gateway.py::build_adapter` 里注册协议名；
3. 在 `app/config.py::SUPPORTED_PROTOCOLS` 加入该协议名；
4. `.env` 里配置 `MODEL_x_PROTOCOL=<新协议>`。

上层 `/v1/chat` 契约与所有可观测、限流、重试、模板能力**零改动**即可复用。

## 7. 常见问题

- **`ModuleNotFoundError: No module named 'app'` / 启动入口失败**：说明提交时漏传了 `app/`。在仓库根跑 `python scripts/check_submission.py`，按缺失清单整包补交 `app/` 与 `scripts/`，再跑 `bash scripts/preflight.sh`。
- **`bash scripts/run.sh` 立即报"仓库根目录缺少实现文件"**：这是 `run.sh` 内置自检在生效，检查当前目录是否为仓库根、实现目录是否齐全（比 uvicorn 的堆栈更可读）。
- **没有 `.env` 能不能启动**：能。缺失时只用内置默认配置，离线 `sim` 模式完全够用；真实调用才需要 `cp .env.example .env` 并填 Key。
- **启动时日志提示"未配置 API Key"**：对应 `MODEL_*_API_KEY` 为空，请求会直接返回 400（不浪费重试）。
- **`upstream_bad_response`**：多为 `base_url` 层级写错，Responses 应填到 `…/v1`，Anthropic 应填到 `https://api.anthropic.com`（网关各自拼接 `/responses`、`/v1/messages`）。
- **结构化输出偶发失败**：降低 `temperature`，或换用对 schema 遵循更好的模型；Anthropic 走的是强制 tool_use，比 prefill 稳定得多。
- **限流太紧/太松**：`MODEL_A_RPM` / `MODEL_A_BURST`，或运行时用 `POST /admin/ratelimit/{alias}?rpm=&burst=` 调整（生产建议 `ENABLE_ADMIN=false`）。
- **端口冲突**：`PORT=8001 bash scripts/run.sh`；干净环境复现用 `PREFLIGHT_PORT=8200 PREFLIGHT_SIM_PORT=8202 bash scripts/preflight.sh`。
