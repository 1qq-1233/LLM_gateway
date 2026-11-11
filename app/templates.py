"""提示词模板：文件化存储 + 版本管理 + 变量渲染。

存储格式 prompts/<name>.json：
{
  "name": "summarize",
  "description": "...",
  "latest": 2,
  "versions": [
    {"version": 1, "system": "...", "content": "总结：{{text}}",
     "variables": ["text"], "created_at": "...", "changelog": "初版"},
    ...
  ]
}
引用语法：name           -> 最新版
         name@2         -> 指定版本
         name@latest    -> 最新版
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .errors import GatewayError

VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def parse_ref(ref: str) -> tuple[str, int | None]:
    """'summarize@2' -> ('summarize', 2)；'summarize' -> ('summarize', None)。"""
    ref = (ref or "").strip()
    if "@" in ref:
        name, _, ver = ref.partition("@")
        name = name.strip()
        if ver.strip().lower() in ("latest", "", "head"):
            return name, None
        if not ver.strip().isdigit():
            raise GatewayError("invalid_request", f"模板版本引用非法：{ref}")
        return name, int(ver)
    return ref, None


def render(template: str, variables: dict[str, Any], *, strict: bool = True) -> str:
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key in variables:
            return str(variables[key])
        missing.append(key)
        return ""

    out = VAR_PATTERN.sub(sub, template)
    if missing and strict:
        raise GatewayError(
            "template_render_error",
            f"缺少模板变量：{sorted(set(missing))}",
            details={"missing": sorted(set(missing))},
        )
    return out


def extract_variables(template: str) -> list[str]:
    return sorted(set(VAR_PATTERN.findall(template or "")))


class TemplateStore:
    def __init__(self, directory: str) -> None:
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, name: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name or ""):
            raise GatewayError("invalid_request", f"模板名非法：{name}")
        return os.path.join(self.dir, f"{name}.json")

    # ---------- CRUD ----------
    def create(self, name: str, description: str, system: str | None, content: str, changelog: str) -> dict:
        path = self._path(name)
        if os.path.exists(path):
            raise GatewayError("invalid_request", f"模板 '{name}' 已存在，请新增版本")
        doc = {
            "name": name,
            "description": description,
            "latest": 1,
            "versions": [{
                "version": 1,
                "system": system,
                "content": content,
                "variables": extract_variables(f"{system or ''}\n{content}"),
                "created_at": _now(),
                "changelog": changelog,
            }],
        }
        self._write(path, doc)
        return doc

    def add_version(self, name: str, system: str | None, content: str, changelog: str) -> dict:
        doc = self.get(name)
        ver = doc["latest"] + 1
        doc["versions"].append({
            "version": ver,
            "system": system,
            "content": content,
            "variables": extract_variables(f"{system or ''}\n{content}"),
            "created_at": _now(),
            "changelog": changelog,
        })
        doc["latest"] = ver
        self._write(self._path(name), doc)
        return doc

    def get(self, name: str) -> dict:
        path = self._path(name)
        if not os.path.exists(path):
            raise GatewayError("template_not_found", f"模板 '{name}' 不存在", details={"path": path})
        return self._read(path)

    def list(self) -> list[dict]:
        out = []
        for fn in sorted(os.listdir(self.dir)):
            if fn.endswith(".json"):
                doc = self._read(os.path.join(self.dir, fn))
                out.append({
                    "name": doc["name"],
                    "description": doc.get("description", ""),
                    "latest": doc["latest"],
                    "versions": [v["version"] for v in doc["versions"]],
                })
        return out

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not os.path.exists(path):
            raise GatewayError("template_not_found", f"模板 '{name}' 不存在")
        os.remove(path)

    def resolve(self, ref: str, variables: dict[str, Any]) -> tuple[dict, int, str, str | None]:
        """返回 (version_doc, version, rendered_content, rendered_system)。"""
        name, ver = parse_ref(ref)
        doc = self.get(name)
        target = ver if ver is not None else doc["latest"]
        for v in doc["versions"]:
            if v["version"] == target:
                content = render(v["content"] or "", variables)
                system = render(v["system"] or "", variables) if v.get("system") else None
                return v, target, content, system
        raise GatewayError(
            "template_version_not_found",
            f"模板 '{name}' 无版本 {target}（可用：{[v['version'] for v in doc['versions']]}）",
        )

    # ---------- io ----------
    @staticmethod
    def _read(path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _write(path: str, doc: dict) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
