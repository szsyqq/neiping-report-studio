"""把项目整理成一份可云端部署的瘦身副本（`_cloud/`）。

为什么不是直接部署项目根目录：根目录里 `data/`（98MB 历史 PDF）与 `output/`
（19MB 历次成品）都是本地作业痕迹，云端实例一件都用不上，反而让上传与冷启动变慢。
云端要的只有：程序、模板、依赖声明、启动脚本。

密钥从本机现有配置读出来写进 `_cloud/start.sh`（该目录在 .gitignore 里，不入库）：
    财汇 MCP 的 url 与 x-api-key   ← ~/.workbuddy/mcp.json
    DeepSeek 的 url 与 apiKey      ← ~/.workbuddy/models.json
访问口令另生成一个随机串，不用默认的 ceshi——云端是对外的。

用法：
    python3 make_cloud.py                 # 重新生成 _cloud/，口令沿用上次的
    python3 make_cloud.py --new-pw        # 重新生成一个随机口令（推荐，云端对外）
    python3 make_cloud.py --password ce-shi   # 指定口令
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
MAIL_JSON = BASE / "data" / "mail.json"     # gitignore，含 SMTP 授权码
MAIL_JSON = BASE / "data" / "mail.json"     # gitignore，含 SMTP 授权码

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


def smtp() -> dict:
    """把本机填好的发件配置带给云端。

    云端容器一重启，data/ 就空了，页面上填的 SMTP 会丢——所以用环境变量注入，
    这些值只写进 _cloud/start.sh（该目录在 .gitignore 里），不进仓库。
    本机没配就返回空，云端仍然可以在页面上填。
    """
    d = _read_json(MAIL_JSON)
    if not (d.get("host") and d.get("user") and d.get("password")):
        return {}
    return {
        "SMTP_HOST": d["host"],
        "SMTP_PORT": str(d.get("port") or 465),
        "SMTP_TLS": d.get("tls") or "ssl",
        "SMTP_USER": d["user"],
        "SMTP_PASS": d["password"],
        "SMTP_FROM": d.get("from") or d["user"],
    }


def smtp() -> dict:
    """把本机填好的发件配置带给云端。

    云端容器一重启，data/ 就空了，页面上填的 SMTP 会丢——所以用环境变量注入，
    这些值只写进 _cloud/start.sh（该目录在 .gitignore 里），不进仓库。
    本机没配就返回空，云端仍然可以在页面上填。
    """
    d = _read_json(MAIL_JSON)
    if not (d.get("host") and d.get("user") and d.get("password")):
        return {}
    return {
        "SMTP_HOST": d["host"],
        "SMTP_PORT": str(d.get("port") or 465),
        "SMTP_TLS": d.get("tls") or "ssl",
        "SMTP_USER": d["user"],
        "SMTP_PASS": d["password"],
        "SMTP_FROM": d.get("from") or d["user"],
    }


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


def build(password: str | None = None, new_pw: bool = False) -> None:
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

    # 版本历史：/versions 时间线页与 /v/<id>/ 快照都从 site/ 读，
    # 云端没有这份就点开是 404。docs 里是模板与方法论页面，同理。
    # 只拷这两个子目录：site/index.html 是 Pages 的迁移落地页，云端用不到
    # （云端的 `/` 由 app/static/index.html 提供），拷过去反而多一个会自指的页面。
    shutil.copy2(BASE / "versions.json", OUT / "versions.json")
    for sub in ("docs", "versions"):
        src = BASE / "site" / sub
        if src.is_dir():
            shutil.copytree(src, OUT / "site" / sub, dirs_exist_ok=True)

    # 口令优先级：命令行 --password > --new-pw 随机 > 上次生成的值 > 出厂默认
    #
    # 默认「沿用上次」而不是每次随机：口令是要发给使用者的，重发一次云端
    # 就得把新口令重新告知一遍，忘了就登不进去。要换再显式 --new-pw / --password。
    pw_file = BASE / "_cloud_pw.txt"
    if password:
        pw = password
    elif new_pw:
        pw = secrets.token_urlsafe(9)
    elif pw_file.exists():
        pw = pw_file.read_text(encoding="utf-8").strip()
    else:
        pw = "ce-shi"
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
        # 输出上限别回落到 8192：一份报告的 JSON 实测要 7800 上下 token，
        # 8192 只剩几百余量，材料稍多就截断，表现为「JSON 无法解析」。
        "LLM_MAX_OUTPUT": str(m.get("maxOutputTokens") or 32000),
    }
    env.update(smtp())
    env.update(smtp())
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
        "- 启动方式两种都行：`bash start.sh`，或直接 `python3 app/server.py`——"
        "后者会由 `app/bootstrap_env.py` 回头读 start.sh，把缺失的密钥补进环境变量。"
        "部署工具若传了自定义启动命令，就走这条兜底，不会因为没执行 start.sh 而丢配置。\n"
        "- 数据（上传件、导出的 docx、历史、用量）写在容器内的 `data/` 与 `output/`，"
        "重启即丢；需要长期留存请用本机部署。\n"
        f"- 访问口令见 `_cloud_pw.txt`（项目根目录）。\n",
        encoding="utf-8")

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"已生成 {OUT}（{size / 1024:.0f} KB）")
    print(f"口令: {pw}")
    print(f"模型: {m['id']}  →  {m['url']}")
    print(f"财汇: {murl}")
    st = smtp()
    print(f"发件邮箱: {st.get('SMTP_USER', '(未配置，云端可在页面上填)')}"
          + (f"  →  {st.get('SMTP_HOST')}:{st.get('SMTP_PORT')} {st.get('SMTP_TLS')}"
             if st else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-pw", action="store_true", help="（旧参数，现为默认行为）沿用上次口令")
    ap.add_argument("--new-pw", action="store_true", help="重新生成随机口令")
    ap.add_argument("--password", help="指定口令，例如 --password ce-shi")
    a = ap.parse_args()
    build(password=(a.password or "").strip() or None, new_pw=a.new_pw)


if __name__ == "__main__":
    main()
