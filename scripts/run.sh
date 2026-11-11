#!/usr/bin/env bash
# 一键启动：加载 .env 后拉起网关。
# 可在任意路径调用：脚本会先切到仓库根目录（即包含 app/ 和 requirements.txt 的那一层）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# 启动前自检：远端若漏传实现目录，这里直接给出可读报错，而不是让 uvicorn 抛 ModuleNotFoundError
MISSING=()
for f in app/main.py app/gateway.py app/adapters/base.py requirements.txt; do
    [ -f "$f" ] || MISSING+=("$f")
done
if [ ${#MISSING[@]} -gt 0 ]; then
    echo "[run.sh] 启动失败：仓库根目录缺少实现文件 -> ${MISSING[*]}" >&2
    echo "[run.sh] 当前目录：$ROOT" >&2
    echo "[run.sh] 请确认提交时包含 app/ 与 scripts/ 全部源码（可运行 python scripts/check_submission.py 自检）。" >&2
    exit 1
fi

# .env 可选：不存在时使用内置默认配置（离线仿真模式无需任何 Key）
set -a
if [ -f .env ]; then
    # shellcheck disable=SC1091
    source .env
    echo "[run.sh] 已加载 .env"
else
    echo "[run.sh] 未找到 .env，使用默认配置（真实 API 调用需 cp .env.example .env 并填 Key）"
fi
set +a

exec uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" --log-level "${LOG_LEVEL:-info}"
