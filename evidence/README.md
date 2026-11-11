# 验收证据索引

本目录**必须随源码一起提交**（`.gitignore` 明确不忽略它）。上一版把报告写在被忽略的 `logs/` 里，导致"17/17"无从核对，这是本次修复的重点之一。

| 文件 | 内容 | 生成方式 |
|---|---|---|
| `verify_report.json` | 17 项验收结果，含每项的 evidence 字段与脱敏声明 | `scripts/verify.py --mode sim` |
| `preflight.log` | 干净环境全流程日志：文件树自检 → 依赖安装 → 两种启动入口 → 17 项验证 | `bash scripts/preflight.sh` |
| `gateway_startup.log` | 干净环境里 `bash scripts/run.sh` 的启动日志 | `bash scripts/preflight.sh` |
| `README.md` | 本文件：证据索引、复核命令、脱敏规则 | 手写 |

## 对应助教四条改进

| 改进要求 | 对应证据 |
|---|---|
| 补交完整 `app/`、`scripts/` 实现源码，确认远端文件树包含实际源码 | `preflight.log` [1/6] 段：`check_submission.py` 在源仓库与临时干净目录各跑一次，输出 required 27/27、`app/*.py` 数量；`scripts/check_submission.py` 可随时复跑 |
| 在干净环境复跑 README 安装及启动命令，确认入口路径与仓库布局一致 | `preflight.log` [2/6]~[4/6] 段：`mktemp` 临时目录 + `pip install -r requirements.txt` + `bash scripts/run.sh` 与 `uvicorn app.main:app` 两种入口各自返回 `/healthz` |
| 补交可执行验证脚本，复验双协议请求、流式、JSON、模板、观测、重试与模型隔离限流 | `verify_report.json` 中 00–15 共 17 项，`passed/total` 字段；`preflight.log` [5/6] 段含逐项 PASS 输出 |
| 脱敏真实调用证据 | 报告 `redaction` 字段 + 下方脱敏规则；`logs/calls.jsonl` 不提交、不记录正文与 Key |

## 复核命令

```bash
# 在仓库根目录执行
python scripts/check_submission.py      # 文件树完整 → [PASS]
bash scripts/preflight.sh                # 干净环境一键复现，重新生成本目录证据
python -c "import json;d=json.load(open('evidence/verify_report.json'));print(d['passed'],'/',d['total'],d['redaction'])"
```

## 脱敏规则（`verify.py` 写盘前递归执行）

- 替换：`sk-*` / `sk-ant-*` 形态的 API Key、`Bearer <token>`、`x-api-key` / `api_key` 赋值、URL 中的 `user:pass@` 凭据 → `<redacted>`。
- 从不记录：提示词原文、模型回复全文（仅保留前 30 字样本）、上游请求头与 `Authorization` 值、`.env` 内容。
- 运行时明细 `logs/calls.jsonl`（本地、不提交）只含 `model / protocol / stream / status / error_code / attempts / usage / latency / template.version / rendered_chars`。

## 边界声明（重要）

本目录现有报告来自 **`--mode sim`**：使用本地 `scripts/faulty_upstream.py` 真实模拟 OpenAI Responses、OpenAI Chat Completions、Anthropic Messages 三种协议的请求体、SSE 事件与返回结构，并注入 500 故障与限流条件。它验证的是**网关自身全链路**，无需任何 API Key，可离线 100% 复现。

**它不包含、也不冒充双模型真实调用证据**——本次未提供可用的真实 endpoint 与 API Key。补齐方式：填好 `.env` 后执行

```bash
python scripts/verify.py --mode real --report evidence/verify_report_real.json
```

生成的报告同样经过上述脱敏流程，可安全提交。`--mode real` 下仅"重试"与"限流"两项会临时切到本地仿真上游（无法对真实服务注入 500），并在结束后还原，其余检查全部打到真实上游，这一点在报告的 `command` 与各项 evidence 里可核对。
