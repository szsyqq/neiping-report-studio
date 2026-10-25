#!/usr/bin/env python3
"""从 git 历史生成可运行的历史版本快照与时间线页。

产物：
  site/versions/<id>/index.html   每个版本一份当时的前端页面（注入兼容垫片）
  site/versions/index.html        时间线页（纯静态，可离线打开）

为什么要快照而不是只贴 git 链接：
  用户要的是「回到那个时候的页面，并且能跑」。git 里的旧文件能读不能跑，
  旧页面也没有登录逻辑、地址写死在 127.0.0.1。所以取出旧 HTML 后做两件事：
    1. 把写死的 http://127.0.0.1:8770 抹成空串 —— 快照由同一个服务提供，同源即可；
    2. 注入垫片：顶部横幅 + 给 fetch/XHR 补 Authorization（旧版没有 token 概念）。
  这样旧界面能对着当前后端真的取数、生成、导出，而不是一张死图。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "site" / "versions"
SPEC = BASE / "versions.json"


def git_show(tag: str, path: str) -> str:
    r = subprocess.run(["git", "show", f"{tag}:{path}"], cwd=BASE,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"取不到 {tag}:{path} —— {r.stderr.strip()[:200]}")
    return r.stdout


BAR_CSS = """
<style id="vbar-css">
/* 用文档流而不是 fixed：旧版页面自带 sticky header，fixed 会把它们压住 32px。
   横幅跟着滚走也无妨——用户是从「版本」面板点进来的，本来就知道自己在看旧版。 */
#vbar{position:relative;z-index:9;display:flex;align-items:center;
  gap:10px;padding:0 14px;height:32px;background:#1b1f24;color:#e8ebef;
  font:12px/32px -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;
  box-shadow:0 1px 0 rgba(255,255,255,.06);box-sizing:border-box}
#vbar .tag{flex:none;font-weight:600;font-size:11px;letter-spacing:.02em;
  color:#111418;background:#e8ebef;border-radius:3px;padding:1px 6px;line-height:17px}
#vbar .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#c8ced6}
#vbar .tt b{color:#fff;font-weight:600}
#vbar .st{flex:none;color:#8b939d;font-size:11px}
#vbar .st.ok{color:#7fd0a2}
#vbar .st.bad{color:#f0a6a6}
#vbar a{flex:none;color:#cfe2ff;text-decoration:none;font-size:11.5px;
  border:1px solid rgba(255,255,255,.22);border-radius:4px;padding:0 8px;line-height:20px;height:20px}
#vbar a:hover{background:rgba(255,255,255,.10)}
</style>
"""

SHIM = """
<script id="vbar-js">
(function () {
  var stat = document.getElementById("vstat");
  var tok = "";
  try { tok = localStorage.getItem("nrToken") || ""; } catch (e) {}
  try {
    var q = new URLSearchParams(location.search);
    if (q.get("t")) { tok = q.get("t"); localStorage.setItem("nrToken", tok); }
  } catch (e) {}

  function setStat(text, cls) { if (stat) { stat.textContent = text; stat.className = "st " + (cls || ""); } }

  // 旧版页面没有登录概念：补一层请求头，让它们能用当前服务的数据接口。
  if (tok && window.fetch) {
    var raw = window.fetch;
    window.fetch = function (u, o) {
      o = o || {};
      try {
        var h = new Headers(o.headers || {});
        if (!h.has("Authorization")) h.set("Authorization", "Bearer " + tok);
        o.headers = h;
      } catch (e) {}
      return raw.call(window, u, o).then(function (r) {
        if (r.status === 401) setStat("登录已失效", "bad");
        return r;
      });
    };
    var xo = XMLHttpRequest.prototype.open,
        xs = XMLHttpRequest.prototype.send,
        xh = XMLHttpRequest.prototype.setRequestHeader;
    XMLHttpRequest.prototype.open = function () { this.__vbody = 1; return xo.apply(this, arguments); };
    XMLHttpRequest.prototype.setRequestHeader = function (k) {
      if (String(k).toLowerCase() === "authorization") this.__vauth = 1;
      return xh.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      if (!this.__vauth) { try { xh.call(this, "Authorization", "Bearer " + tok); } catch (e) {} }
      return xs.apply(this, arguments);
    };
  }

  // 探活要校验内容：网关爱用 200 + 错误页，只看状态码会误判。
  if (window.fetch) {
    fetch("/api/health", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) throw new Error("bad");
        setStat(tok ? "数据接口已连接" : "已连接，未登录", tok ? "ok" : "");
      })
      .catch(function () { setStat("连不上服务 · 需配合本机或云端服务运行", "bad"); });
  }
})();
</script>
"""


def inject(html: str, v: dict) -> str:
    """抹掉写死的本机地址，并在页面最前面插入横幅。"""
    html = html.replace("http://127.0.0.1:8770", "")
    bar = f"""{BAR_CSS}
