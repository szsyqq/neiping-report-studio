"""内评简评报告生成工作台 —— 本地 HTTP 服务（仅用标准库 + python-docx + pymupdf）。

启动：
    /Users/panyp/.workbuddy/binaries/python/envs/rating-flow/bin/python app/server.py
默认监听 127.0.0.1:8770，可用 PORT 环境变量覆盖。

取数、下载材料、生成草稿耗时较长，全部改为后台任务（app/jobs.py），
前端用 job_id 轮询百分比与阶段文案；导出仍走同步接口（秒级完成）。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sys
import threading
import traceback
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth as auth_mod  # noqa: E402
import analysis  # noqa: E402
import docx_writer  # noqa: E402
import facts as facts_mod  # noqa: E402
import generator  # noqa: E402
import history  # noqa: E402
import jobs  # noqa: E402
import llm  # noqa: E402
import mailer  # noqa: E402
import settings_store  # noqa: E402
import pdf_text  # noqa: E402
import updater  # noqa: E402
import usage as usage_mod  # noqa: E402
from mcp_client import CaihuiClient, MCPError  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
OUTPUT = BASE / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = BASE / "data" / "uploads"
VERSIONS = BASE / "site" / "versions"
DATA_DIR = BASE / "data"

# 单份材料的摘录上限与本次生成的总上限（字符）。评级报告正文约 2.5 万字符，
# 募集说明书只取与主体相关的前半部；两份合计控制在 6 万字符内，
# 相当于把一次生成送入模型的材料量压到 4 万 token 上下。
BUDGET_TOTAL = 62000
BUDGET_BY_KIND = {"跟踪评级报告": 32000, "募集说明书": 26000}

STATE: dict[str, dict] = {}
LOCK = threading.Lock()


def _client() -> CaihuiClient:
    return CaihuiClient()


def _key(company: str) -> str:
    """本地只上传、不检索时也要能生成，用一个固定键挂材料。"""
    return company.strip() or "__local__"


# ---------------------------------------------------------------- 业务动作

def do_search(company: str, progress=None) -> dict:
    client = _client()
    stats: dict = {}
    facts = facts_mod.collect_materials(client, company, progress=progress, stats=stats)
    with LOCK:
        st = STATE.setdefault(_key(company), {"facts": {}, "materials": []})
        st["facts"] = facts
        # 重新检索＝换一轮材料重来。上一轮读过的材料必须作废，否则同一进程里
        # 第二次生成会把上一轮（甚至上一家）的 PDF 一起送进提示词、附件和邮件。
        st["materials"] = []
    n = sum(len(v) for v in (facts.get("materials") or {}).values())
    calls = int(stats.get("calls") or 0)
    usage_mod.record("search", company=company, chars=n, mcp_calls=calls,
                     note=f"公告 {n} 条 · 财汇 {calls} 次")
    return {"company": company, "facts": facts, "mcp_calls": calls,
            "queries": stats.get("queries") or []}


def _budget_for(title: str, remaining: int) -> int:
    want = next((v for k, v in BUDGET_BY_KIND.items() if k in (title or "")), 30000)
    return max(6000, min(want, remaining))


def do_fetch(company: str, picks: list[dict], budget: int = 0, progress=None,
             replace: bool = False) -> dict:
    """读材料。

    replace=True 表示「本次勾选就是这一轮的全部材料」——界面上材料清单一次列全，
    勾选结果就是完整集合，所以先清空再写入，避免上一轮的 PDF 留在 STATE 里
    继续参与生成与附件。上传 PDF 是增量动作，保持 replace=False 追加。
    """
    n = max(len(picks), 1)
    items = [{"title": m.get("title") or "", "state": "wait", "pct": 0, "detail": "等待"} for m in picks]
    loaded: list[dict] = []
    bases: list[dict] = []
    left = budget or BUDGET_TOTAL
    for i, m in enumerate(picks):
        title = m.get("title") or "material"
        items[i].update(state="run", detail="准备读取")

        def cb(frac, label, i=i):
            items[i].update(state="run", pct=int(frac * 100), detail=label)
            if progress:
                progress((i + frac) / n * 100, "读取材料",
                         f"第 {i + 1}/{n} 份 · {label}", items=items)

        try:
            one = _budget_for(title, left)
            path = (m.get("path") or "")
            is_docx = path.lower().endswith(".docx")
            # 谁能当底稿不能只看扩展名：外部评级报告也常是 docx。
            # 前端在更新模式下会明说这一份是「底稿」还是「外部材料」；
            # 没说的话按扩展名兜底（docx 视作底稿），保持新建模式的老行为。
            role = (m.get("role") or "").strip().lower()
            if not role:
                role = "base" if is_docx else "material"
            if role == "base":
                if not is_docx:
                    raise ValueError("旧报告需要 Word（.docx）文件")
                up = updater.load_docx(path)
                bases.append({"title": title, "path": path, "chars": up["chars"],
                              "paras": len(up["rows"]), "tables": up["tables"]})
                items[i].update(state="done", pct=100, pages=0, chars=up["chars"],
                                detail=f"底稿 · {len(up['rows'])} 段 · {up['chars']} 字")
                if progress:
                    progress((i + 1) / n * 100, "读取材料", "底稿已就绪", items=items)
                continue
            if is_docx:
                # docx 作为外部材料：提取文本即可，页数没有意义
                up = updater.load_docx(path)
                item = {"title": title, "text": up["text"], "pages": 0,
                        "chars": up["chars"], "path": path}
                items[i].update(state="run", pct=60, detail="提取 docx 文本")
            elif path:  # 本地上传的 PDF
                item = pdf_text.load_file(path, title, budget=one, progress=cb)
            else:
                item = pdf_text.load_material(m["url"], title, budget=one, progress=cb)
            left = max(6000, left - item["chars"])
            item["date"] = m.get("date", "")
            item["source"] = m.get("source", "upload" if m.get("path") else "caihui")
            loaded.append(item)
            shown = (f"{item['pages']} 页 · " if item["pages"] else "")
            items[i].update(state="done", pct=100, pages=item["pages"], chars=item["chars"],
                            detail=f"{shown}摘录 {item['chars']} 字")
        except Exception as e:  # 单份失败不影响其余
            items[i].update(state="error", detail=str(e)[:120])
        if progress:
            progress((i + 1) / n * 100, "读取材料",
                     f"已完成 {i + 1}/{n} 份", items=items)

    with LOCK:
        st = STATE.setdefault(_key(company), {"facts": {}, "materials": []})
        titles = {m.get("title") for m in loaded}
        prev = [] if replace else [x for x in st["materials"] if x.get("title") not in titles]
        st["materials"] = prev + [x for x in loaded if x.get("text")]
        all_titles = [x.get("title") for x in st["materials"] if x.get("title")]
        # 底稿每轮只认最新那份：换主体或重新上传就该覆盖，留着旧底稿会让
        # 修订落到上一份文件上，产物与页面看到的不是一回事。
        if bases:
            st["base"] = bases[-1]
            if replace:
                all_titles = all_titles + [bases[-1]["title"]]
    usage_mod.record("fetch", company=company or "本地材料",
                     chars=sum(m["chars"] for m in loaded),
                     note=f"{len(loaded)}/{len(picks)} 份 · "
                          f"{sum(m['pages'] for m in loaded)} 页")
    return {"materials": [{k: v for k, v in m.items() if k != "text"} for m in loaded],
            "loaded": [m.get("title") for m in loaded if m.get("text")],
            # 这一轮之后该主体名下的材料全集：前端据此覆盖本地清单，
            # 而不是把每次的结果并进一个只增不减的数组
            "all": all_titles,
            "base": bases[-1] if bases else None,
            "base_titles": [b["title"] for b in bases],
            "items": items}


def _guess_company(base_title: str, materials: list[dict]) -> str:
    """从底稿文件名或材料标题里认出主体全称。

    底稿按交付约定命名为「公司全称 + 内评简评报告 + 日期」，直接去掉后两段即可；
    认不出再看外部材料标题（「2026年度××公司信用评级报告」取中间那段）。
    都不成立就返回空串，由调用方兜底，绝不凭空编一个名字。
    """
    t = re.sub(r"\.(docx|pdf)$", "", base_title or "", flags=re.I)
    t = re.sub(r"内评简评报告.*$", "", t).strip(" _-")
    if len(t) >= 4:
        return t
    for m in materials:
        title = re.sub(r"\.(docx|pdf)$", "", m.get("title") or "", flags=re.I)
        hit = re.search(r"\d{4}\s*年度?\s*(.{4,40}?)(?:信用评级报告|跟踪评级报告|评级报告)", title)
        if hit:
            return hit.group(1).strip()
        hit = re.search(r"^(.{4,40}?)(?:信用评级报告|跟踪评级报告|募集说明书)", title)
        if hit:
            return hit.group(1).strip()
    return ""


def do_generate(company: str, model: str | None, progress=None,
                titles: list[str] | None = None, mode: str = "new") -> dict:
    with LOCK:
        st = STATE.get(_key(company)) or {}
        facts = st.get("facts") or {}
        materials = st.get("materials") or []
        base = st.get("base") or None
    # 只写这一轮真正选中的材料：STATE 里可能还留着同一主体更早读过的几份，
    # 不按标题过滤的话，第二次生成的正文会混进没勾选的旧材料。
    if titles:
        want = {t for t in titles if t}
        picked = [m for m in materials if m.get("title") in want]
        if picked:
            materials = picked

    if mode == "update":
        # 更新模式：底稿是修订对象，外部材料是依据，两者缺一不可。
        if not base:
            raise ValueError("更新既有报告需要先上传待修订的旧报告（.docx）")
        if not materials:
            raise ValueError("请先拉取或上传外部评级报告（PDF），作为修订依据")
        old = updater.load_docx(base["path"])
        result = updater.analyze(old, materials, model_id=model,
                                 progress=progress, facts=facts)
        # 历史记录、导出文件名、邮件标题都按这个名字找主体。更新模式下用户
        # 未必先去检索，企业名可能是空的——那就从底稿文件名或材料标题里认：
        # 按交付约定，底稿名形如「××公司内评简评报告 25.8.14.docx」。
        stem = re.sub(r"\.(docx|pdf)$", "", (base.get("title") or ""), flags=re.I).strip() \
            or "未命名主体"
        # 兜底链：用户填的 → 从文件名认出主体 → 至少用底稿文件名，别叫「未命名主体」：
        # 后者落进文件名后，隔一天自己都认不出这是谁的稿子。
        result["company_full_name"] = company or _guess_company(base.get("title") or "", materials) or stem
        result["base_title"] = base.get("title") or ""
        result["base_path"] = base["path"]
        rid = history.save(result, materials,
                           model=(result.get("usage") or {}).get("model", ""),
                           seconds=(result.get("usage") or {}).get("seconds", 0))
        result["history_id"] = rid
        with LOCK:
            STATE.setdefault(_key(company), {})["draft"] = result
        return {"draft": result}

    if not materials:
        raise ValueError("请先拉取材料，或上传评级报告/募集说明书 PDF")
    draft = generator.generate(facts or facts_mod.empty_facts(company), materials,
                               model_id=model, progress=progress)
    # 落一条历史：draft 全文 + 用了哪几份材料，之后可回看、接着改、重新导出
    rid = history.save(draft, materials, model=(draft.get("usage") or {}).get("model", ""),
                       seconds=(draft.get("usage") or {}).get("seconds", 0))
    draft["history_id"] = rid
    with LOCK:
        STATE.setdefault(_key(company), {})["draft"] = draft
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
            facts = (STATE.get(_key(company)) or {}).get("facts")
        if not facts:
            raise ValueError("请先检索企业，或上传 PDF")
        picks = facts_mod.pick_materials(facts)
    return do_fetch(company, picks, int(payload.get("budget") or 0),
                    replace=bool(payload.get("replace")))


def save_upload(name: str, data: bytes) -> dict:
    """把上传的文件落到 data/uploads/，等下一步统一解析。

    PDF 是外部材料；docx 是待修订的旧报告底稿。两者都在这里落盘，
    由 do_fetch 按扩展名分流，前端不需要为两种文件准备两套上传逻辑。
    """
    if len(data) < 1200:
        raise ValueError("文件太小，不像是完整的报告")
    is_pdf = data[:5].startswith(b"%PDF")
    is_docx = data[:2] == b"PK" and b"word/" in data[:4096]
    if not (is_pdf or is_docx):
        raise ValueError("只接受 PDF 或 Word（.docx）文件")
    title = facts_mod.title_key(name) or "上传材料"
    safe = re.sub(r"[^\w\u4e00-\u9fa5.\-]+", "_", title)[:80] or "upload"
    digest = hashlib.sha256(data).hexdigest()[:12]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".pdf" if is_pdf else ".docx"
    path = UPLOAD_DIR / f"{safe}_{digest}{suffix}"
    path.write_bytes(data)
    return {"path": str(path), "title": title, "size": len(data), "name": path.name,
            "kind": "pdf" if is_pdf else "docx"}


def api_generate(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    return {"draft": do_generate(company, payload.get("model"),
                                 titles=[t for t in (payload.get("titles") or []) if t],
                                 mode=(payload.get("mode") or "new"))["draft"]}


def api_export(payload: dict) -> dict:
    draft = payload.get("draft") or {}
    # 更新模式的产物是「在旧报告上打了修订标记的文件」，不是重新排一份。
    # 所以它走自己的分支：底稿必须是记录里那份 docx，正文不再参与排版。
    if (draft.get("mode") or payload.get("mode")) == "update":
        return _export_revision(draft, payload)
    # 导出是最终形态：页面上改过的段落也要过一遍语言清理，别把「材料未披露」带进 Word
    draft = generator.polish_draft(draft)
    out = write_docx(draft, payload.get("company") or "")
    name = (draft.get("company_full_name") or payload.get("company") or "").strip()
    usage_mod.record("export", company=name or out.stem, note=out.name)
    rid = payload.get("history_id") or draft.get("history_id") or ""
    if rid:
        history.attach_docx(rid, out.name)
    return {"path": str(out), "name": out.name, "url": "/download/" + quote(out.name),
            "cleaned": draft.get("_cleaned") or 0}


def _export_revision(draft: dict, payload: dict) -> dict:
    """把页面上的修订清单落到旧报告 docx 上，输出带修订标记的文件。

    底稿一律取记录里存的那份（draft.base_path），不接受页面另传路径——
    修订必须发生在「这一轮分析过的那份文件」上，否则改动会落到别处。
    文件命名照评级更新流程的约定：公司全称 + 内评简评报告 + YY.M.D。
    """
    actions = draft.get("actions") or []
    base_path = draft.get("base_path") or ""
    if not actions:
        raise ValueError("没有可执行的改动")
    if not base_path or not Path(base_path).is_file():
        raise ValueError("找不到这次的旧报告底稿，无法生成修订版；请重新上传后分析")

    name = (draft.get("company_full_name") or payload.get("company") or "未命名主体").strip()
    name = re.sub(r"[\\/:*?\"<>|]", "", name)
    stamp = datetime.now().strftime("%y.%-m.%-d")
    out = OUTPUT / f"{name}内评简评报告 {stamp}.docx"
    n = 2
    while out.exists():
        out = OUTPUT / f"{name}内评简评报告 {stamp}-{n}.docx"
        n += 1

    res = updater.apply(actions, base_path, out, author="内评简评")
    usage_mod.record("export", company=name, note=out.name)
    rid = payload.get("history_id") or draft.get("history_id") or ""
    if rid:
        history.attach_docx(rid, out.name)
    return {"path": str(out), "name": out.name, "url": "/download/" + quote(out.name),
            "mode": "update", "counts": res["counts"],
            "failed_detail": res["failed_detail"], "review_detail": res["review_detail"],
            "cleaned": 0}


def api_settings(payload: dict) -> dict:
    """设置中心：一次读写四类配置。密码类字段只进不出。"""
    sec = (payload.get("section") or "").strip()
    if sec == "llm":
        settings_store.save_llm(payload)
        llm.check_all()          # 改完立刻探活，下拉马上反映
    elif sec == "caihui":
        settings_store.save_caihui(payload)
    elif sec == "mail":
        mailer.save_config(payload)
    elif sec == "password":
        old, new = payload.get("old") or "", payload.get("new") or ""
        if not auth_mod.check(old):
            raise ValueError("原口令不对")
        try:
            auth_mod.set_password(new)
        except ValueError as e:
            raise ValueError(str(e)) from None
        return {"ok": True, **auth_mod.issue(), "settings": settings_store.overview()}
    return {"ok": True, "settings": settings_store.overview()}


def api_mail_config(payload: dict) -> dict:
    """读或改发件配置。密码只进不出。"""
    if payload.get("save"):
        mailer.save_config(payload)
    return mailer.describe()


def api_mail(payload: dict) -> dict:
    """把报告寄出去。标题默认企业全称，收件人默认 panyuping641@pingan.com.cn。"""
    company = (payload.get("company") or "").strip()
    draft = payload.get("draft") or {}
    name = (draft.get("company_full_name") or company or "").strip()

    docx_name = _base_name(payload.get("docx") or "")
    docx_path = OUTPUT / docx_name if docx_name else None
    if not (docx_path and docx_path.is_file()):
        docx_path = None
        if draft:
            docx_path = write_docx(generator.polish_draft(draft), name or company)
            usage_mod.record("export", company=company or docx_path.stem, note=docx_path.name)
            rid = payload.get("history_id") or draft.get("history_id") or ""
            if rid:
                history.attach_docx(rid, docx_path.name)

    files: list[Path] = []
    if docx_path:
        files.append(docx_path)
    if payload.get("with_materials"):
        for m in (payload.get("materials") or []):
            base = _base_name(m)
            for d in (DATA_DIR, UPLOAD_DIR):
                p = d / base
                if p.is_file():
                    files.append(p)
                    break
    # 分析附件（核对结果、自算算式、来源对照……）：勾了才附。
    # 附件是文件，txt 得先落盘；统一放 output/analysis/，同名覆盖即可——
    # 这些东西每次都从草稿重算，不存在「要保留上一版」的诉求。
    ana_keys = [k for k in (payload.get("analysis") or []) if isinstance(k, str)]
    if ana_keys and (draft or payload.get("company")):
        src, mats = _draft_for_analysis(payload)
        if src:
            ana_dir = OUTPUT / "analysis"
            ana_dir.mkdir(parents=True, exist_ok=True)
            for f in analysis.build(src, materials=mats):
                if f["key"] not in ana_keys:
                    continue
                fp = ana_dir / _base_name(f["name"])
                fp.write_text(f["text"], encoding="utf-8")
                files.append(fp)
    if not files:
        raise ValueError("没有可发送的附件：先导出报告，或勾选原底稿")

    subject = (payload.get("subject") or "").strip() or name or (docx_path.stem if docx_path else "内评简评报告")
    body = payload.get("body") or ""
    if not body.strip():
        body = (f"{name or '受评主体'}的内评简评报告见附件，请查收。\n\n"
                + "、".join(p.name for p in files) + "\n")

    res = mailer.send(payload.get("to") or mailer.DEFAULT_TO, subject, body, files,
                      cc=payload.get("cc") or "")
    usage_mod.record("mail", company=name or company, note=f"{'、'.join(res['to'])}｜{res['subject'][:30]}")
    rid = payload.get("history_id") or draft.get("history_id") or ""
    if rid:
        history.attach_mail(rid, res["to"], res["subject"])
    return res


def write_docx(draft: dict, company: str = "") -> Path:
    """渲染 Word，按「企业名内评简评报告 日期」命名并避免覆盖。"""
    name = (draft.get("company_full_name") or company or "未命名主体").strip()
    name = re.sub(r"[\\/:*?\"<>|]", "", name)
    stamp = datetime.now().strftime("%y.%-m.%-d")
    out = OUTPUT / f"{name}内评简评报告 {stamp}.docx"
    n = 2
    while out.exists():
        out = OUTPUT / f"{name}内评简评报告 {stamp}-{n}.docx"
        n += 1
    docx_writer.render_report(draft, out)
    return out


# ---------------------------------------------------------------- 打包下载

def _base_name(name: str) -> str:
    """只取文件名，并去掉仍是路径/通配的字符，防止拼出目录外的路径。"""
    base = Path(str(name or "").replace("\\", "/")).name
    return re.sub(r"[\\/:*?\"<>|\r\n]+", "_", base).strip()


def _docx_index(company: str) -> list[dict]:
    """output/ 下属于这家企业的报告，最新的在最前。"""
    prefix = re.sub(r"[\\/:*?\"<>|]", "", company or "")
    if not prefix:
        return []
    rows = [p for p in OUTPUT.glob("*.docx") if p.name.startswith(prefix + "内评简评报告")]
    rows.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": p.stat().st_size,
             "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")}
            for p in rows[:4]]


def _find_pdf(name_or_title: str) -> Path | None:
    """按「材料标题」找回落盘的 PDF：缓存文件是「标题净化后_哈希.pdf」。"""
    safe = re.sub(r"[^\w\u4e00-\u9fa5.\-]+", "_", name_or_title or "")[:80]
    if not safe:
        return None
    for d in (DATA_DIR, UPLOAD_DIR):
        p = d / safe
        if p.suffix.lower() != ".pdf":
            p = d / (safe + ".pdf")
        if p.is_file():
            return p
        hits = sorted(d.glob(safe + "*.pdf"))
        if hits:
            return hits[0]
    return None


def _pdf_index(company: str, titles: list[str] | None = None) -> list[dict]:
    """可打包的原底稿。

    前端把本次读取过的材料标题传进来（服务重启后 STATE 是空的，靠标题
    在缓存目录里重新对上文件）；标题缺失时再按企业名兜底扫 data/。
    """
    rows: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, title: str, pages: int, source: str):
        if not path.is_file() or path.name in seen:
            return
        seen.add(path.name)
        rows.append({"name": path.name,
                     "title": title or re.sub(r"_[0-9a-f]{12}$", "", path.stem),
                     "size": path.stat().st_size, "pages": pages,
                     "source": source or ("upload" if path.parent == UPLOAD_DIR else "caihui")})

    with LOCK:
        mats = ((STATE.get(_key(company)) or {}).get("materials") or []) if company else []
    by_title = {m.get("title"): m for m in mats if m.get("title")}

    if titles:
        for t in titles:
            m = by_title.get(t) or {}
            path = Path(m["path"]) if m.get("path") and Path(m["path"]).is_file() else _find_pdf(t)
            if path and path.is_file():
                add(path, t, m.get("pages") or 0, m.get("source") or "")
        if rows:
            return rows

    for m in mats:
        add(Path(m.get("path") or ""), m.get("title") or "", m.get("pages") or 0,
            m.get("source") or "")

    # 仍然没有，按企业名在缓存目录里找回同名主体的材料
    if company and len(company) >= 5:
        for d in (DATA_DIR, UPLOAD_DIR):
            for p in sorted(d.glob("*.pdf")):
                if company in p.name:
                    add(p, "", 0, "upload" if d == UPLOAD_DIR else "caihui")
    return rows


def api_bundle_items(company: str, titles: list[str] | None = None) -> dict:
    company = (company or "").strip()
    with LOCK:
        st = STATE.get(_key(company)) or {}
        has_draft = bool(st.get("draft"))
        mats = st.get("materials") or []
    # 分析附件是「这一轮分析留下的过程稿」，只有本次有草稿时才给清单：
    # 换了主体或刚重启时给空清单，比把上一家的核对结果端上来安全。
    ana = analysis.meta_list(st["draft"], mats) if has_draft else []
    return {"company": company, "docx": _docx_index(company),
            "materials": _pdf_index(company, titles), "has_draft": has_draft,
            "analysis": ana}


def _draft_for_analysis(payload: dict) -> tuple[dict, list[dict]]:
    """本次草稿与材料。前端带了草稿就用前端的（页面上改过的才算数）。"""
    company = (payload.get("company") or "").strip()
    with LOCK:
        st = STATE.get(_key(company)) or {}
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else st.get("draft")
        mats = st.get("materials") or []
    return (draft if isinstance(draft, dict) else {}), mats


def api_analysis_list(payload: dict) -> dict:
    """分析附件清单（不含全文）：侧栏与两个勾选面板都用它。"""
    draft, mats = _draft_for_analysis(payload)
    if not draft:
        return {"files": []}
    return {"files": analysis.meta_list(draft, mats)}


def api_analysis_get(payload: dict) -> dict:
    """取一份分析附件的全文，用于侧栏预览。"""
    draft, mats = _draft_for_analysis(payload)
    if not draft:
        raise ValueError("还没有生成结果：先完成一次生成，再看分析附件")
    key = (payload.get("key") or "").strip()
    f = analysis.one(draft, key, materials=mats)
    if not f:
        raise ValueError(f"没有这一份分析附件：{key}")
    return {"key": f["key"], "name": f["name"], "desc": f["desc"], "text": f["text"]}


def api_bundle(payload: dict) -> dict:
    """把勾选的报告、原底稿与分析附件打成一个 zip。"""
    company = (payload.get("company") or "").strip()
    draft = payload.get("draft") or {}
    want_json = bool(payload.get("with_json"))
    docx_name = _base_name(payload.get("docx") or "")
    picked = [m for m in (_base_name(x) for x in (payload.get("materials") or [])) if m]
    ana_keys = [k for k in (payload.get("analysis") or []) if isinstance(k, str)]

    docx_path = OUTPUT / docx_name if docx_name else None
    if not (docx_path and docx_path.is_file()):
        docx_path = None
        if draft:
            docx_path = write_docx(generator.polish_draft(draft), company)
            usage_mod.record("export", company=company or docx_path.stem, note=docx_path.name)

    pdfs = []
    for name in picked:
        for d in (DATA_DIR, UPLOAD_DIR):
            p = d / name
            if p.is_file():
                pdfs.append(p)
                break

    # 分析附件只有文字，直接从草稿现场生成；没带草稿就退回服务端存的那份
    ana: list[dict] = []
    if ana_keys:
        src = draft or _draft_for_analysis(payload)[0]
        mats = _draft_for_analysis(payload)[1]
        if src:
            ana = [f for f in analysis.build(src, materials=mats) if f["key"] in ana_keys]

    if not docx_path and not pdfs and not ana and not (want_json and draft):
        raise ValueError("没有可打包的文件")

    label = re.sub(r"[\\/:*?\"<>|]", "", company or "内评简评报告")
    stamp = datetime.now().strftime("%y.%-m.%-d %H%M")
    out = OUTPUT / f"{label}内评简评报告打包 {stamp}.zip"
    n = 2
    while out.exists():
        out = OUTPUT / f"{label}内评简评报告打包 {stamp}-{n}.zip"
        n += 1

    files = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        if docx_path:
            z.write(docx_path, _base_name(docx_path.name))
            files.append({"name": docx_path.name, "dir": "", "size": docx_path.stat().st_size})
        for p in pdfs:
            z.write(p, "原底稿/" + _base_name(p.name))
            files.append({"name": p.name, "dir": "原底稿", "size": p.stat().st_size})
        for f in ana:
            blob = f["text"].encode("utf-8")
            z.writestr(f"{f['dir']}/{f['name']}", blob)
            files.append({"name": f["name"], "dir": f["dir"], "size": len(blob)})
        if want_json and draft:
            blob = json.dumps(draft, ensure_ascii=False, indent=2).encode()
            z.writestr("草稿数据.json", blob)
            files.append({"name": "草稿数据.json", "dir": "", "size": len(blob)})

    _prune_zips(label + "内评简评报告")
    return {"name": out.name, "url": "/download/" + quote(out.name),
            "size": out.stat().st_size, "files": files}


def _prune_zips(prefix: str, keep: int = 3):
    """同一主体反复打包会堆出很多大文件，只留最近几份（含刚生成的）。"""
    rows = sorted(OUTPUT.glob(prefix + "*打包*.zip"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for p in rows[keep:]:
        try:
            p.unlink()
        except OSError:
            pass


def api_history_list(payload: dict | None = None) -> dict:
    return {"items": history.list_records(int((payload or {}).get("limit") or 60))}


# 旧记录的校验结果只到段落级（那时还没有 issue_items）。恢复时按记录里存的
# 材料路径重新读一遍 PDF、重跑 validate，就能补上「哪一段 / 哪一句」的定位。
# 读 PDF 一份约 0.6 秒，所以按「路径 + 修改时间」缓存，同一份材料只解析一次。
_MAT_CACHE: dict[str, dict] = {}
_MAT_LOCK = threading.Lock()


def _reload_materials(mats: list[dict]) -> list[dict] | None:
    out: list[dict] = []
    for m in mats or []:
        p = (m or {}).get("path") or ""
        fp = Path(p)
        if not p or not fp.exists():
            return None
        try:
            key = f"{p}|{fp.stat().st_mtime_ns}"
        except OSError:
            return None
        with _MAT_LOCK:
            hit = _MAT_CACHE.get(key)
        if hit is None:
            try:
                hit = pdf_text.load_file(str(fp), (m.get("title") or ""), budget=45000)
            except Exception:                                  # noqa: BLE001 — 读不了就退回旧数据
                return None
            with _MAT_LOCK:
                _MAT_CACHE[key] = hit
        out.append(hit)
    return out or None


def _upgrade_record(rec: dict) -> dict:
    """把旧记录的校验重算到当前规则版本。

    判据是「记录的校验版本」而不是「有没有 issue_items」：后者只在新增结构化
    字段那一次有效，之后再加校验项，老记录永远不会重算，用户打开旧报告就
    看不到新提示——这正是上一轮的教训。
    """
    draft = rec.get("draft") or {}
    val = draft.get("validation") or {}
    # 更新模式的校验是「每条改动能否在旧报告里定位」，与新建模式的数字溯源
    # 不是一套规则，不能拿 generator.validate 去重算，否则会得出无意义的结论。
    if (draft.get("mode") or "new") == "update":
        return rec
    if val.get("rev") == generator.VALIDATOR_REV:   # 已按当前规则算过
        return rec
    if rec.get("_reval") == "skip":                 # 试过、材料已不在，别每次都试
        return rec
    mats = _reload_materials(rec.get("materials") or [])
    if not mats:
        rec["_reval"] = "skip"
        return rec
    # 存量记录的期次列多半是时间正序（旧代码没有重排），重算时一并纠正，
    # 这样打开旧报告看到的表序与新生成的报告一致。
    c = dict(draft.get("commentary") or {})
    for k in ("financial_table", "segment_table"):
        if isinstance(c.get(k), dict):
            c[k] = generator.reorder_table(c[k])
    if c:
        draft["commentary"] = c
        rec["draft"] = draft
    try:
        new_val = generator.validate(draft, {}, mats)
    except Exception:                                       # noqa: BLE001
        rec["_reval"] = "skip"
        return rec
    new_val["upgraded"] = True
    draft["validation"] = new_val
    rec["draft"] = draft
    try:
        history.update_validation(rec.get("id") or "", new_val)
    except Exception:                                       # noqa: BLE001 — 落盘失败不影响本次使用
        pass
    return rec


def api_history_get(rid: str) -> dict:
    rec = history.get(rid)
    if not rec:
        raise ValueError("记录不存在")
    return _upgrade_record(rec)


def api_history_delete(payload: dict) -> dict:
    ok = history.remove(payload.get("id") or "")
    return {"ok": ok}


def api_prompt(payload: dict) -> dict:
    company = (payload.get("company") or "").strip()
    with LOCK:
        st = STATE.get(_key(company)) or {}
        facts = st.get("facts") or {}
        materials = st.get("materials") or []
    if not materials:
        raise ValueError("请先拉取材料，或上传 PDF")
    titles = {t for t in (payload.get("titles") or []) if t}
    if titles:
        materials = [m for m in materials if m.get("title") in titles] or materials
    prompt = generator.build_user_prompt(facts or facts_mod.empty_facts(company), materials)
    return {"prompt": prompt, "system": generator.SYSTEM_PROMPT,
            "traits": generator.describe_traits(facts or {})}


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
        if not picks:
            with LOCK:
                facts = (STATE.get(_key(company)) or {}).get("facts")
            if not facts:
                raise ValueError("请先检索企业，或上传 PDF")
            picks = facts_mod.pick_materials(facts)
        budget = int(payload.get("budget") or 0)
        replace = bool(payload.get("replace"))
        job = jobs.submit("fetch", lambda p: do_fetch(company, picks, budget, p, replace))
    elif kind == "generate":
        company = (payload.get("company") or "").strip()
        model = payload.get("model")
        titles = [t for t in (payload.get("titles") or []) if t]
        mode = payload.get("mode") or "new"
        job = jobs.submit("generate",
                          lambda p: do_generate(company, model, p, titles, mode))
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        # Chrome 的 Private Network Access：公网页面（GitHub Pages）请求本机地址前
        # 会先发预检并带 Access-Control-Request-Private-Network，不认这个头就直接拦，
        # 页面上表现为「连不上本机服务」——服务其实活着。头加了才放行。
        self.send_header("Access-Control-Allow-Private-Network", "true")
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

    def _json(self, data: dict, code: int = 200, extra: dict | None = None):
        self._send(code, json.dumps(data, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", extra)

    # ── 口令闸门 ────────────────────────────────────────────────────
    def _token(self) -> str:
        """三种取法，按场景选：
        - Cookie nr_auth：同源（云端）最稳，浏览器自动带，代理也不动它
        - Authorization: Bearer：本机跨域（Pages → 127.0.0.1），Cookie 会因跨站被拦
        - ?t=：下载是浏览器直接跳转，带不了头
        """
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == "nr_auth" and v:
                return v.strip()
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        q = parse_qs(urlparse(self.path).query)
        return (q.get("t") or [""])[0].strip()

    def _authed(self) -> bool:
        try:
            return auth_mod.valid(self._token())
        except OSError:
            return False

    def _need_login(self):
        self._json({"error": "未登录或登录已过期，请重新输入口令", "need_login": True}, 401)

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
            elif p == "/versions":
                # 补斜杠，页面里的 ./<id>/ 才是相对于 /versions/ 解析
                self._send(301, b"", "text/plain",
                           {"Location": "/versions/" + (("?" + urlparse(self.path).query)
                                                       if urlparse(self.path).query else "")})
            elif p == "/versions/":
                # 版本时间线：页面骨架公开，数据接口仍在闸门后
                self._file(VERSIONS / "index.html")
            elif p.startswith("/versions/") or p.startswith("/v/"):
                # 历史版本快照：/v/<id>/ 与 /versions/<id>/ 等价
                seg = p[len("/versions/"):] if p.startswith("/versions/") else p[len("/v/"):]
                vid = seg.strip("/").split("/")[0]
                if not re.fullmatch(r"[0-9A-Za-z_-]{1,32}", vid or ""):
                    self._json({"error": "版本号不合法"}, 400)
                else:
                    fp = VERSIONS / vid / "index.html"
                    if fp.is_file():
                        self._file(fp)
                    else:
                        self._json({"error": f"没有这个版本：{vid}", "versions": "/versions"}, 404)
            elif p == "/api/versions":
                self._json(json.loads((BASE / "versions.json").read_text(encoding="utf-8")))
            elif p == "/api/session":
                self._json({"authed": self._authed(),
                            "default_password": auth_mod.is_default()})
            elif p == "/api/health":
                self._json({"ok": True, "time": datetime.now().isoformat()})
            elif not self._authed():
                # 口令闸门之后的一切：下载、历史、统计、任务状态……
                self._need_login()
            elif p.startswith("/download/"):
                name = _base_name(p[len("/download/"):])
                fp = OUTPUT / name
                if not fp.is_file():
                    self._json({"error": "文件不存在"}, 404)
                    return
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                fallback = "bundle.zip" if name.lower().endswith(".zip") else "report.docx"
                self._send(200, fp.read_bytes(), ctype,
                           {"Content-Disposition": f'attachment; filename="{fallback}"; '
                                                    "filename*=UTF-8''" + quote(name)})
            elif p.startswith("/api/jobs/"):
                job = jobs.get(p[len("/api/jobs/"):])
                if not job:
                    self._json({"error": "任务不存在或已过期"}, 404)
                else:
                    self._json(job)
            elif p == "/api/models":
                q = parse_qs(urlparse(self.path).query)
                if (q.get("refresh") or [""])[0] in ("1", "true"):
                    llm.check_all()
                models, skipped = llm.usable_models()
                cache = llm.load_health()
                self._json({"models": models, "default": llm.pick_default(models),
                            "skipped": skipped, "checked_at": cache.get("ts") or "",
                            "reasons": {k: v.get("why") for k, v in
                                        (cache.get("results") or {}).items() if not v.get("ok")}})
            elif p == "/api/settings":
                self._json(settings_store.overview())
            elif p == "/api/mail/config":
                self._json(mailer.describe())
            elif p == "/api/stats":
                self._json(usage_mod.summary())
            elif p == "/api/history":
                self._json(api_history_list())
            elif p.startswith("/api/history/"):
                self._json(api_history_get(p[len("/api/history/"):]))
            elif p == "/api/bundle":
                q = parse_qs(urlparse(self.path).query)
                self._json(api_bundle_items((q.get("company") or [""])[0],
                                            q.get("title") or []))
            elif p == "/api/analysis/list":
                q = parse_qs(urlparse(self.path).query)
                self._json(api_analysis_list({"company": (q.get("company") or [""])[0]}))
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError:
            self._json({"error": "文件不存在"}, 404)
        except Exception as e:
            self._json({"error": str(e), "trace": traceback.format_exc()[-800:]}, 500)

    def do_POST(self):
        parsed = urlparse(self.path)
        p = unquote(parsed.path)
        length = int(self.headers.get("Content-Length") or 0)

        # 上传走裸字节（前端用 XHR 传，能显示上传百分比），不走 multipart
        if p == "/api/upload":
            if not self._authed():
                self._need_login()
                return
            try:
                name = (parse_qs(parsed.query).get("name") or ["上传材料.pdf"])[0]
                data = self.rfile.read(length) if length else b""
                self._json(save_upload(name, data))
            except (ValueError, OSError) as e:
                self._json({"error": str(e)}, 400)
            return

        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        # 登录/登出不需要已有 token
        if p == "/api/login":
            if not auth_mod.check(payload.get("password") or ""):
                self._json({"error": "口令不对"}, 401)
                return
            sess = auth_mod.issue()
            self._json({"ok": True, **sess, "default_password": auth_mod.is_default()},
                       extra={"Set-Cookie": auth_mod.cookie(sess["token"])})
            return
        if p == "/api/logout":
            auth_mod.revoke(self._token())
            self._json({"ok": True}, extra={"Set-Cookie": auth_mod.cookie("", clear=True)})
            return
        if p == "/api/password":
            if not self._authed():
                self._need_login()
                return
            old, new = payload.get("old") or "", payload.get("new") or ""
            if not auth_mod.check(old):
                self._json({"error": "原口令不对"}, 400)
                return
            try:
                auth_mod.set_password(new)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            sess = auth_mod.issue()   # 改完直接换发新 token，免得自己也被踢下线
            self._json({"ok": True, **sess})
            return

        if not self._authed():
            self._need_login()
            return

        routes = {
            "/api/search": api_search,
            "/api/fetch": api_fetch,
            "/api/generate": api_generate,
            "/api/export": api_export,
            "/api/prompt": api_prompt,
            "/api/bundle": api_bundle,
            "/api/analysis/list": api_analysis_list,
            "/api/analysis/get": api_analysis_get,
            "/api/history/list": api_history_list,
            "/api/history/delete": api_history_delete,
            "/api/mail": api_mail,
            "/api/mail/config": api_mail_config,
            "/api/settings": api_settings,
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
    # 模型体检放到后台跑：8 个端点逐个探活要十几秒，别挡住首页
    threading.Thread(target=lambda: llm.check_all(), daemon=True, name="model-probe").start()
    print(f"内评简评报告工作台：http://{host}:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
