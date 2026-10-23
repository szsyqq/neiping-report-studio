"""财汇 MCP 客户端（streamable-http，JSON-RPC over POST）。

配置从 WorkBuddy 的 ~/.workbuddy/mcp.json 读取，无需另行填写 key；
也支持用环境变量 CAIHUI_MCP_URL / CAIHUI_MCP_KEY 覆盖。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MCP_CONFIG = Path.home() / ".workbuddy" / "mcp.json"
SERVER_NAMES = ("caihui_mcp", "caihui-mcp", "caihui")


class MCPError(RuntimeError):
    pass


def _load_endpoint() -> tuple[str, str]:
    url = os.environ.get("CAIHUI_MCP_URL")
    key = os.environ.get("CAIHUI_MCP_KEY")
    if url and key:
        return url, key
    # 页面「设置」里填的（data/caihui.json），本机与云端都适用
    f = Path(__file__).resolve().parent.parent / "data" / "caihui.json"
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            d = {}
        if d.get("url") and d.get("key"):
            return d["url"], d["key"]
    if MCP_CONFIG.exists():
        cfg = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        servers = cfg.get("mcpServers") or {}
        for name in SERVER_NAMES:
            s = servers.get(name)
            if not s:
                continue
            u = s.get("url") or ""
            k = (s.get("headers") or {}).get("x-api-key") or ""
            if u and k:
                return u, k
    raise MCPError("未找到财汇 MCP 配置：请检查 ~/.workbuddy/mcp.json 中的 caihui_mcp 条目")


class CaihuiClient:
    def __init__(self, url: str | None = None, key: str | None = None, timeout: int = 180):
        if url and key:
            self.url, self.key = url, key
        else:
            self.url, self.key = _load_endpoint()
        self.timeout = timeout
        self._seq = 0
        # 计费口径：每调用一次二级工具算一次点数（导航/元数据工具不计），
        # 让上层能如实记下「取一次清单花了几点」。
        self.calls = 0
        self.call_log: list[dict] = []

    def _rpc(self, method: str, params: dict) -> dict:
        self._seq += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params},
                          ensure_ascii=False).encode()
        req = urllib.request.Request(self.url, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-api-key": self.key,
        })
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read().decode("utf-8", "ignore")
                break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        else:
            raise MCPError(f"MCP 连接失败：{last}")
        if not raw.strip():
            raise MCPError("MCP 返回空响应")
        # 兼容 SSE 帧（event: message\ndata: {...}）
        if raw.lstrip().startswith("event:") or "\ndata:" in raw:
            chunks = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            raw = chunks[-1] if chunks else raw
        payload = json.loads(raw)
        if "error" in payload:
            raise MCPError(f"MCP 错误：{payload['error']}")
        return payload

    def call_tool(self, name: str, args: dict[str, Any] | None = None) -> dict:
        payload = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        result = payload.get("result") or {}
        if result.get("isError"):
            raise MCPError(f"工具 {name} 返回错误：{result}")
        if isinstance(result, dict) and "content" in result:
            text = "".join(c.get("text", "") for c in result["content"] if c.get("type") == "text")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
        return result

    # ---- 业务封装 -------------------------------------------------

    def execute(self, subtool: str, args: dict[str, Any]) -> dict:
        """调用二级子工具，返回 {status, data} 结构。"""
        self.calls += 1
        self.call_log.append({"tool": subtool, "at": time.strftime("%H:%M:%S"),
                              "args": {k: v for k, v in args.items() if k in
                                       ("target_company", "title_keywords", "limit")}})
        out = self.call_tool("execute_tool", {"tool_name": subtool, "arguments": args})
        if "status" in out:
            code = (out["status"] or {}).get("code")
            if isinstance(code, int) and code >= 100:
                raise MCPError(f"{subtool} 查询失败：{(out['status'] or {}).get('message')}")
        return out

    @staticmethod
    def rows(out: dict) -> list[dict]:
        """把 records.headInfo + records.data 的行数组转成 dict 列表。

        键取 headInfo 的中文列名（去掉首尾空白），因为财汇的 field 是英文缩写，
        中文列名才是各工具之间稳定的口径。
        """
        try:
            rec = out["data"]["records"]
            heads = [(h.get("name") or h.get("field") or "").strip() for h in rec["headInfo"]]
            return [dict(zip(heads, row)) for row in rec["data"]]
        except (KeyError, TypeError):
            return []

    @staticmethod
    def summary(out: dict) -> str:
        st = out.get("status") or {}
        qs = st.get("querySummary")
        return "；".join(qs) if isinstance(qs, list) else (qs or "")

    def subtools(self, nav: str, sub_fields: list[str] | None = None) -> list[dict]:
        args = {"sub_fields": sub_fields} if sub_fields else {}
        out = self.call_tool(nav, args)
        try:
            return out["data"]["records"]["subTools"]
        except (KeyError, TypeError):
            return []
