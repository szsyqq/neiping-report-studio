"""启动前把 start.sh 里的密钥补进环境变量。

为什么需要这一层：云端副本的财汇、模型、SMTP 密钥只写在 `_cloud/start.sh`
里（它不入库），正常路径是平台执行 `bash start.sh`，export 完再起服务。
但只要启动方式换成「直接 python3 app/server.py」——部署工具传了自定义启动命令
就会这样——start.sh 根本没被执行，进程环境里一个密钥都没有：
页面上表现为「提示未找到财汇 MCP」，而真正的原因是整份配置都没加载。

所以服务自己读一遍 start.sh：环境变量里已经有的不覆盖（平台注入的 PORT
必须优先），缺什么补什么。这样无论从哪条路启动，配置都在。

只认 `export KEY=VALUE` 这种一行一条的写法——那是 make_cloud.py 生成的格式。
值里带 `$` 的（`export PORT="${PORT:-8000}"`）跳过：它是给 shell 求值的，
原样塞进环境变量只会得到字面量。
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

# 只关心这几个：其余环境变量（PATH 之类）不该由文件覆盖
KEYS = (
    "PORT", "HOST", "REPORT_PASSWORD",
    "CAIHUI_MCP_URL", "CAIHUI_MCP_KEY",
    "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
    "LLM_MAX_INPUT", "LLM_MAX_OUTPUT",
    "SMTP_HOST", "SMTP_PORT", "SMTP_TLS", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
)


def _candidates() -> list[Path]:
    """start.sh 可能在 app/ 的上一级（云端副本根目录），也可能在 app/ 里。"""
    here = Path(__file__).resolve().parent
    return [here.parent / "start.sh", here / "start.sh"]


def _parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("export "):
            continue
        try:
            parts = shlex.split(line[len("export "):])
        except ValueError:
            continue
        for p in parts:
            k, sep, v = p.partition("=")
            if not sep or k not in KEYS or not v:
                continue
            # 交给 shell 求值的写法（含 $ 或反引号）原样读进来是错的
            if "$" in v or "`" in v:
                continue
            out[k] = v
    return out


def load() -> list[str]:
    """把缺失的密钥补进 os.environ，返回补了哪些键（便于启动时打一行日志）。"""
    want: dict[str, str] = {}
    for p in _candidates():
        if p.is_file():
            want = _parse(p)
            if want:
                break
    if not want:
        return []
    filled: list[str] = []
    for k, v in want.items():
        if (os.environ.get(k) or "").strip():
            continue            # 已有值优先：平台注入的 PORT、临时覆盖的口令
        os.environ[k] = v
        filled.append(k)
    if filled:
        os.environ["_BOOTSTRAP_FILLED"] = ",".join(filled)
    return filled


def describe() -> str:
    """启动日志用：只说「start.sh 读到了没有」，不评判其它来源。

    本机本来就不该靠 start.sh——财汇读 ~/.workbuddy/mcp.json、模型读 models.json。
    在这里报「缺少 CAIHUI_MCP_URL」会把人往错的方向引，所以端点是否可用
    由 mcp_client 自己判断（见 server.main 里的那行打印）。
    """
    src = next((p for p in _candidates() if p.is_file()), None)
    if not src:
        return "启动配置：没有 start.sh，按环境变量与本地配置文件读取"
    if os.environ.get("_BOOTSTRAP_FILLED"):
        return (f"启动配置：已从 start.sh 补齐 "
                + os.environ["_BOOTSTRAP_FILLED"].replace(",", "、"))
    return f"启动配置：start.sh 存在（{src}），其中的值已在环境变量里，未改动"
