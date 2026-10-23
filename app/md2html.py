"""把模板与方法论 md 转成可直接浏览的 HTML（无第三方依赖）。

用途：GitHub Pages 上 .md 会被浏览器下载而不是显示，转成 HTML 后可以直接读，
样式与工作台保持一致，表格也能正常渲染。
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DOCS = BASE / "site" / "docs"

CSS = """
:root{--bg:#12151c;--card:#171b24;--line:#242a36;--text:#dfe4ec;--muted:#8b93a3;--accent:#6ea8fe}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.75 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:36px 22px 80px}
a{color:var(--accent)}
h1{font-size:25px;margin:0 0 6px;letter-spacing:.2px}
h2{font-size:18.5px;margin:34px 0 10px;padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:15.5px;margin:22px 0 6px;color:#c9d3e3}
p{margin:9px 0}
ul,ol{margin:9px 0 9px 22px;padding:0}
li{margin:4px 0}
code{background:#0d1016;padding:1.5px 5px;border-radius:4px;font-size:13px;color:#a8c7fa}
pre{background:#0d1016;padding:13px 15px;border-radius:9px;overflow:auto;border:1px solid var(--line)}
pre code{background:none;padding:0;color:#cfe0ff}
table{border-collapse:collapse;width:100%;margin:13px 0;font-size:13.5px}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{background:#1d2330;font-weight:600}
tr:nth-child(even) td{background:#151922}
blockquote{margin:12px 0;padding:8px 14px;border-left:3px solid var(--accent);background:#161c27;color:var(--muted)}
.top{margin-bottom:22px;font-size:13px;color:var(--muted)}
"""


def inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    return s


def convert(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", ln)
        if m:
            n = len(m.group(1))
            out.append(f"<h{n}>{inline(m.group(2))}</h{n}>")
            i += 1
            continue
        # 表格：| a | b | 紧跟 |---|---|
        if ln.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in ln.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            t = ["<table><thead><tr>"] + [f"<th>{inline(c)}</th>" for c in header] + ["</tr></thead><tbody>"]
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
            continue
        if re.match(r"^\s*[-*]\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
            continue
        if re.match(r"^\s*\d+[.)]\s+", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ol>")
            continue
        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].startswith(">"):
                buf.append(lines[i].lstrip("> ").strip())
                i += 1
            out.append("<blockquote>" + inline(" ".join(buf)) + "</blockquote>")
            continue
        if not ln.strip():
            i += 1
            continue
        buf = [ln.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,4}\s|\||```|>|\s*[-*]\s|\s*\d+[.)]\s)", lines[i]):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>" + inline("".join(buf)) + "</p>")
    return "\n".join(out)


def build(name: str, title: str) -> Path:
    src = DOCS / name
    body = convert(src.read_text(encoding="utf-8"))
    page = (f"<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class=\"wrap\">"
            f"<div class=\"top\"><a href=\"../\">← 工作台</a></div>{body}</div></body></html>")
    out = src.with_suffix(".html")
    out.write_text(page, encoding="utf-8")
    return out


if __name__ == "__main__":
    for f, t in (("template-v1.1.md", "内评简评报告通用模板 v1.1"),
                 ("method-v1.1.md", "内评简评报告写作方法论 v1.1")):
        print(build(f, t))
