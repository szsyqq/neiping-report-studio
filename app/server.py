"""内评简评报告生成工作台 —— 本地 HTTP 服务（仅用标准库 + python-docx + pymupdf）。

启动：
    /Users/panyp/.workbuddy/binaries/python/envs/rating-flow/bin/python app/server.py
默认监听 127.0.0.1:8770，可用 PORT 环境变量覆盖。

取数、下载材料、生成草稿耗时较长，全部改为后台任务（app/jobs.py），
前端用 job_id 轮询百分比与阶段文案；导出仍走同步接口（秒级完成）。
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
import jobs  # noqa: E402
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


# ---------------------------------------------------------------- 业务动作

def do_search(company: str, progress=None) -> dict:
    client = _client()
    facts = facts_mod.build_facts(client, company, progress=progress)
    if progress:
        progress(97, "整理台账", "汇总工商、股权、评级、债券与财务")
    with LOCK:
        STATE[company] = {"facts": facts, "materials": []}
    return {"company": facts.get("company", {}).get("name") or company, "facts": facts}


def do_fetch(company: str, picks: list[dict], budget: int = 45000, progress=None) -> dict:
    n = max(len(picks), 1)
    items = [{"title": m.get("title") or "", "state": "wait", "pct": 0, "detail": "等待"} for m in picks]
    loaded = []
    for i, m in enumerate(picks):
        title = m.get("title") or "material"
        items[i].update(state="run", detail="准备下载")

        def cb(frac, label, i=i):
            items[i].update(state="run", pct=int(frac * 100), detail=label)
            if progress:
                progress((i + frac) / n * 100, "拉取材料",
                         f"第 {i + 1}/{n} 份 · {label}", items=items)

        try:
            item = pdf_text.load_material(m["url"], title, budget=budget, progress=cb)
            item["date"] = m.get("date", "")
            loaded.append(item)
            items[i].update(state="done", pct=100, pages=item["pages"], chars=item["chars"],
                            detail=f"{item['pages']} 页 · 摘录 {item['chars']} 字")
        except Exception as e:  # 单份失败不影响其余
            items[i].update(state="error", detail=str(e)[:120])
        if progress:
            progress((i + 1) / n * 100, "拉取材料",
                     f"已完成 {i + 1}/{n} 份", items=items)

    with LOCK:
        st = STATE.setdefault(company, {"facts": {}, "materials": []})
        titles = {m.get("title") for m in loaded}
        st["materials"] = [x for x in st["materials"] if x.get("title") not in titles] \
            + [x for x in loaded if x.get("text")]
    return {"materials": [{k: v for k, v in m.items() if k != "text"} for m in loaded],
            "loaded": [m.get("title") for m in loaded if m.get("text")],
            "items": items}


def do_generate(company: str, model: str | None, progress=None) -> dict:
    with LOCK:
        st = STATE.get(company) or {}
        facts = st.get("facts") or {}
        materials = st.get("materials") or []
    if not facts:
        raise ValueError("请先检索企业")
    if not materials:
        raise ValueError("请先拉取材料（至少一份评级报告）")
    draft = generator.generate(facts, materials, model_id=model, progress=progress)
    with LOCK:
        STATE.setdefault(company, {})["draft"] = draft
    return {"draft": draft}


# ---------------------------------------------------------------- HTTP 动作

def api_search(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    if not company:
        raise ValueError("请填写企业名称")
    return do_search(company)


def api_fetch(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    picks = payload.get("materials") or []
    if not picks:
        with LOCK:
            facts = (STATE.get(company) or {}).get("facts")
        if not facts:
            raise ValueError("请先检索企业")
        picks = facts_mod.pick_materials(facts)
    return do_fetch(company, picks, int(payload.get("budget") or 45000))


def api_generate(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    return {"draft": do_generate(company, payload.get("model"))["draft"]}


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


def api_job(kind: str, payload: dict) -> dict:
    """把长动作交给后台线程，立刻返回 job_id。"""
    if kind == "search":
        company = (payload.get("company") or "").strip()
        if not company:
            raise ValueError("请填写企业名称")
        job = jobs.submit("search", lambda p: do_search(company, p))
    elif kind == "fetch":
        company = (payload.get("company") or "").strip()
        picks = payload.get("materials") or []
        if not company:
            raise ValueError("请先检索企业")
        if not picks:
            with LOCK:
                facts = (STATE.get(company) or {}).get("facts")
            if not facts:
                raise ValueError("请先检索企业")
            picks = facts_mod.pick_materials(facts)
        budget = int(payload.get("budget") or 45000)
        job = jobs.submit("fetch", lambda p: do_fetch(company, picks, budget, p))
    elif kind == "generate":
        company = (payload.get("company") or "").strip()
        model = payload.get("model")
        job = jobs.submit("generate", lambda p: do_generate(company, model, p))
    else:
        raise ValueError("未知任务类型")
    return {"job": job["id"]}


class Handler(BaseHTTPRequestHandler):
    server_version = "NeipingReport/1.1"

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
            elif p.startswith("/api/jobs/"):
                job = jobs.get(p[len("/api/jobs/"):])
                if not job:
                    self._json({"error": "任务不存在或已过期"}, 404)
                else:
                    self._json(job)
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
        p = unquote(urlparse(self.path).path)
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
        try:
            if p.startswith("/api/jobs/"):
                self._json(api_job(p[len("/api/jobs/"):], payload))
                return
            fn = routes.get(p)
            if not fn:
                self._json({"error": "not found"}, 404)
                return
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
