#!/usr/bin/env bash
# 干净环境复现：模拟一次 fresh clone，逐字复跑 README 的安装 / 启动命令，再跑 17 项验收。
#
#   bash scripts/preflight.sh                # 默认端口 8123，仿真上游 8102
#   PREFLIGHT_PORT=8200 bash scripts/preflight.sh
#   bash scripts/preflight.sh --keep         # 保留临时目录便于排查
#
# 产出（均应随仓库提交）：
#   evidence/verify_report.json     17 项脱敏验收报告
#   evidence/preflight.log          干净环境全流程日志
#   evidence/gateway_startup.log    网关启动日志
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
EVIDENCE="$ROOT/evidence"
mkdir -p "$EVIDENCE"
LOG="$EVIDENCE/preflight.log"

PORT="${PREFLIGHT_PORT:-8123}"
ALT_PORT=$((PORT + 1))
SIM_PORT="${PREFLIGHT_SIM_PORT:-8102}"
KEEP=0
for arg in "$@"; do
    [ "$arg" = "--keep" ] && KEEP=1
done

TMP="$(mktemp -d /tmp/llm-gateway-preflight.XXXXXX)"
PIDS="$(mktemp /tmp/llm-gateway-preflight-pids.XXXXXX)"

cleanup() {
    # 先按记录的 PID 关进程，再按端口兜底清理，避免残留实例让下次复现出现"假 PASS"
    while read -r p; do
        [ -n "$p" ] && kill "$p" 2>/dev/null || true
    done < "$PIDS"
    for port in "$PORT" "$ALT_PORT"; do
        pkill -f "app[.]main:app.*--port ${port}" 2>/dev/null || true
    done
    rm -f "$PIDS"
    if [ "$KEEP" -eq 1 ]; then
        echo "[preflight] 保留干净环境目录：$TMP" >&2
    else
        rm -rf "$TMP"
    fi
}
trap cleanup EXIT

wait_health() {
    local port="$1" tries="$2" i
    for ((i = 0; i < tries; i++)); do
        if curl -fsS -m 2 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}

main() {
    echo "=============================================================="
    echo " LLM 统一网关 · 干净环境复现 (preflight)"
    echo " 时间      : $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo " 源仓库    : $ROOT"
    echo " 临时目录  : $TMP"
    echo " Python    : $(python -V 2>&1)"
    echo " 网关端口  : $PORT / $ALT_PORT   仿真上游端口: $SIM_PORT"
    echo "=============================================================="

    echo
    echo "--- [1/6] 环境自检：提交文件树 + 端口占用（确认远端包含实际源码） ---"
    python scripts/check_submission.py --root "$ROOT" || return 1
    # 若端口上已有服务，说明上次有残留；此时 wait_health 会命中旧实例造成假 PASS，必须拒绝继续
    for p in "$PORT" "$ALT_PORT"; do
        if curl -fsS -m 1 "http://127.0.0.1:${p}/healthz" >/dev/null 2>&1; then
            echo "端口 ${p} 已被占用（疑似残留网关实例），请先清理或设置 PREFLIGHT_PORT 换端口。" >&2
            return 1
        fi
    done
    echo "端口 $PORT / $ALT_PORT 空闲，不会发生旧实例假 PASS"

    echo
    echo "--- [2/6] 构造干净环境（仅拷贝应提交文件，剔除缓存/.env/运行时产物） ---"
    cp -a README.md requirements.txt .env.example .gitignore app scripts prompts "$TMP"/ || return 1
    mkdir -p "$TMP/evidence" && cp evidence/README.md "$TMP/evidence/" || return 1
    find "$TMP" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$TMP" -name "*.pyc" -delete 2>/dev/null || true
    rm -f "$TMP/.env"
    local n
    n="$(find "$TMP" -type f | wc -l | tr -d ' ')"
    echo "干净目录文件数：$n"
    echo "根目录内容：$(cd "$TMP" && ls -1 | tr '\n' ' ')"
    python scripts/check_submission.py --root "$TMP" --json | python -c \
        'import json,sys; d=json.load(sys.stdin); print("干净目录自检: required %s/%s, app/*.py = %s" % (d["required_present"], d["required_total"], len(d["app_python_files"])))' || return 1

    echo
    echo "--- [3/6] 复跑 README 安装命令：pip install -r requirements.txt ---"
    (cd "$TMP" && python -m pip install -q -r requirements.txt) || { echo "依赖安装失败"; return 1; }
    python -c 'import fastapi,uvicorn,httpx,pydantic,jsonschema;print(f"依赖就绪: fastapi={fastapi.__version__} httpx={httpx.__version__}")' || return 1

    echo
    echo "--- [4/6] 复跑 README 启动入口 A：bash scripts/run.sh ---"
    # exec 保证 PID 链不换壳，cleanup 能精确回收；重定向放在子 shell 外
    (cd "$TMP" && exec env PORT="$PORT" bash scripts/run.sh) > "$TMP/gateway.log" 2>&1 < /dev/null &
    echo $! >> "$PIDS"
    if ! wait_health "$PORT" 40; then
        echo "入口 A 启动失败，日志："
        tail -30 "$TMP/gateway.log"
        return 1
    fi
    echo -n "healthz -> "
    curl -fsS -m 3 "http://127.0.0.1:$PORT/healthz"
    echo

    echo
    echo "--- [4/6] 复跑 README 启动入口 B：uvicorn app.main:app ---"
    (cd "$TMP" && exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$ALT_PORT" \
        --log-level warning) > "$TMP/gateway_alt.log" 2>&1 < /dev/null &
    echo $! >> "$PIDS"
    if ! wait_health "$ALT_PORT" 40; then
        echo "入口 B 启动失败，日志："
        tail -30 "$TMP/gateway_alt.log"
        return 1
    fi
    echo -n "healthz -> "
    curl -fsS -m 3 "http://127.0.0.1:$ALT_PORT/healthz"
    echo
    echo "两种启动入口均通过（入口路径与仓库布局一致）"

    echo
    echo "--- [5/6] 运行验收脚本：python scripts/verify.py --mode sim ---"
    (cd "$TMP" && python scripts/verify.py --mode sim \
        --base-url "http://127.0.0.1:$PORT" \
        --sim-port "$SIM_PORT" \
        --report evidence/verify_report.json)
    local rc=$?
    echo "验收脚本退出码：$rc"
    [ "$rc" -eq 0 ] || return "$rc"

    echo
    echo "--- [6/6] 回写证据到仓库 evidence/ ---"
    cp "$TMP/evidence/verify_report.json" "$EVIDENCE/verify_report.json" || return 1
    cp "$TMP/gateway.log" "$EVIDENCE/gateway_startup.log" || return 1
    python - <<'PY'
import json, os
p = "evidence/verify_report.json"
d = json.load(open(p, encoding="utf-8"))
print(f"证据文件：{os.path.abspath(p)}")
print(f"结果：{d['passed']}/{d['total']} 通过 | 模式：{d['mode']} | 脱敏：{d['redaction']['applied']}")
print(f"检查项：{', '.join(x['id'] for x in d['results'] if x['passed'])}")
PY
    echo
    python scripts/check_submission.py --root "$ROOT"
    echo
    echo "=== PREFLIGHT PASS：干净环境安装/启动/双协议验证全部通过 ==="
    return 0
}

main "$@" 2>&1 | tee "$LOG"
rc=${PIPESTATUS[0]}
echo "[preflight] 全流程日志：$LOG（退出码 $rc）"
exit "$rc"
