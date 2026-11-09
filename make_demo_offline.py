# -*- coding: utf-8 -*-
"""把工作台打成一个离线单文件 HTML（演示版）。

产物是 `site/demo.html`：一份文件，双击就能开，不需要后端、不需要口令。
打开时铺的是演示那一轮（中山城建），可以改字、加减条目、翻段、看检查栏；
生成、导出 Word、发信这些真的要后端的事，点下去给一句人话，不做假动作。

用法：
    python3 make_demo_offline.py

改前端之后要重跑一次；`app/static/offline-demo.js` 是垫片源码，
它只被这一份产物内联，线上版不加载。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
SRC = ROOT / "app" / "static" / "index.html"
SHIM = ROOT / "app" / "static" / "offline-demo.js"
OUT = ROOT / "site" / "demo.html"


def must_replace(html, old, new, what):
    """替换必须命中，命不中就报错——静默跳过会产出一份「看着像但其实没改」的文件。"""
    if old not in html:
        raise SystemExit(f"[x] 没找到要改的位置：{what}\n    期望出现：{old[:80]!r}")
    if html.count(old) > 1:
        raise SystemExit(f"[x] 要改的位置出现了 {html.count(old)} 次：{what}（应唯一）")
    return html.replace(old, new)


def main():
    if not SRC.exists():
        raise SystemExit(f"[x] 找不到前端：{SRC}")
    if not SHIM.exists():
        raise SystemExit(f"[x] 找不到垫片：{SHIM}")

    html = SRC.read_text(encoding="utf-8")
    shim = SHIM.read_text(encoding="utf-8")

    # 垫片靠 TOUR_KEY 这个名字把「首次自动播引导」关掉。前端改了名，
    # 这里必须当场报错——否则产物会悄悄退回自动播，打开先糊一层遮罩。
    m = re.search(r'const TOUR_KEY = "([^"]+)"', html)
    if not m:
        raise SystemExit("[x] 前端里找不到 TOUR_KEY 定义，垫片里的键名要跟着改")
    if '"%s"' % m.group(1) not in shim:
        raise SystemExit("[x] 垫片里的 TOUR_KEY 与前端不一致：前端是 %r" % m.group(1))

    html = must_replace(html, "<title>内评简评报告</title>",
                        "<title>内评简评报告 · 演示版（离线）</title>", "标题")
    # 口令页默认是 show + body.locked，离线版连登录都没有，先摘掉再谈别的，
    # 否则打开先闪一下「请输入访问口令」。
    html = must_replace(html, '<body class="locked">', '<body class="offline">', "body")
    html = must_replace(html, '<div id="gate" class="show">', '<div id="gate">', "口令遮罩")

    # 垫片放在 body 开头：比页面底部那段主脚本先执行，fetch 才拦得住
    html = must_replace(html, '<body class="offline">',
                        '<body class="offline">\n<script>\n' + shim + '\n</script>', "垫片位置")

    # 不登录、不探本机服务，直接铺示例
    html = must_replace(html, "\ngateBoot();\n", """
/* 离线演示版：没有服务端，也就没有口令这一步 */
if(window.OFFLINE_DEMO){
  unlockUI();
  bootData();
}else{
  gateBoot();
}
""", "启动入口")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")

    # 产物自检：判据是产物里的字符串，不是「脚本跑完了」
    got = OUT.read_text(encoding="utf-8")
    checks = [
        ("离线标记", "window.OFFLINE_DEMO = true" in got),
        ("口令页已摘", '<body class="offline">' in got and '<div id="gate" class="show">' not in got),
        ("不再直接 gateBoot", "\ngateBoot();\n" not in got),
        ("演示快照在位", "const TOUR_DATA = {" in got),
        ("无外链资源", "<script src" not in got and "<link " not in got),
    ]
    bad = [n for n, ok in checks if not ok]
    size = OUT.stat().st_size / 1024
    print(f"[ok] {OUT.relative_to(ROOT)}  {size:.0f} KB")
    for name, ok in checks:
        print(("  [ok] " if ok else "  [x]  ") + name)
    if bad:
        raise SystemExit("[x] 自检没过：" + "、".join(bad))


if __name__ == "__main__":
    main()