<div id="vbar">
  <span class="tag">v{v.get('ver') or v['label']} · {v['at'][5:]}</span>
  <span class="tt"><b>{v['title']}</b>　{v['summary']}</span>
  <span class="st" id="vstat">正在连接…</span>
  <a href="../">全部版本</a>
  <a href="../../">回到最新版</a>
</div>
{SHIM}"""
    if "<body" in html:
        i = html.index(">", html.index("<body")) + 1
        return html[:i] + "\n" + bar + html[i:]
    return bar + html


TIMELINE_CSS = """
:root{--ink:#16191d;--ink2:#3f4650;--muted:#7d848e;--line:#e6e8ec;--line2:#f0f2f5;--hover:#f7f8fa}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:var(--ink);
  font:14px/1.7 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:860px;margin:0 auto;padding:44px 24px 80px}
.top{display:flex;align-items:baseline;gap:10px;margin-bottom:6px;flex-wrap:wrap}
.top h1{font-size:20px;font-weight:600;margin:0;letter-spacing:.01em}
.top .n{font-size:12px;color:var(--muted)}
.lede{color:var(--ink2);font-size:13px;margin:0 0 26px;max-width:64ch}
.lede a{color:#2f5fd0;text-decoration:none}
.lede a:hover{text-decoration:underline}
/* 大版本：一块浅底卡片，写主要更新与主要修复；小版本挂在它下面的时间线上 */
.mj{margin:22px 0 10px;border:1px solid var(--line);border-radius:8px;padding:12px 14px;
  background:#fafbfc}
.mj .hd{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.mj .v{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}
.mj .t{font-size:14px;font-weight:600}
.mj .p{margin-left:auto;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.mj .s{font-size:12.5px;color:var(--ink2);margin:6px 0 0;max-width:70ch}
.mj .k{display:grid;grid-template-columns:64px 1fr;gap:2px 10px;margin:8px 0 0;font-size:12.5px}
.mj .k b{font-weight:600;color:var(--muted);font-size:11.5px;letter-spacing:.04em;padding-top:2px}
.mj .k.c b{color:#2f6b3a}
.mj .k.f b{color:#9a6700}
.mj .k ul{margin:0;padding-left:16px}
.mj .k li{margin:2px 0;color:var(--ink2)}
.grp{position:relative;padding-left:24px;margin:0 0 22px}
.grp::before{content:"";position:absolute;left:5px;top:4px;bottom:6px;width:1px;background:var(--line)}
.v{position:relative;padding:0 0 18px}
.v::before{content:"";position:absolute;left:-22px;top:6px;width:9px;height:9px;border-radius:50%;
  background:#fff;border:1.5px solid #c3c9d2}
.v.cur::before{border-color:var(--ink);background:var(--ink)}
.v .hd{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.v .lab{font-size:11.5px;font-weight:600;color:var(--ink2);letter-spacing:.02em;
  font-variant-numeric:tabular-nums}
.v .at{font-size:11.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.v .kind{font-size:10px;color:var(--muted);border:1px solid var(--line);border-radius:8px;
  padding:0 5px;line-height:15px}
.v .kind.mj{color:#1f4e79;border-color:#cfe0ef;background:#f4f9fd}
.v .cur-tag{font-size:10px;color:#2e7d32;background:#f1f8f1;border:1px solid #c8e6c9;
  border-radius:9px;padding:0 6px;line-height:16px}
.v h2{font-size:14px;font-weight:600;margin:3px 0 4px;line-height:1.5}
.v .sum{color:var(--ink2);font-size:12.5px;margin:0 0 6px;max-width:66ch}
.v details summary{cursor:pointer;font-size:11.5px;color:#2f5fd0;list-style:none;
  display:inline-block;outline:none}
.v details summary::-webkit-details-marker{display:none}
.v details summary::before{content:"▸ ";color:var(--muted)}
.v details[open] summary::before{content:"▾ "}
.v ul{margin:5px 0 0;padding-left:17px}
.v li{color:var(--ink2);font-size:12px;margin:3px 0;line-height:1.7}
.v .note{font-size:11.5px;color:#8a6d1f;background:#fffbf0;border:1px solid #f0e0b8;
  border-radius:5px;padding:5px 9px;margin:6px 0 0;max-width:66ch}
.v .open{display:inline-block;margin-top:6px;font-size:12px;color:#2f5fd0;text-decoration:none;
  border:1px solid #d6ddea;border-radius:5px;padding:3px 10px;background:#fbfcfe}
.v .open:hover{background:#f1f5fd;border-color:#bccbe6}
.foot{margin-top:26px;padding-top:16px;border-top:1px solid var(--line2);
  color:var(--muted);font-size:11.5px}
"""


def timeline_html(spec: dict) -> str:
    vs = spec["versions"]
    cur = spec["current"]
    ms = spec.get("majors") or []
    # 组的顺序取 versions 的自然顺序（新的在前）去重，最新的大版本置顶；
    # 不能按 majors 的声明顺序，否则 1.x 永远排在最上面。
    order = []
    for v in vs:
        mid = v.get("major") or "m?"
        if mid not in order:
            order.append(mid)

    blocks = []
    for mid in order:
        rows = [v for v in vs if (v.get("major") or "m?") == mid]
        if not rows:
            continue
        m = next((x for x in ms if x["id"] == mid), {}) or {}
        ver = m.get("ver") or str(rows[0].get("ver") or "").split(".")[0] or "?"
        card = [f'<div class="mj"><div class="hd"><span class="v">{ver}.x</span>'
                f'<span class="t">{m.get("title") or "未分类"}</span>'
                f'<span class="p">{m.get("period") or ""} · {len(rows)} 次改动</span></div>']
        if m.get("summary"):
            card.append(f'<p class="s">{m["summary"]}</p>')
        if m.get("core"):
            card.append('<div class="k c"><b>主要更新</b><ul>'
                        + "".join(f"<li>{i}</li>" for i in m["core"]) + "</ul></div>")
        if m.get("fixed"):
            card.append('<div class="k f"><b>主要修复</b><ul>'
                        + "".join(f"<li>{i}</li>" for i in m["fixed"]) + "</ul></div>")
        card.append("</div>")

        items = []
        for v in rows:
            is_cur = v["id"] == cur
            is_major = v.get("kind") == "major"
            lis = "".join(f"<li>{i}</li>" for i in v.get("items") or [])
            note = f'<div class="note">{v["note"]}</div>' if v.get("note") else ""
            items.append(f"""<div class="v{' cur' if is_cur else ''}">
  <div class="hd"><span class="lab">v{v.get('ver') or v['label']}</span>
    <span class="at">{v['at'][5:]}</span>
    <span class="kind{' mj' if is_major else ''}">{'大版本' if is_major else '小版本'}</span>
    {'<span class="cur-tag">当前</span>' if is_cur else ''}</div>
  <h2>{v['title']}</h2>
  <p class="sum">{v['summary']}</p>
  <details><summary>具体改动 {len(v.get('items') or [])} 项</summary><ul>{lis}</ul>{note}</details>
  <a class="open" href="./{v['id']}/">打开这个版本 →</a>
</div>""")
        blocks.append("".join(card) + '<div class="grp">' + "".join(items) + "</div>")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>版本历史 · 内评简评报告工作台</title>
<style>{TIMELINE_CSS}</style>
</head>
<body>
<div class="wrap">
  <div class="top"><h1>版本历史</h1>
    <span class="n">{len(order)} 个大版本 · {len(vs)} 次改动</span></div>
  <p class="lede">按大版本归档：每个大版本写明主要更新与主要修复，其下按时间列出各次改动。
    每次改动都留了一份当时的页面，点开能直接用——不是截图，是那时的界面接现在的数据接口。
    想回最新版去 <a href="../../">工作台</a>。</p>
  {''.join(blocks)}
  <div class="foot">快照由 <code>make_versions.py</code> 从 git 历史生成。清单在 <code>versions.json</code>，
    改完重跑脚本即可。</div>
</div>
</body>
</html>
"""


def main() -> None:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    # 清单是手写的，难免插入重复项；按 id 去重，保留先出现的那条，
    # 否则时间线会出现两行一样的版本、快照目录互相覆盖。
    seen, uniq = set(), []
    for v in spec["versions"]:
        if v["id"] in seen:
            print(f"  跳过重复版本 {v['id']}")
            continue
        seen.add(v["id"])
        uniq.append(v)
    spec["versions"] = vs = uniq
    latest = spec["current"]
    OUT.mkdir(parents=True, exist_ok=True)

    made = 0
    for v in vs:
        tag = (vs and next((x["tag"] for x in vs if x["id"] == v.get("sameAs")), None)) or v["tag"]
        html = git_show(tag, "app/static/index.html")
        d = OUT / v["id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "index.html").write_text(inject(html, v), encoding="utf-8")
        made += 1
        print(f"  {v['id']}  {v['label']:>4}  {len(html) // 1024:>3}KB  {v['title']}")

    (OUT / "index.html").write_text(timeline_html(spec), encoding="utf-8")
    total = sum(p.stat().st_size for p in OUT.rglob("*.html"))
    print(f"\n共 {made} 份快照 + 1 个时间线页，{total / 1024:.0f}KB → {OUT.relative_to(BASE)}")


if __name__ == "__main__":
    main()
