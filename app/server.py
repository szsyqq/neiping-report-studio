"""内评简评报告生成工作台 —— 本地 HTTP 服务（仅用标准库 + python-docx + pymupdf）。

启动：
    /Users/panyp/.workbuddy/binaries/python/envs/rating-flow/bin/python app/server.py
默认监听 127.0.0.1:8770，可用 PORT 环境变量覆盖。
"""

from __future__ import annotations

import json
import mimetypes
import re
import sys
import threading
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import docx_writer  # noqa: E402
import facts as facts_mod  # noqa: E402
import generator  # noqa: E402
import llm  # noqa: E402
import pdf_text  # noqa: E402
from mcp_client import CaihuiClient, MCPError  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
OUTPUT = BASE / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)

STATE: dict[str, dict] = {}
LOCK = threading.Lock()


def _client() -> CaihuiClient:
    return CaihuiClient()


def api_search(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    if not company:
        raise ValueError("请填写企业名称")
    client = _client()
    facts = facts_mod.build_facts(client, company)
    with LOCK:
        STATE[company] = {"facts": facts, "materials": []}
    return {"company": facts.get("company", {}).get("name") or company, "facts": facts}


def api_fetch(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    picks = payload.get("materials") or []
    if not picks:
        with LOCK:
            facts = (STATE.get(company) or {}).get("facts")
        if not facts:
            raise ValueError("请先检索企业")
        picks = facts_mod.pick_materials(facts)
    loaded = []
    for m in picks:
        try:
            item = pdf_text.load_material(m["url"], m.get("title") or "material",
                                          budget=int(payload.get("budget") or 45000))
            item["date"] = m.get("date", "")
            loaded.append(item)
        except Exception as e:  # 单份失败不影响其余
            loaded.append({"title": m.get("title"), "error": str(e), "text": "", "chars": 0})
    with LOCK:
        st = STATE.setdefault(company, {"facts": {}, "materials": []})
        have = {x["title"] for x in st["materials"] if x.get("text")}
        st["materials"] = [x for x in st["materials"] if x.get("title") not in
                           {m.get("title") for m in loaded}] + [x for x in loaded if x.get("text")]
        _ = have
    return {"materials": [{k: v for k, v in m.items() if k != "text"} for m in loaded],
            "loaded": [m.get("title") for m in loaded if m.get("text")]}


def api_generate(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    with LOCK:
        st = STATE.get(company) or {}
        facts = st.get("facts") or {}
        materials = st.get("materials") or []
    if not facts:
        raise ValueError("请先检索企业")
    if not materials:
        raise ValueError("请先拉取材料（至少一份评级报告）")
    draft = generator.generate(facts, materials, model_id=payload.get("model"))
    with LOCK:
        STATE.setdefault(company, {})["draft"] = draft
    return {"draft": draft}


def api_export(payload: dict) -> dict:
    draft = payload.get("draft") or {}
    company = (draft.get("company_full_name") or payload.get("company") or "未命名主体").strip()
    company = re.sub(r"[\\/:*?\"<>|]", "", company)
    stamp = datetime.now().strftime("%y.%-m.%-d")
    out = OUTPUT / f"{company}内评简评报告 {stamp}.docx"
    n = 2
    while out.exists():
        out = OUTPUT / f"{company}内评简评报告 {stamp}-{n}.docx"
        n += 1
    docx_writer.render_report(draft, out)
    return {"path": str(out), "name": out.name, "url": "/download/" + quote(out.name)}


def api_prompt(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    with LOCK:
        st = STATE.get(company) or {}
        facts = st.get("facts") or {}
        materials = st.get("materials") or []
    if not facts:
        raise ValueError("请先检索企业")
    prompt = generator.build_user_prompt(facts, materials)
    return {"prompt": prompt, "system": generator.SYSTEM_PROMPT,
            "traits": generator.describe_traits(facts)}


class Handler(BaseHTTPRequestHandler):
    server_version = "NeipingReport/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 允许 GitHub Pages 上的入口页跨域连本机服务；服务只监听 127.0.0.1，
        # 外部网络到不了，放开 Origin 不会扩大暴露面。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            # 分块写并 flush：一次性 write 大文件时，浏览器提前断开会让
            # 整个请求抛异常且响应被截断
            view = memoryview(body)
            for i in range(0, len(view), 65536):
                self.wfile.write(view[i:i + 65536])
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: dict, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _file(self, path: Path):
        if not path.exists() or not path.is_file():
            self._json({"error": "文件不存在"}, 404)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        p = unquote(urlparse(self.path).path)
        try:
            if p in ("/", "/index.html"):
                self._file(STATIC / "index.html")
            elif p.startswith("/static/"):
                self._file(STATIC / p[len("/static/"):])
            elif p.startswith("/docs/"):
                # 模板与方法论的浏览页，与 GitHub Pages 上的同一份文件
                self._file(BASE / "site" / "docs" / p[len("/docs/"):])
            elif p.startswith("/download/"):
                name = p[len("/download/"):]
                self._send(200, (OUTPUT / name).read_bytes(),
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           {"Content-Disposition": 'attachment; filename="report.docx"; '
                                                    "filename*=UTF-8''" + quote(name)})
            elif p == "/api/models":
                models = llm.list_models()
                self._json({"models": models, "default": llm.pick_default(models)})
            elif p == "/api/health":
                self._json({"ok": True, "time": datetime.now().isoformat()})
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError:
            self._json({"error": "文件不存在"}, 404)
        except Exception as e:
            self._json({"error": str(e), "trace": traceback.format_exc()[-800:]}, 500)

    def do_POST(self):
        p = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return
        routes = {
            "/api/search": api_search,
            "/api/fetch": api_fetch,
            "/api/generate": api_generate,
            "/api/export": api_export,
            "/api/prompt": api_prompt,
        }
        fn = routes.get(p)
        if not fn:
            self._json({"error": "not found"}, 404)
            return
        try:
            self._json(fn(payload))
        except (ValueError, MCPError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{e}", "trace": traceback.format_exc()[-1200:]}, 500)


def main():
    import os
    port = int(os.environ.get("PORT") or 8770)
    host = os.environ.get("HOST") or "127.0.0.1"
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"内评简评报告工作台：http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
