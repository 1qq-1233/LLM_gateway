#!/usr/bin/env python3
"""提交前自检：确认仓库根目录包含全部实现源码，而不是只有文档与配置。

用法：
    python scripts/check_submission.py                # 检查当前目录
    python scripts/check_submission.py --root /path   # 检查指定目录
    python scripts/check_submission.py --json         # 机器可读输出（CI 用）

退出码 0 = 完整；1 = 有缺失（打印缺失清单）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REQUIRED = [
    # 入口与配置
    "README.md", "requirements.txt", ".env.example", ".gitignore",
    # 应用源码
    "app/__init__.py", "app/main.py", "app/gateway.py", "app/config.py",
    "app/models.py", "app/errors.py", "app/http.py", "app/retry.py",
    "app/ratelimit.py", "app/observability.py", "app/templates.py",
    # 适配器
    "app/adapters/__init__.py", "app/adapters/base.py",
    "app/adapters/openai_responses.py", "app/adapters/openai_chat.py",
    "app/adapters/anthropic_messages.py",
    # 脚本与模板
    "scripts/run.sh", "scripts/verify.py", "scripts/faulty_upstream.py",
    "scripts/check_submission.py", "scripts/preflight.sh",
    "prompts/summarize.json",
    # 证据索引
    "evidence/README.md",
]

OPTIONAL_BUT_EXPECTED = ["evidence/verify_report.json", "evidence/preflight.log"]


def check(root: str) -> dict:
    missing = [p for p in REQUIRED if not os.path.isfile(os.path.join(root, p))]
    optional_missing = [p for p in OPTIONAL_BUT_EXPECTED if not os.path.isfile(os.path.join(root, p))]
    py_files = []
    for base, _dirs, files in os.walk(os.path.join(root, "app")):
        for fn in files:
            if fn.endswith(".py"):
                py_files.append(os.path.relpath(os.path.join(base, fn), root))
    return {
        "root": os.path.abspath(root),
        "required_total": len(REQUIRED),
        "required_present": len(REQUIRED) - len(missing),
        "missing": missing,
        "optional_missing": optional_missing,
        "app_python_files": sorted(py_files),
        "passed": not missing,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = check(args.root)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"仓库根目录：{result['root']}")
        print(f"必需文件：{result['required_present']}/{result['required_total']}")
        print(f"app/ 下 Python 源码：{len(result['app_python_files'])} 个")
        if result["missing"]:
            print("\n[FAIL] 以下实现文件缺失，提交后服务将无法启动：")
            for p in result["missing"]:
                print(f"  - {p}")
            print("\n请整包提交 app/ 与 scripts/，不要只提交 README / requirements / .env.example / prompts。")
        else:
            print("\n[PASS] 文件树完整，远端包含实际源码。")
        if result["optional_missing"]:
            print(f"提示：证据文件尚未生成 -> {', '.join(result['optional_missing'])}")
            print("      运行 bash scripts/preflight.sh 可一键生成。")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
