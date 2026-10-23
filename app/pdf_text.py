"""材料下载与文本抽取。

用 pymupdf 逐页取文本，按关键词优先保留实质内容页，产出可控长度的摘录，
供大模型阅读（评级报告全文通常可直接进上下文，募集说明书与年报必须裁剪）。
"""

from __future__ import annotations

import hashlib
import re
import urllib.request
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# 明显无分析价值的页（封面、声明、目录、签字页、释义）
SKIP_HINTS = ("评级声明", "跟踪评级安排", "分析师", "评级报告使用说明", "法律声明",
              "目录", "释义", "签字", "盖章", "附录")

# 实质内容页的加分关键词
KEEP_HINTS = ("基本情况", "股权", "实际控制人", "业务", "经营", "行业", "营业收入",
              "吞吐量", "产能", "在建工程", "资产构成", "负债", "债务", "偿债",
              "现金流", "受限资产", "应收账款", "对外担保", "诉讼", "评级观点",
              "正面", "关注", "财务", "主要指标", "募集资金", "主营业务")


def download(url: str, filename: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    safe = re.sub(r"[^\w\u4e00-\u9fa5.\-]+", "_", filename)[:80]
    path = DATA_DIR / f"{safe}_{digest}.pdf"
    if path.exists() and path.stat().st_size > 5000:
        return path
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    path.write_bytes(data)
    return path


def extract_pages(pdf_path: str | Path) -> list[str]:
    try:
        import pymupdf  # noqa: F401
        doc = pymupdf.open(pdf_path)
    except ImportError:
        import fitz as pymupdf  # type: ignore
        doc = pymupdf.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        try:
            txt = page.get_text("text")
        except Exception:
            txt = ""
        pages.append(txt or "")
    doc.close()
    return pages


def _clean(t: str) -> str:
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def score_page(text: str) -> float:
    if len(text.strip()) < 80:
        return -100
    s = 0.0
    for h in SKIP_HINTS:
        if h in text[:200]:
            s -= 6
    for h in KEEP_HINTS:
        if h in text:
            s += 1.5
    # 含数字密集（表格/指标）的页加权
    if len(re.findall(r"\d+\.\d{2}", text)) > 15:
        s += 2
    return s


def excerpt(pages: list[str], budget: int = 45000, title: str = "") -> str:
    """按页打分，保留得分高的页，输出不超过 budget 字符的摘录。"""
    scored = [(i, score_page(p), p) for i, p in enumerate(pages)]
    total = sum(len(p) for _, _, p in scored)
    if total <= budget:
        body = "\n\n".join(f"[第{i + 1}页]\n{_clean(p)}" for i, _, p in scored if p.strip())
        return f"《{title}》全文（{len(pages)}页）\n{body}"

    keep: list[tuple[int, str]] = []
    used = 0
    for i, s, p in sorted(scored, key=lambda x: (-x[1], x[0])):
        if s < 0:
            break
        text = _clean(p)
        if used + len(text) > budget:
            continue
        keep.append((i, text))
        used += len(text)
        if used > budget * 0.95:
            break
    keep.sort(key=lambda x: x[0])
    body = "\n\n".join(f"[第{i + 1}页]\n{t}" for i, t in keep)
    return f"《{title}》摘录（原{len(pages)}页，保留{len(keep)}页）\n{body}"


def load_material(url: str, title: str, budget: int = 45000) -> dict:
    """下载并抽取一份材料，返回 {title, path, chars, text}。"""
    pdf = download(url, title)
    pages = extract_pages(pdf)
    text = excerpt(pages, budget=budget, title=title)
    return {"title": title, "path": str(pdf), "pages": len(pages), "chars": len(text), "text": text}
