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

# 财务三表与主要指标页：数字的唯一出处，一旦被预算裁掉，模型就会把
# 「材料里明明有」的期次写成「未披露」。所以这些页先占预算，再谈打分。
FIN_STMT_HINTS = ("合并资产负债表", "合并利润表", "合并现金流量表", "合并所有者权益变动表",
                  "资产负债表", "利润表", "现金流量表", "所有者权益变动表")
FIN_SUM_HINTS = ("主要财务数据", "主要财务指标", "主要会计数据", "财务指标")
# 报表独有的小计行：普通业务页不会出现，用它认跨页后半张最稳
FIN_BALANCE_ANCHORS = ("资产总计", "负债总计", "所有者权益合计", "所有者权益总计")
_NUM_CELL = re.compile(r"\d{1,3}(?:,\d{3})+\.\d{2}")


def fin_level(text: str, prev_level: int = 0) -> int:
    """这一页的财务价值：3=三大报表（含跨页后半张），2=主要财务数据/指标，0=其他。

    判据刻意收窄：一开始用「数字密集 + 营业收入」这种宽条件，结果把普通业务页
    也当成财务报表，反而把真正的三表挤出了预算。现在只认表名与报表独有的小计行
    （资产总计／负债合计／所有者权益合计），续页靠「上一页是报表 + 数字密集」跟随。
    """
    body = text or ""
    if any(a in body for a in FIN_STMT_HINTS):
        return 3
    if any(b in body for b in FIN_BALANCE_ANCHORS):
        return 3
    if prev_level == 3 and len(_NUM_CELL.findall(body)) >= 8:
        return 3
    if any(h in body[:800] for h in FIN_SUM_HINTS):
        return 2
    return 0




def download(url: str, filename: str, on_progress=None) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    safe = re.sub(r"[^\w\u4e00-\u9fa5.\-]+", "_", filename)[:80]
    path = DATA_DIR / f"{safe}_{digest}.pdf"
    if path.exists() and path.stat().st_size > 5000:
        if on_progress:
            on_progress(1.0, "本地已缓存")
        return path
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        total = int(r.headers.get("Content-Length") or 0)
        chunk, got = b"", 0
        parts = []
        while True:
            block = r.read(65536)
            if not block:
                break
            parts.append(block)
            got += len(block)
            if on_progress:
                on_progress(min(got / total, 1.0) if total else 0.0,
                            f"{got / 1048576:.1f}".rstrip("0").rstrip(".") + "MB")
        data = b"".join(parts)
    path.write_bytes(data)
    return path


def extract_pages(pdf_path: str | Path, on_progress=None) -> list[str]:
    try:
        import pymupdf  # noqa: F401
        doc = pymupdf.open(pdf_path)
    except ImportError:
        import fitz as pymupdf  # type: ignore
        doc = pymupdf.open(pdf_path)
    pages = []
    n = doc.page_count or 1
    for i, page in enumerate(doc):
        try:
            txt = page.get_text("text")
        except Exception:
            txt = ""
        pages.append(txt or "")
        if on_progress and (i % 10 == 0 or i == n - 1):
            on_progress(min((i + 1) / n, 1.0), f"{i + 1}/{n} 页")
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
    """按页打分，保留得分高的页，输出不超过 budget 字符的摘录。

    顺序是「先保财务页，再按打分填」：财务三表页在打分上不占优（页首多是表名，
    关键词命中少，还可能被当成年报附录），但它们是资产总额、负债总额这类数字的
    唯一出处。先让它们占掉不超过三成预算，剩下的再按内容价值分配。
    """
    scored = [(i, score_page(p), p) for i, p in enumerate(pages)]
    total = sum(len(p) for _, _, p in scored)
    if total <= budget:
        body = "\n\n".join(f"[第{i + 1}页]\n{_clean(p)}" for i, _, p in scored if p.strip())
        return f"《{title}》全文（{len(pages)}页）\n{body}"

    keep: list[tuple[int, str]] = []
    taken: set[int] = set()
    used = 0

    # 先给每一页定财务等级（含跨页后半张），后面三轮挑选都用这份标记
    levels: list[int] = []
    prev = 0
    for p in pages:
        prev = fin_level(p, prev)
        levels.append(prev)

    def take(indices, cap: int):
        """按页序收，直到这一类的字符额度用完。"""
        got = 0
        for i in indices:
            if i in taken or got >= cap:
                continue
            text = _clean(pages[i])
            if not text or got + len(text) > cap:
                continue
            keep.append((i, text))
            taken.add(i)
            got += len(text)
        return got

    # ① 三大报表页：资产负债、利润、现金流是刚需，先占三成八
    used += take([i for i in range(len(pages)) if levels[i] == 3], int(budget * 0.38))
    # ② 主要财务数据/指标页：评级报告里通常也有同一张表，给它一成
    used += take([i for i in range(len(pages)) if levels[i] == 2], int(budget * 0.1))

    for i, s, p in sorted(scored, key=lambda x: (-x[1], x[0])):
        if i in taken or s < 0:
            continue
        text = _clean(p)
        if used + len(text) > budget:
            continue
        keep.append((i, text))
        taken.add(i)
        used += len(text)
        if used > budget * 0.95:
            break

    # 还剩预算就回头补财务页（报表跨页被截断时常见）
    if used < budget * 0.85:
        for lv, cap in ((3, int(budget * 0.5)), (2, int(budget * 0.16))):
            got = sum(len(t) for i, t in keep if levels[i] == lv)
            for i in range(len(pages)):
                if i in taken or levels[i] != lv or got >= cap:
                    continue
                text = _clean(pages[i])
                if not text or used + len(text) > budget:
                    continue
                keep.append((i, text))
                taken.add(i)
                used += len(text)
                got += len(text)

    keep.sort(key=lambda x: x[0])
    body = "\n\n".join(f"[第{i + 1}页]\n{t}" for i, t in keep)
    return f"《{title}》摘录（原{len(pages)}页，保留{len(keep)}页）\n{body}"


def load_file(pdf_path: str | Path, title: str, budget: int = 32000, progress=None) -> dict:
    """抽取一份已在本地（下载或上传）的 PDF。"""
    def rep(frac, label):
        if progress:
            progress(max(0.0, min(1.0, frac)), label)

    rep(0.05, "解析 PDF")
    pages = extract_pages(pdf_path, on_progress=lambda f, lab: rep(0.05 + 0.85 * f, "解析 " + lab))
    rep(0.94, "生成摘录")
    text = excerpt(pages, budget=budget, title=title)
    rep(1.0, "完成")
    return {"title": title, "path": str(pdf_path), "pages": len(pages),
            "chars": len(text), "text": text}


def load_material(url: str, title: str, budget: int = 32000, progress=None) -> dict:
    """下载并抽取一份材料，返回 {title, path, chars, text}。

    progress(frac, label)：frac 是这一份材料的完成度（0-1），供前端显示逐份进度。
    """
    def rep(frac, label):
        if progress:
            progress(max(0.0, min(1.0, frac)), label)

    rep(0.02, "开始下载")
    pdf = download(url, title, on_progress=lambda f, lab: rep(0.05 + 0.45 * f, "下载 " + lab))
    return load_file(pdf, title, budget=budget,
                     progress=lambda f, lab: rep(0.5 + 0.5 * f, lab))
