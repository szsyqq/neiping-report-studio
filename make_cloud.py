"""把项目整理成一份可云端部署的瘦身副本（`_cloud/`）。

为什么不是直接部署项目根目录：根目录里 `data/`（98MB 历史 PDF）与 `output/`
（19MB 历次成品）都是本地作业痕迹，云端实例一件都用不上，反而让上传与冷启动变慢。
云端要的只有：程序、模板、依赖声明、启动脚本。

密钥从本机现有配置读出来写进 `_cloud/start.sh`（该目录在 .gitignore 里，不入库）：
    财汇 MCP 的 url 与 x-api-key   ← ~/.workbuddy/mcp.json
    DeepSeek 的 url 与 apiKey      ← ~/.workbuddy/models.json
访问口令另生成一个随机串，不用默认的 ceshi——云端是对外的。

用法：
    python3 make_cloud.py            # 重新生成 _cloud/
    python3 make_cloud.py --keep-pw  # 保留上次生成的口令
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "_cloud"
HOME = Path.home()
MCP_JSON = HOME / ".workbuddy" / "mcp.json"
MODELS_JSON = HOME / ".workbuddy" / "models.json"

CAIHUI_NAMES = ("caihui_mcp", "caihui-mcp", "caihui")
# 云端只需要一个能跑的模型，优先 DeepSeek（当前本机实测唯一可用的）
MODEL_PRIORITY = ("deepseek-v4-flash", "deepseek", "kimi", "glm", "qwen")


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def caihui() -> tuple[str, str]:
    for kv in [(MCP_JSON,)]:
        for name in CAIHUI_NAMES:
            s = (_read_json(kv[0]).get("mcpServers") or {}).get(name) or {}
            url = s.get("url") or ""
            key = (s.get("headers") or {}).get("x-api-key") or ""
            if url and key:
                return url, key
    raise SystemExit("读不到财汇 MCP 配置：检查 ~/.workbuddy/mcp.json 的 caihui_mcp 条目")


def model() -> dict:
    data = _read_json(MODELS_JSON)
    if not isinstance(data, list) or not data:
        raise SystemExit("读不到模型配置：检查 ~/.workbuddy/models.json")
    for want in MODEL_PRIORITY:
        for m in data:
            if want in str(m.get("id", "")).lower() and m.get("url") and m.get("apiKey"):
                return m
    for m in data:
        if m.get("url") and m.get("apiKey"):
            return m
    raise SystemExit("models.json 里没有带 url/apiKey 的模型")


def build(keep_pw: bool) -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "app").mkdir(parents=True)
    (OUT / "01_模板").mkdir(parents=True)

    for f in (BASE / "app").glob("*.py"):
        shutil.copy2(f, OUT / "app" / f.name)
    static = OUT / "app" / "static"
    static.mkdir()
    shutil.copy2(BASE / "app" / "static" / "index.html", static / "index.html")
    shutil.copy2(BASE / "01_模板" / "内评简评报告通用模板_v1.1.json",
                 OUT / "01_模板" / "内评简评报告通用模板_v1.1.json")
    shutil.copy2(BASE / "requirements.txt", OUT / "requirements.txt")

    # 口令：默认沿用上次的，除非要求重新生成
    pw_file = BASE / "_cloud_pw.txt"
    if keep_pw and pw_file.exists():
        pw = pw_file.read_text(encoding="utf-8").strip()
    else:
        pw = secrets.token_urlsafe(9)
        pw_file.write_text(pw, encoding="utf-8")

    murl, mkey = caihui()
    m = model()
    env = {
        "PORT": "8000",           # 部署工具会注入真实端口，这里是本地自测用
        "HOST": "0.0.0.0",        # 云端必须监听全地址
        "REPORT_PASSWORD": pw,
        "CAIHUI_MCP_URL": murl,
        "CAIHUI_MCP_KEY": mkey,
        "LLM_BASE_URL": m["url"],
        "LLM_API_KEY": m["apiKey"],
        "LLM_MODEL": m["id"],
        "LLM_MAX_INPUT": str(m.get("maxInputTokens") or 131072),
        "LLM_MAX_OUTPUT": str(m.get("maxOutputTokens") or 8192),
    }
    quoted = {k: ("" if v == "" else str(v).replace("'", "'\\''")) for k, v in env.items()}
    lines = ["#!/usr/bin/env bash", "# 由 make_cloud.py 生成，含密钥，勿提交",
             "set -e"]
    for k, v in quoted.items():
        if k == "PORT":
            lines.append(f'export PORT="${{PORT:-8000}}"')
        else:
            lines.append(f"export {k}='{v}'")
    lines += ['cd "$(dirname "$0")"',
              '# 沙箱里 python3 就是装好依赖的那个；本机自测时用 PYTHON 指定虚拟环境',
              'exec "${PYTHON:-python3}" app/server.py']
    (OUT / "start.sh").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "start.sh").chmod(0o755)
    (OUT / "README.md").write_text(
        "# 云端副本\n\n"
        "由 `make_cloud.py` 生成，只含运行时需要的文件。\n\n"
        "- `start.sh` 里带财汇与模型的密钥，**不要提交到任何仓库**。\n"
        "- 数据（上传件、导出的 docx、历史、用量）写在容器内的 `data/` 与 `output/`，"
        "重启即丢；需要长期留存请用本机部署。\n"
        f"- 访问口令见 `_cloud_pw.txt`（项目根目录）。\n",
        encoding="utf-8")

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"已生成 {OUT}（{size / 1024:.0f} KB）")
    print(f"口令: {pw}")
    print(f"模型: {m['id']}  →  {m['url']}")
    print(f"财汇: {murl}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-pw", action="store_true")
    build(ap.parse_args().keep_pw)


if __name__ == "__main__":
    main()
