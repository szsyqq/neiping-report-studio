/* 全流程演示层：任务列表 → 报告编辑 → 级别认定 → 债项评级 → 提交审批
   ─────────────────────────────────────────────────────────────
   这一层只出现在演示产物里（make_demo_offline.py 把它内联进 site/demo.html，
   排在主脚本之后）。线上工作台不加载它，也就不会被它改到。

   分工：第 2 步不另画界面，直接把工作台本身露出来——报告编辑器是真的，
   能改字、能翻段、能看检查栏；本层负责它前后的四步，以及第 2 步那条底部导航。
   流程里的数据（级别、债项、意见、审批状态）只活在这一层，不进 S，也不发请求。 */
(function(){
  "use strict";

  /* ── 演示数据 ─────────────────────────────────────────────── */
  var CO = {
    name: "中山城市建设集团有限公司",
    industry: "城投",
    model: "城建类-其他",
    year: 2025,
    task: "RT-2026-1108-017",
    analyst: "潘宇平",
    reviewer: "王凯",
    reviewerRole: "评级总监",
    fin: 52.6,
    nonfin: 23.4,
    ext: { org: "中诚信国际", level: "AA+", date: "2026-06-18" },
    bonds: [
      { code: "102680123", short: "26中山城建MTN001", type: "中期票据",
        amount: "10.00", due: "2031-03-12", gtype: "保证担保", gman: "中山市国资委" },
      { code: "042680456", short: "26中山城建CP001", type: "短期融资券",
        amount: "5.00", due: "2027-01-20", gtype: "无", gman: "—" }
    ]
  };

  /* 内评级别：1A 最好，9D 最差，DDD 为违约。与截图里的底稿同一套刻度。 */
  var LEVELS = [];
  ["1","2","3","4","5","6","7","8","9"].forEach(function(n){
    ["A","B","C","D"].forEach(function(s){ LEVELS.push(n + s); });
  });
  var MODEL_LEVEL = "4C";
  var MODEL_SCORE = +(CO.fin * 0.6 + CO.nonfin * 0.4).toFixed(2);

  var TASKS = [
    { name: CO.name, tag: "国企", type: "主体+债项(2)", industry: "城投", analyst: "潘宇平",
      status: "处理中", model: "城建类-其他", due: "2026/12/31", level: "4C", live: true },
    { name: "辽宁方大集团实业有限公司", tag: "民营", type: "主体+债项(1)", industry: "材料",
      analyst: "潘宇平", status: "待处理", model: "大型建材材料-其他", due: "2026/11/30",
      level: "4C", live: false },
    { name: "珠海香洲正菱控股有限公司", tag: "国企", type: "主体+债项(1)", industry: "城投",
      analyst: "李文彬", status: "待处理", model: "城建类-其他", due: "2026/12/20",
      level: "3B", live: false },
    { name: "河源润业投资有限公司", tag: "国企", type: "主体", industry: "城投",
      analyst: "陈路", status: "已提交", model: "城建类-其他", due: "2026/09/30",
      level: "5C", live: false },
    { name: "佛山高明发展投资建设有限公司", tag: "国企", type: "主体", industry: "城投",
      analyst: "陈路", status: "已提交", model: "城建类-其他", due: "2026/10/31",
      level: "4B", live: false }
  ];

  var S = {
    step: 1, max: 1, dir: 1,
    tab: "全部", kw: "",
    level: MODEL_LEVEL, adj: 0, reason: "",
    bonds: CO.bonds.map(function(b){ return { level: "", note: "" }; }),
    bondOpinion: "",
    opinion: "",
    status: "draft",      // draft → review → done / returned
    reviewerNote: "",
    at: ""
  };

  /* ── 小工具 ───────────────────────────────────────────────── */
  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function lvIndex(v){ return LEVELS.indexOf(v); }
  function bondLevel(i){
    var v = S.bonds[i].level;
    return v || S.level;
  }
  /* 主体选了「都不评」时，债项也谈不上级别，写「不评级」而不是把四个字塞进级别位 */
  function bondWord(i){
    if(S.level === "都不评") return "不评级";
    return bondLevel(i);
  }
  function levelWord(v){
    if(v === "都不评") return "不评级";
    if(!v) return "待选择";
    return v;
  }
  function nowText(){
    var d = new Date();
    function p(n){ return n < 10 ? "0" + n : "" + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
           " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function toastMsg(msg){
    if(typeof window.toast === "function"){ toast(msg); }
  }

  /* 定性调整：只让选一档，够讲清「模型给 4C，人也可以动一档」这件事 */
  var ADJ = [
    { v: 0,  t: "不作调整" },
    { v: -1, t: "上调一档" },
    { v: 1,  t: "下调一档" }
  ];
  function adjLevel(){
    var i = lvIndex(S.level);
    if(i < 0) return S.level;
    var j = Math.min(LEVELS.length - 1, Math.max(0, i + S.adj));
    return LEVELS[j];
  }

  /* ── 样式 ─────────────────────────────────────────────────── */
  var CSS = [
    "#flow{position:fixed;inset:0;z-index:200;display:flex;flex-direction:column;background:#f3f4f6;",
    "  font-size:13px;color:var(--ink);overflow:hidden}",
    "#flow .fl-hd{flex:none;height:52px;display:flex;align-items:center;gap:16px;padding:0 18px;",
    "  background:#fff;border-bottom:1px solid var(--line)}",
    "#flow .fl-logo{width:26px;height:26px;border-radius:7px;background:var(--ink);color:#fff;",
    "  display:grid;place-items:center;font-size:13px;font-weight:600;flex:none}",
    "#flow .fl-hd-l{display:flex;align-items:center;gap:9px;min-width:0}",
    "#flow .fl-hd-l b{font-size:13.5px;font-weight:600;white-space:nowrap}",
    "#flow .fl-hd-l i{display:block;font-style:normal;font-size:11px;color:var(--muted);line-height:1.2}",
    "#flow .fl-nav{display:flex;align-items:center;gap:1px;margin-left:auto;overflow:hidden}",
    "#flow .fl-nav button{border:0;background:transparent;display:flex;align-items:center;gap:6px;",
    "  padding:5px 9px;border-radius:6px;color:var(--muted);font-size:12px;white-space:nowrap}",
    "#flow .fl-nav button b{width:17px;height:17px;border-radius:50%;border:1px solid var(--line);",
    "  display:grid;place-items:center;font-size:10px;font-weight:500;flex:none;",
    "  font-variant-numeric:tabular-nums}",
    "#flow .fl-nav button:hover{background:#f5f6f8;color:var(--ink2)}",
    "#flow .fl-nav button.on{background:#eef0f3;color:var(--ink);font-weight:500}",
    "#flow .fl-nav button.on b{background:var(--ink);border-color:var(--ink);color:#fff}",
    "#flow .fl-nav button.done b{background:#fff;border-color:#b9c0c8;color:var(--ink2)}",
    "#flow .fl-nav button[disabled]{color:#c2c8d0;cursor:default;background:transparent}",
    "#flow .fl-nav button[disabled] b{border-color:#e2e5e9;color:#aeb5be}",
    "#flow .fl-nav i{font-style:normal;color:#d8dce1;font-size:11px}",
    "#flow .fl-hd-r{display:flex;align-items:center;gap:10px;flex:none}",
    "#flow .fl-badge{font-size:11px;color:#7a5a08;background:#fff8e6;border:1px solid #f0dfae;",
    "  border-radius:999px;padding:1px 8px;white-space:nowrap}",
    "#flow .fl-x{border:1px solid var(--line);background:#fff;border-radius:6px;padding:4px 10px;",
    "  font-size:12px;color:var(--ink2)}",
    "#flow .fl-x:hover{border-color:#c4c9d1;background:var(--hover)}",
    "#flow .fl-body{flex:1;overflow-y:auto;padding:20px 22px 96px}",
    /* 换页带方向：往下一步从右边推进来，退回上一步从左邊滑回来 */
    "#flow .fl-page{max-width:1120px;margin:0 auto}",
    "#flow .fl-page.dirR{animation:flInR .3s cubic-bezier(.22,.7,.3,1) both}",
    "#flow .fl-page.dirL{animation:flInL .3s cubic-bezier(.22,.7,.3,1) both}",
    "@keyframes flInR{from{opacity:0;transform:translate3d(34px,0,0)}to{opacity:1;transform:none}}",
    "@keyframes flInL{from{opacity:0;transform:translate3d(-34px,0,0)}to{opacity:1;transform:none}}",
    /* 同页重绘（选级别、切标签）不重播入场，否则点一下整页闪一次 */
    "#flow .fl-page.quiet *{animation:none!important}",
    "#flow .fl-ph{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:14px}",
    "#flow h1{font-size:17px;font-weight:600;margin:0 0 4px;letter-spacing:.2px}",
    "#flow .fl-ph p{margin:3px 0 0;color:var(--muted);font-size:12px;line-height:1.6}",
    "#flow .fl-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}",
    "#flow .fl-kpis div{background:#fff;border:1px solid var(--line);border-radius:9px;padding:10px 14px}",
    "#flow .fl-kpis b{display:block;font-size:19px;font-weight:600;line-height:1.3;",
    "  font-variant-numeric:tabular-nums}",
    "#flow .fl-kpis span{font-size:11.5px;color:var(--muted)}",
    "#flow .fl-card{background:#fff;border:1px solid var(--line);border-radius:10px;",
    "  padding:14px 16px 15px;margin-bottom:14px}",
    "#flow .fl-card>h3{margin:0 0 11px;font-size:13px;font-weight:600;display:flex;align-items:baseline;gap:8px}",
    "#flow .fl-card>h3 i{font-style:normal;font-weight:400;color:var(--muted);font-size:11.5px}",
    "#flow .fl-card>h3 .r{margin-left:auto;font-weight:400;color:var(--muted);font-size:11.5px}",
    "#flow table{width:100%;border-collapse:collapse;font-size:12.5px}",
    "#flow th{background:#fafbfc;text-align:left;font-weight:500;color:var(--ink2);padding:7px 10px;",
    "  border-bottom:1px solid var(--line);white-space:nowrap;font-size:11.5px}",
    "#flow td{padding:8px 10px;border-bottom:1px solid var(--line2);vertical-align:middle}",
    "#flow tr:last-child td{border-bottom:0}",
    "#flow .num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}",
    "#flow .fl-tw{overflow:auto;margin:0 -16px;padding:0 16px}",
    "#flow .fl-tools{display:flex;align-items:center;gap:12px;margin-bottom:10px;flex-wrap:wrap}",
    "#flow .fl-tabs{display:flex;gap:2px;background:#f4f5f7;border:1px solid var(--line2);",
    "  border-radius:7px;padding:2px}",
    "#flow .fl-tabs button{border:0;background:transparent;padding:4px 10px;border-radius:5px;",
    "  font-size:12px;color:var(--ink2)}",
    "#flow .fl-tabs button.on{background:#fff;color:var(--ink);font-weight:500;",
    "  box-shadow:0 1px 2px rgba(17,20,24,.06)}",
    "#flow .fl-search{margin-left:auto;display:flex;align-items:center;gap:8px}",
    "#flow .fl-search input{border:1px solid var(--line);border-radius:6px;padding:5px 9px;",
    "  font-size:12.5px;width:190px;outline:none}",
    "#flow .fl-search input:focus{border-color:#c4c9d1}",
    "#flow .fl-search span{font-size:11.5px;color:var(--muted);white-space:nowrap}",
    "#flow .fl-op{border:1px solid var(--line);background:#fff;border-radius:5px;padding:2px 9px;",
    "  font-size:11.5px;color:var(--ink);white-space:nowrap}",
    "#flow .fl-op:hover{border-color:var(--ink);background:var(--ink);color:#fff}",
    "#flow .fl-op.ghost{border-color:transparent;color:var(--muted)}",
    "#flow .fl-op.ghost:hover{border-color:var(--line);background:#fff;color:var(--ink)}",
    "#flow .tg{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;",
    "  border:1px solid var(--line);color:var(--ink2);white-space:nowrap}",
    "#flow .tg.run{color:#8a5a00;background:#fff8e6;border-color:#f0dfae}",
    "#flow .tg.wait{color:var(--muted);background:#fafbfc}",
    "#flow .tg.done{color:#1f5c3d;background:#eef7f1;border-color:#cfe6d8}",
    "#flow .tg.live{color:#1f3f7a;background:#eef3fb;border-color:#d3e0f3}",
    "#flow .fl-lv{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:5px;max-width:560px}",
    "#flow .fl-lv button{border:1px solid var(--line);background:#fff;border-radius:6px;",
    "  padding:5px 0;font-size:12px;color:var(--ink2);font-variant-numeric:tabular-nums;",
    "  transition:background .12s ease,border-color .12s ease,color .12s ease}",
    "#flow .fl-lv button:hover{border-color:#b9c0c8;background:var(--hover)}",
    "#flow .fl-lv button.on{background:var(--ink);border-color:var(--ink);color:#fff;font-weight:600}",
    "#flow .fl-lv button.sp{grid-column:span 2;display:flex;align-items:center;",
    "  justify-content:center;gap:5px;background:#fbfbfc}",
    "#flow .fl-lv button.sp i{font-style:normal;color:var(--muted);font-size:10px}",
    "#flow .fl-lv button.sp.on{background:var(--ink)}",
    "#flow .fl-lv button.sp.on i{color:#c9ced6}",
    "#flow .fl-pick{display:flex;align-items:center;gap:10px;margin:12px 0 4px;",
    "  font-size:12.5px;color:var(--ink2)}",
    "#flow .fl-pick b{font-size:20px;font-weight:600;color:var(--ink);letter-spacing:.5px;",
    "  font-variant-numeric:tabular-nums}",
    "#flow .fl-dev{font-size:11.5px;color:var(--muted)}",
    "#flow .fl-dev.warn{color:#8a5a00}",
    "#flow textarea{width:100%;border:1px solid var(--line);border-radius:7px;padding:9px 11px;",
    "  font-size:12.5px;line-height:1.75;outline:none;resize:vertical;background:#fff;",
    "  font-family:inherit;color:inherit}",
    "#flow textarea:focus{border-color:#c4c9d1}",
    "#flow .fl-hint{margin:8px 0 0;font-size:11.5px;color:var(--muted);line-height:1.7}",
    "#flow .fl-stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}",
    "#flow .fl-stats div{border:1px solid var(--line2);border-radius:8px;padding:9px 12px;background:#fbfbfc}",
    "#flow .fl-stats b{display:block;font-size:17px;font-weight:600;line-height:1.4;",
    "  font-variant-numeric:tabular-nums}",
    "#flow .fl-stats span{font-size:11px;color:var(--muted)}",
    "#flow .fl-stats .key{background:#fff;border-color:#cfd4db}",
    "#flow .fl-grid{display:grid;grid-template-columns:minmax(0,1fr) 306px;gap:14px;align-items:start}",
    "#flow .fl-main{min-width:0}",
    "#flow .fl-side .fl-card{margin-bottom:14px}",
    "#flow .fl-kv{display:flex;justify-content:space-between;gap:12px;padding:5px 0;font-size:12.5px;",
    "  border-bottom:1px dashed var(--line2)}",
    "#flow .fl-kv:last-child{border-bottom:0}",
    "#flow .fl-kv span{color:var(--muted);flex:none}",
    "#flow .fl-kv b{font-weight:500;text-align:right;min-width:0;word-break:break-all}",
    "#flow .fl-kv b.big{font-size:15px}",
    "#flow .fl-list{margin:0;padding-left:16px;font-size:12px;color:var(--ink2);line-height:1.8}",
    "#flow .fl-list li{margin:0;padding:0}",
    "#flow .fl-sel{width:auto;min-width:104px;padding:4px 6px;font-size:12px;border-radius:5px}",
    "#flow .fl-warn{display:block;font-size:11px;color:#8a5a00;margin-top:4px;line-height:1.5}",
    "#flow .fl-flow{display:flex;flex-direction:column;gap:0}",
    "#flow .fl-n{position:relative;padding:0 0 14px 20px;font-size:12.5px}",
    "#flow .fl-n:last-child{padding-bottom:0}",
    "#flow .fl-n:before{content:'';position:absolute;left:3px;top:5px;width:7px;height:7px;",
    "  border-radius:50%;border:1px solid #c9ced6;background:#fff;z-index:1}",
    "#flow .fl-n:after{content:'';position:absolute;left:6px;top:12px;bottom:0;width:1px;",
    "  background:var(--line)}",
    "#flow .fl-n:last-child:after{display:none}",
    "#flow .fl-n.done:before{background:var(--ink);border-color:var(--ink)}",
    "#flow .fl-n.on:before{background:#fff;border-color:var(--ink);box-shadow:0 0 0 3px #eef0f3}",
    "#flow .fl-n b{display:block;font-weight:500;line-height:1.5}",
    "#flow .fl-n span{color:var(--muted);font-size:11.5px}",
    "#flow .fl-n em{font-style:normal;color:#8a5a00;font-size:11.5px}",
    "#flow .fl-sumb{display:flex;align-items:center;gap:12px;padding:11px 14px;border-radius:9px;",
    "  border:1px solid #f0dfae;background:#fffaf0;font-size:12.5px;margin-bottom:14px}",
    "#flow .fl-sumb b{font-weight:600}",
    "#flow .fl-sumb.ok{border-color:#cfe6d8;background:#f3faf5}",
    "#flow .fl-sumb.bad{border-color:#f0d5d1;background:#fdf5f4}",
    "#flow .fl-thru{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:14px}",
    "#flow .fl-thru div{font-size:11.5px;color:var(--muted);line-height:1.7}",
    "#flow .fl-thru b{display:block;color:var(--ink);font-weight:500;font-size:12px;margin-bottom:1px}",
    "#flow .fl-thru em{font-style:normal;color:#b9c0c8;margin-right:6px;",
    "  font-variant-numeric:tabular-nums}",
    "#flow .fl-bar{display:flex;align-items:center;gap:10px;margin-top:6px;flex-wrap:wrap}",
    "#flow .fl-btn{border:1px solid var(--line);background:#fff;border-radius:7px;padding:7px 16px;",
    "  font-size:12.5px;color:var(--ink)}",
    "#flow .fl-btn:hover{border-color:#c4c9d1;background:var(--hover)}",
    "#flow .fl-btn.primary{background:var(--ink);border-color:var(--ink);color:#fff}",
    "#flow .fl-btn.primary:hover{background:#000}",
    "#flow .fl-btn.primary[disabled]{background:#c9ccd1;border-color:#c9ccd1;cursor:not-allowed}",
    "#flow .fl-btn.ghost{border-color:transparent;color:var(--ink2)}",
    "#flow .fl-btn.ghost:hover{border-color:var(--line);background:#fff}",
    "#flow .fl-done{max-width:560px;margin:60px auto;background:#fff;border:1px solid var(--line);",
    "  border-radius:14px;padding:34px 34px 28px;text-align:center}",
    "#flow .fl-ck{width:48px;height:48px;border-radius:50%;background:var(--ink);color:#fff;",
    "  display:grid;place-items:center;margin:0 auto 16px;font-size:22px}",
    "#flow .fl-done h2{font-size:18px;margin:0 0 8px}",
    "#flow .fl-done p{color:var(--muted);font-size:12.5px;margin:0 0 18px;line-height:1.8}",
    "#flow .fl-done .fl-kv{text-align:left;max-width:296px;margin:0 auto 18px}",
    "#flow .fl-chk{display:flex;align-items:flex-start;gap:8px;font-size:11.5px;color:var(--ink2);",
    "  line-height:1.6;padding:3px 0}",
    "#flow .fl-chk s{width:13px;height:13px;border-radius:3px;background:var(--ink);color:#fff;",
    "  text-decoration:none;display:grid;place-items:center;font-size:9px;flex:none;margin-top:2px}",

    /* ── 进度：标题条顶端的细进度 + 页头的圈 ── */
    "#flow .fl-hd{position:relative}",
    "#flow .fl-hd:after{content:'';position:absolute;left:0;bottom:-1px;height:2px;width:var(--flp,20%);",
    "  background:linear-gradient(90deg,#a8b0b8,#2b323a);border-radius:0 2px 2px 0;",
    "  transition:width .5s cubic-bezier(.3,.8,.3,1)}",
    "#flow .fl-step{display:flex;align-items:center;gap:9px;flex:none;margin-left:2px;",
    "  padding-left:14px;border-left:1px solid var(--line2)}",
    "#flow .fl-ring-w{position:relative;width:32px;height:32px;flex:none}",
    "#flow .fl-ring-w b{position:absolute;inset:0;display:grid;place-items:center;font-size:11px;",
    "  font-weight:600;font-variant-numeric:tabular-nums}",
    "#flow .fl-ring{width:32px;height:32px;flex:none;transform:rotate(-90deg)}",
    "#flow .fl-ring circle{fill:none;stroke-width:2.6}",
    "#flow .fl-ring .rbg{stroke:#eceef1}",
    "#flow .fl-ring .rfg{stroke:var(--ink);stroke-linecap:round;",
    "  transition:stroke-dashoffset .55s cubic-bezier(.3,.8,.3,1)}",
    "#flow .fl-ring-t{font-size:11px;line-height:1.35;white-space:nowrap}",
    "#flow .fl-ring-t b{display:block;font-size:11px;font-weight:600;letter-spacing:.2px}",
    "#flow .fl-ring-t span{color:var(--muted);font-size:10.5px}",
    "#flow .fl-pct{font-variant-numeric:tabular-nums;color:var(--ink2);font-weight:600}",

    /* ── 模型打分：算式条 + 分项得分 ── */
    "#flow .fl-meta{display:flex;flex-wrap:wrap;background:#fff;border:1px solid var(--line);",
    "  border-radius:9px;padding:8px 14px;margin-bottom:14px;font-size:12px}",
    "#flow .fl-meta span{display:flex;align-items:baseline;gap:6px;padding-right:16px;",
    "  margin-right:16px;border-right:1px solid var(--line2);line-height:1.9}",
    "#flow .fl-meta span:last-child{border-right:0;margin-right:0;padding-right:0}",
    "#flow .fl-meta em{font-style:normal;color:var(--muted);font-size:11px;white-space:nowrap}",
    "#flow .fl-meta b{font-weight:500}",
    "#flow .fl-eq{display:flex;align-items:center;gap:7px;flex-wrap:wrap;font-size:12.5px;",
    "  color:var(--ink2);background:#fbfbfc;border:1px solid var(--line2);border-radius:8px;",
    "  padding:9px 12px;margin-bottom:11px}",
    "#flow .fl-eq em{font-style:normal;color:var(--muted);font-size:11.5px}",
    "#flow .fl-eq>b{font-size:15px;font-variant-numeric:tabular-nums;letter-spacing:.2px}",
    "#flow .fl-eq .ar{color:#b9c0c8}",
    "#flow .fl-eq .lv{background:var(--ink);color:#fff;border-radius:5px;padding:1px 8px;",
    "  font-size:12px;font-weight:600;letter-spacing:.4px;margin-left:2px}",
    "#flow .fl-eq .t{color:var(--muted);font-size:11.5px;margin-left:auto}",
    "#flow .fl-sc{display:flex;align-items:center;gap:7px;justify-content:flex-end}",
    "#flow .fl-sc i{display:block;width:64px;height:5px;border-radius:3px;background:#e9ebef;",
    "  overflow:hidden;flex:none}",
    "#flow .fl-sc i em{display:block;height:100%;border-radius:3px;background:#7c8794;",
    "  transform-origin:left;animation:barGrow .55s cubic-bezier(.3,.8,.3,1) both}",
    "#flow .fl-sc b{font-weight:600;font-variant-numeric:tabular-nums;min-width:34px;text-align:right}",
    "@keyframes barGrow{from{transform:scaleX(0)}to{transform:scaleX(1)}}",
    "#flow .fl-grp{font-size:11px;color:var(--muted);background:#f7f8f9;font-weight:500}",
    "#flow .fl-grp b{color:var(--ink2);font-weight:500}",
    "#flow .fl-sum td{background:#fafbfc;font-weight:500}",
    "#flow .fl-hl{background:#fffbeb}",
    "#flow tr.fl-total td{border-top:1px solid var(--line);border-bottom:0;padding-top:9px}",

    /* ── 级别刻度：模型在哪、本次定在哪 ── */
    "#flow .fl-scale{position:relative;height:72px;margin:6px 0 6px}",
    "#flow .fl-track{position:absolute;left:26px;right:26px;top:30px;height:6px;border-radius:3px;",
    "  background:linear-gradient(90deg,#c3d2ea,#d9dde3 48%,#ead4ce)}",
    "#flow .fl-tk{position:absolute;top:24px;width:1px;height:18px;background:#e4e7eb}",
    "#flow .fl-mk{position:absolute;transform:translateX(-50%);display:flex;flex-direction:column;",
    "  align-items:center;transition:left .45s cubic-bezier(.25,.8,.3,1)}",
    "#flow .fl-mk.mdl{top:0}",
    "#flow .fl-mk.mdl b{font-size:10.5px;color:var(--muted);font-weight:500;white-space:nowrap;",
    "  letter-spacing:.2px}",
    "#flow .fl-mk.mdl i{width:0;height:0;border-left:5px solid transparent;",
    "  border-right:5px solid transparent;border-top:6px solid #9aa3ad;margin-top:8px}",
    "#flow .fl-mk.cur{top:37px}",
    "#flow .fl-mk.cur i{width:10px;height:10px;border-radius:50%;background:var(--ink);",
    "  box-shadow:0 0 0 3px #fff,0 0 0 4px #dfe3e8}",
    "#flow .fl-mk.cur b{font-size:10.5px;font-weight:600;white-space:nowrap;margin-top:4px;",
    "  font-variant-numeric:tabular-nums}",
    "#flow .fl-axes{display:flex;justify-content:space-between;font-size:10px;color:#b3bac2;",
    "  margin:0 20px 10px;font-variant-numeric:tabular-nums}",

    /* ── 小块动效 ── */
    "#flow .fl-page:not(.quiet) .fl-card{animation:cardIn .36s cubic-bezier(.22,.7,.3,1) both}",
    "@keyframes cardIn{from{opacity:0;transform:translate3d(0,8px,0)}to{opacity:1;transform:none}}",
    "#flow .fl-page:not(.quiet) .fl-main>.fl-card:nth-child(2){animation-delay:.06s}",
    "#flow .fl-page:not(.quiet) .fl-main>.fl-card:nth-child(3){animation-delay:.12s}",
    "#flow .fl-page:not(.quiet) .fl-main>.fl-card:nth-child(4){animation-delay:.17s}",
    "#flow .fl-page:not(.quiet) .fl-side{animation:cardIn .4s cubic-bezier(.22,.7,.3,1) .1s both}",
    "#flow .fl-lv button.on{animation:lvPop .24s cubic-bezier(.2,.9,.3,1.4)}",
    "@keyframes lvPop{0%{transform:scale(.9)}100%{transform:scale(1)}}",
    "#flow .fl-lv button{transition:background .12s ease,border-color .12s ease,color .12s ease,",
    "  transform .12s ease}",
    "#flow .fl-lv button:active{transform:scale(.94)}",
    "#flow .fl-page:not(.quiet) .fl-chk{animation:chkIn .3s ease both}",
    "#flow .fl-page:not(.quiet) .fl-chk:nth-child(2){animation-delay:.05s}",
    "#flow .fl-page:not(.quiet) .fl-chk:nth-child(3){animation-delay:.1s}",
    "#flow .fl-page:not(.quiet) .fl-chk:nth-child(4){animation-delay:.15s}",
    "@keyframes chkIn{from{opacity:0;transform:translateX(-6px)}to{opacity:1;transform:none}}",
    "#flow .fl-done .fl-ck{animation:ckPop .52s cubic-bezier(.2,.9,.3,1.3) both}",
    "@keyframes ckPop{0%{opacity:0;transform:scale(.4)}62%{opacity:1;transform:scale(1.12)}",
    "  100%{opacity:1;transform:scale(1)}}",
    "#flow .fl-done{animation:cardIn .4s cubic-bezier(.22,.7,.3,1) both}",
    "#flow .fl-kpis div{transition:border-color .2s ease}",
    "#flow .fl-kpis div:hover{border-color:#c4c9d1}",
    "#flowBar.show{animation:barIn .32s cubic-bezier(.22,.7,.3,1) both}",
    "@keyframes barIn{from{opacity:0;transform:translate(-50%,14px)}to{opacity:1;transform:translateX(-50%)}}",
    "#flowBar .fb-prog{display:flex;gap:3px;align-items:center;padding:0 2px 0 6px}",
    "#flowBar .fb-prog i{width:15px;height:4px;border-radius:2px;background:#e3e6ea;",
    "  transition:background .3s ease}",
    "#flowBar .fb-prog i.on{background:var(--ink)}",
    /* 不想看动效的机器上就别动 */
    "@media (prefers-reduced-motion:reduce){",
    "  #flow *, #flowBar *{animation-duration:.001s!important;",
    "    animation-delay:0s!important;transition-duration:.001s!important}",
    "}",
    /* 第 2 步：工作台自己露出来，靠底部一条导航把流程接上 */
    "#flowBar{position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:190;",
    "  display:none;align-items:center;gap:10px;background:#fff;border:1px solid var(--line);",
    "  border-radius:999px;box-shadow:0 12px 30px -14px rgba(17,20,24,.4);",
    "  padding:6px 7px 6px 6px;max-width:calc(100vw - 20px)}",
    "#flowBar.show{display:flex}",
    "#flowBar button{border:1px solid var(--line);background:#fff;border-radius:999px;",
    "  padding:6px 13px;font-size:12px;color:var(--ink2);white-space:nowrap}",
    "#flowBar button:hover{border-color:#c4c9d1;background:var(--hover);color:var(--ink)}",
    "#flowBar button.primary{background:var(--ink);border-color:var(--ink);color:#fff}",
    "#flowBar button.primary:hover{background:#000}",
    "#flowBar .fb-mid{display:flex;align-items:center;gap:9px;padding:0 8px 0 12px;min-width:0}",
    "#flowBar .fb-mid b{font-size:12px;font-weight:600;white-space:nowrap;flex:none}",
    "#flowBar .fb-mid span{font-size:11.5px;color:var(--muted);white-space:nowrap;",
    "  overflow:hidden;text-overflow:ellipsis;max-width:380px;padding-left:9px;border-left:1px solid var(--line)}",
    "#flAct{display:none}",
    "body.flow-open{overflow:hidden}",
    "body.flow-bar #fabNav{display:none!important}",
    "body.flow-bar #toast{bottom:96px}",
    "#toast{z-index:400}",
    "@media (max-width:1000px){",
    "  #flow .fl-grid{grid-template-columns:minmax(0,1fr)}",
    "  #flow .fl-side{order:2}",
    "  #flow .fl-stats{grid-template-columns:repeat(2,minmax(0,1fr))}",
    "}",
    "@media (max-width:760px){",
    "  #flow .fl-hd{height:auto;flex-wrap:wrap;padding:8px 12px;gap:8px}",
    /* 手机上一行放不下五个「圆 + 文字」，只留圆；圆与圆之间拉开，不然挤成一坨 */
    "  #flow .fl-nav{order:3;width:100%;margin:0;justify-content:center;gap:16px;overflow:visible}",
    "  #flow .fl-nav button{padding:0;gap:0}",
    "  #flow .fl-nav button b{width:22px;height:22px;font-size:11px}",
    "  #flow .fl-nav button span{display:none}",
    "  #flow .fl-nav i{display:none}",
    "  #flow .fl-sc i{width:44px}",
    "  #flow .fl-sc b{min-width:30px;font-size:12px}",
    "  #flow .fl-hd-l b{font-size:12.5px}",
    "  #flow .fl-hd-l i{display:none}",
    "  #flow .fl-badge{display:none}",
    "  #flow .fl-body{padding:14px 12px 100px}",
    "  #flow .fl-kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}",
    "  #flow .fl-kpis b{font-size:16px}",
    "  #flow h1{font-size:15px}",
    "  #flow .fl-lv{max-width:none;grid-template-columns:repeat(4,minmax(0,1fr))}",
    "  #flow .fl-search{width:100%;margin-left:0}",
    "  #flow .fl-search input{flex:1;width:auto}",
    // 只藏任务表里那些「手机上看了也看不清」的列；其余表格不动，
    // 早先按 nth-child 全局藏列，把级别认定页的得分列一起藏了。
    "  #flow .fl-task th:nth-child(3),#flow .fl-task td:nth-child(3),",
    "  #flow .fl-task th:nth-child(4),#flow .fl-task td:nth-child(4),",
    "  #flow .fl-task th:nth-child(5),#flow .fl-task td:nth-child(5),",
    "  #flow .fl-task th:nth-child(6),#flow .fl-task td:nth-child(6),",
    "  #flow .fl-task th:nth-child(8),#flow .fl-task td:nth-child(8),",
    "  #flow .fl-task th:nth-child(9),#flow .fl-task td:nth-child(9){display:none}",
    "  #flow .fl-score th:nth-child(3),#flow .fl-score td:nth-child(3):not(.num){display:none}",
    "  #flow .fl-score td:first-child{white-space:nowrap}",
    "  #flow .fl-meta{display:grid;grid-template-columns:1fr 1fr;gap:0 10px;padding:8px 12px}",
    "  #flow .fl-meta span{border-right:0;margin-right:0;padding-right:0}",
    "  #flow .fl-step .fl-ring-t{display:none}",
    "  #flow .fl-eq .t{display:none}",
    "  #flow .fl-tw{margin:0 -12px;padding:0 12px}",
    "  #flow .fl-pick b{font-size:18px}",
    "  #flow .fl-body{padding-bottom:78px}",
    "  #flAct{display:flex;position:fixed;left:0;right:0;bottom:0;z-index:205;gap:8px;",
    "    padding:8px 12px calc(8px + env(safe-area-inset-bottom,0px));background:#fff;",
    "    border-top:1px solid var(--line)}",
    "  #flAct button{flex:1;border:1px solid var(--line);background:#fff;border-radius:7px;",
    "    padding:9px 0;font-size:13px;color:var(--ink)}",
    "  #flAct button.primary{background:var(--ink);border-color:var(--ink);color:#fff}",
    "  #flAct button[disabled]{color:#c2c7cf;border-color:var(--line2)}",
    "  #flowBar{gap:6px;bottom:10px;padding:5px}",
    "  #flowBar .fb-mid span{display:none}",
    "  #flowBar .fb-prog{display:none}",
    "  #flowBar button{padding:5px 10px;font-size:11.5px}",
    "}"
  ].join("\n");

  /* ── 骨架 ─────────────────────────────────────────────────── */
  var root, body, nav, bar, progText, progSub, progNum, ring;

  function build(){
    var st = document.createElement("style");
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);

    root = document.createElement("div");
    root.id = "flow";
    root.innerHTML =
      '<div class="fl-hd">' +
        '<div class="fl-hd-l"><span class="fl-logo">内</span>' +
          '<div><b>信用评级管理系统</b><i>内评简评 · 全流程演示</i></div></div>' +
        '<nav class="fl-nav" id="flNav"></nav>' +
        '<div class="fl-step" id="flStep">' +
          '<div class="fl-ring-w"><svg class="fl-ring" viewBox="0 0 36 36" aria-hidden="true">' +
            '<circle class="rbg" cx="18" cy="18" r="15"></circle>' +
            '<circle class="rfg" id="flRing" cx="18" cy="18" r="15" ' +
              'stroke-dasharray="94.25" stroke-dashoffset="75.4"></circle>' +
          '</svg><b id="flStepN">1</b></div>' +
          '<div class="fl-ring-t"><b id="flStepB">第 1 步 / 共 5 步</b>' +
            '<span id="flStepS">任务列表 · 已完成 20%</span></div>' +
        "</div>" +
        '<div class="fl-hd-r"><span class="fl-badge">演示数据</span>' +
          '<button class="fl-x" data-act="workbench">回到工作台</button></div>' +
      '</div>' +
      '<div class="fl-body" id="flBody"></div>' +
      '<div id="flAct"></div>';
    document.body.appendChild(root);

    bar = document.createElement("div");
    bar.id = "flowBar";
    document.body.appendChild(bar);

    body = root.querySelector("#flBody");
    nav = root.querySelector("#flNav");
    progText = root.querySelector("#flStepB");
    progNum = root.querySelector("#flStepN");
    progSub = root.querySelector("#flStepS");
    ring = root.querySelector("#flRing");

    root.addEventListener("click", onClick);
    body.addEventListener("input", onInput);
    body.addEventListener("change", onChange);
    bar.addEventListener("click", onClick);
  }

  var STEP_NAMES = ["任务列表", "报告编辑", "级别认定", "债项评级", "提交审批"];

  function paintNav(){
    var html = "";
    STEP_NAMES.forEach(function(n, i){
      var k = i + 1;
      var on = k === S.step;
      var done = !on && k < S.max;
      var cls = on ? "on" : (done ? "done" : "");
      var dis = k > S.max ? " disabled" : "";
      html += '<button class="' + cls + '"' + dis + ' data-act="go" data-n="' + k + '">' +
              "<b>" + (done ? "✓" : k) + "</b><span>" + n + "</span></button>";
      if(k < 5) html += "<i>›</i>";
    });
    nav.innerHTML = html;
  }

  /* 进度：顶栏那根细线 + 右上角的圈。第 2 步在工作台那边，靠 #flowBar 的格子。 */
  function paintProg(){
    var p = S.step / 5;
    root.style.setProperty("--flp", (p * 100).toFixed(1) + "%");
    if(progText) progText.textContent = "第 " + S.step + " 步 / 共 5 步";
    if(progNum) progNum.textContent = S.step;
    if(progSub) progSub.textContent = STEP_NAMES[S.step - 1] + " · 已完成 " + Math.round(p * 100) + "%";
    if(ring) ring.setAttribute("stroke-dashoffset", (94.25 * (1 - p)).toFixed(2));
  }

  function paintBar(){
    var dots = "";
    for(var k = 1; k <= 5; k++) dots += '<i class="' + (k <= 2 ? "on" : "") + '"></i>';
    bar.innerHTML =
      '<button data-act="go" data-n="1">← 任务列表</button>' +
      '<div class="fb-prog">' + dots + "</div>" +
      '<div class="fb-mid"><b>第 2 步 / 共 5 步</b>' +
        '<span>内评简评报告 · ' + esc(CO.name) + "</span></div>" +
      '<button class="primary" data-act="go" data-n="3">下一步：级别认定 →</button>';
  }

  /* ── 第 1 步：任务列表 ─────────────────────────────────────── */
  /* 第 1 行是这一轮演示的主人公：走完审批，它的状态就从「处理中」变成「已完成」 */
  function effStatus(t, i){
    if(i === 0 && S.status === "done") return "已完成";
    return t.status;
  }

  function taskRows(){
    var kw = S.kw.trim();
    return TASKS.map(function(t, i){
      var status = effStatus(t, i);
      if(S.tab !== "全部" && status !== S.tab) return "";
      if(kw && t.name.indexOf(kw) < 0) return "";
      var tg = status === "处理中" ? "run" : status === "待处理" ? "wait"
             : status === "已完成" ? "live" : "done";
      var lv = (i === 0 && S.status !== "draft") ? S.level : t.level;
      return '<tr data-i="' + i + '">' +
        '<td><input type="checkbox" disabled></td>' +
        "<td><b style=\"font-weight:500\">" + esc(t.name) + "</b></td>" +
        '<td><span class="tg">' + t.tag + "</span></td>" +
        "<td>" + t.type + "</td>" +
        "<td>" + t.industry + "</td>" +
        "<td>" + t.analyst + "</td>" +
        '<td><span class="tg ' + tg + '">' + status + "</span></td>" +
        "<td>" + t.model + "</td>" +
        '<td class="num">' + t.due + "</td>" +
        '<td class="num">' + levelWord(lv) + "</td>" +
        '<td><button class="fl-op" data-act="open" data-i="' + i + '">' +
          (i === 0 ? (S.status === "done" ? "查看" : "处理") : "处理") + "</button></td>" +
      "</tr>";
    }).join("");
  }

  function pageTasks(){
    var counts = { "全部": TASKS.length };
    TASKS.forEach(function(t, i){
      var s = effStatus(t, i);
      counts[s] = (counts[s] || 0) + 1;
    });
    var tabs = ["全部", "待处理", "处理中", "已提交"].map(function(t){
      return '<button class="' + (S.tab === t ? "on" : "") + '" data-act="tab" data-v="' + t + '">' +
             t + " " + (counts[t] || 0) + "</button>";
    }).join("");
    var run = counts["处理中"] || 0;
    var wait = counts["待处理"] || 0;

    return '<div class="fl-page">' +
      '<div class="fl-ph"><div><h1>评级任务</h1>' +
        "<p>标债评级任务池。点「处理」进入内评简评报告，之后依次完成模型级别认定、债项评级与审批提交。</p>" +
      "</div></div>" +
      '<div class="fl-kpis">' +
        "<div><b>" + wait + "</b><span>待处理</span></div>" +
        "<div><b>" + run + "</b><span>处理中</span></div>" +
        "<div><b>" + (12 + (S.status === "done" ? 1 : 0)) + "</b><span>本月已完成</span></div>" +
        "<div><b>1</b><span>30 天内到期</span></div>" +
      "</div>" +
      '<div class="fl-card">' +
        '<div class="fl-tools"><div class="fl-tabs">' + tabs + "</div>" +
          '<div class="fl-search"><input id="flKw" placeholder="搜发行人名称" value="' + esc(S.kw) + '"></div></div>' +
        '<div class="fl-tw"><table class="fl-task"><thead><tr>' +
          '<th style="width:28px"><input type="checkbox" disabled></th>' +
          "<th>发行人 / 主体名称</th><th>标签</th><th>评级类型</th><th>所属行业</th>" +
          "<th>分析师</th><th>任务状态</th><th>评级模型</th><th>到期日</th><th>主体级别</th>" +
          '<th style="width:64px">操作</th>' +
        "</tr></thead><tbody>" + taskRows() + "</tbody></table></div>" +
      "</div>" +
      '<div class="fl-card"><h3>这条流程会走完什么<i>按真实业务顺序串起来</i></h3>' +
        '<div class="fl-thru">' +
          "<div><b><em>1</em>任务列表</b>从标债任务池取出待评企业</div>" +
          "<div><b><em>2</em>报告编辑</b>读材料、生成并逐段改正文</div>" +
          "<div><b><em>3</em>级别认定</b>给主体一个 1A 至 DDD 的级别</div>" +
          "<div><b><em>4</em>债项评级</b>逐只债券定级，可高于主体</div>" +
          "<div><b><em>5</em>提交审批</b>审核同意后归档并出意见书</div>" +
        "</div>" +
      "</div>" +
    "</div>";
  }

  /* ── 第 3 步：级别认定 ─────────────────────────────────────── */
  /* 偏离提示按「大档 / 小档」说：4C→4B 是一个小档，4C→2A 是两个大档。
     按格子数说会变成「上调 10 档」，看着像算错了。 */
  function devText(){
    if(!S.level) return { t: "先选一个级别", c: "warn" };
    if(S.level === "都不评") return { t: "本次不作评级", c: "warn" };
    var i = lvIndex(S.level), m = lvIndex(MODEL_LEVEL);
    if(i < 0) return { t: "", c: "" };
    if(i === m) return { t: "与模型测算一致", c: "" };
    var dir = i < m ? "上调" : "下调";
    var big = Math.floor(i / 4) - Math.floor(m / 4);
    var tail = "，需在认定理由中说明";
    if(big === 0) return { t: "较模型" + dir + " 1 个小档" + tail, c: "warn" };
    return { t: "较模型" + dir + " " + Math.abs(big) + " 个大档" + tail, c: "warn" };
  }

  /* 模型打分明细。加权合计要能和 40.92 对上，所以分数是倒推的：
     48.20×20% + 52.40×25% + 58.80×15% = 31.56
     26.00×15% + 24.00×15% + 18.60×10% =  9.36  →  合计 40.92，对应 4C */
  var SCORE = [
    { g: "财务风险", w: 60, sub: [
      { n: "盈利能力", w: 20, s: 48.20, v: "营业总收入 30.04 亿元，净利润 0.55 亿元" },
      { n: "偿债能力", w: 25, s: 52.40, v: "总资本化比率 63.03%，EBITDA 利息保障 2.66 倍" },
      { n: "经营规模", w: 15, s: 58.80, v: "总资产 412.65 亿元，净资产 158.32 亿元" }
    ]},
    { g: "非财务风险", w: 40, sub: [
      { n: "区域环境", w: 15, s: 26.00, v: "一般公共预算收入 316.8 亿元，区域债务率偏高" },
      { n: "股东与支持", w: 15, s: 24.00, v: "市国资委全资持股，上年财政补贴 1.86 亿元" },
      { n: "治理与信息质量", w: 10, s: 18.60, v: "标准无保留意见，未发现重大会计差错" }
    ]}
  ];

  function scoreTable(){
    var rows = "";
    SCORE.forEach(function(g){
      var wt = g.sub.reduce(function(a, r){ return a + r.w * r.s / 100; }, 0);
      rows += '<tr class="fl-grp"><td colspan="2"><b>' + g.g + "</b> · 权重 " + g.w + "%</td>" +
              "<td>" + g.sub.length + ' 项指标</td><td class="num">加权 ' + wt.toFixed(2) +
              "</td></tr>";
      g.sub.forEach(function(r){
        rows += "<tr><td>" + r.n + "</td>" +
          '<td class="num">' + r.w + "%</td>" +
          '<td class="fl-v">' + r.v + "</td>" +
          '<td class="num"><span class="fl-sc"><i><em style="width:' + r.s + '%"></em></i>' +
            "<b>" + r.s.toFixed(2) + "</b></span></td></tr>";
      });
    });
    return '<div class="fl-tw"><table class="fl-score"><thead><tr><th>评分指标</th>' +
      '<th class="num">权重</th><th>指标表现</th><th class="num">得分</th></tr></thead><tbody>' +
      rows +
      '<tr class="fl-total"><td><b>加权合计</b></td><td class="num">100%</td>' +
        "<td>对应模型级别 <b>" + MODEL_LEVEL + "</b></td>" +
        '<td class="num"><b>' + MODEL_SCORE.toFixed(2) + "</b></td></tr>" +
      "</tbody></table></div>";
  }

  /* 级别在刻度上的位置：36 格平均铺开，DDD 与「都不评」压到最右 */
  function ratioOf(v){
    if(!v || v === "都不评" || v === "DDD") return 1;
    var i = lvIndex(v);
    return i < 0 ? 0 : i / (LEVELS.length - 1);
  }
  /* 两端各留 26px：标记是以中心定位的，不留余量时选到 1A 文字会顶出卡片 */
  function at(r){ return "calc(26px + (100% - 52px)*" + r.toFixed(4) + ")"; }

  function scaleBar(){
    var ticks = "";
    for(var i = 0; i < LEVELS.length; i += 4){
      ticks += '<div class="fl-tk" style="left:' + at(i / (LEVELS.length - 1)) + '"></div>';
    }
    return '<div class="fl-scale">' + ticks + '<div class="fl-track"></div>' +
      '<div class="fl-mk mdl" style="left:' + at(ratioOf(MODEL_LEVEL)) + '">' +
        "<b>模型 " + MODEL_LEVEL + "</b><i></i></div>" +
      '<div class="fl-mk cur" style="left:' + at(ratioOf(S.level)) + '">' +
        "<i></i><b>" + (S.level === "都不评" ? "不评级"
          : (S.level === MODEL_LEVEL ? "本次一致" : "本次 " + S.level)) + "</b></div>" +
      "</div>" +
      '<div class="fl-axes"><span>1A 最优</span><span>9D 最差 · DDD 违约</span></div>';
  }

  /* 改级别不整页重绘：重绘会把刻度那根指针直接挪到位，看不到它滑过去。
     只改这几处随级别变的地方，指针的 transition 才起作用。 */
  function applyLevel(){
    body.querySelectorAll(".fl-lv button").forEach(function(x){
      x.classList.toggle("on", x.getAttribute("data-v") === S.level);
    });
    var pick = body.querySelector(".fl-pick b");
    if(pick) pick.textContent = levelWord(S.level);
    var dev = body.querySelector(".fl-pick .fl-dev");
    if(dev){
      var d = devText();
      dev.textContent = d.t;
      dev.className = "fl-dev " + d.c;
    }
    var mk = body.querySelector(".fl-mk.cur");
    if(mk){
      mk.style.left = at(ratioOf(S.level));
      var lb = mk.querySelector("b");
      if(lb) lb.textContent = S.level === "都不评" ? "不评级"
        : (S.level === MODEL_LEVEL ? "本次一致" : "本次 " + S.level);
    }
    var big = body.querySelector(".fl-side .fl-kv b.big");
    if(big) big.textContent = levelWord(S.level);
    var ao = body.querySelector("[data-adjout]");
    if(ao) ao.textContent = adjLevel();
  }

  function pageLevel(){
    var tiles = LEVELS.map(function(v){
      return '<button class="' + (S.level === v ? "on" : "") + '" data-act="lv" data-v="' + v + '">' +
             v + "</button>";
    }).join("") +
    '<button class="sp' + (S.level === "DDD" ? " on" : "") + '" data-act="lv" data-v="DDD">' +
      "DDD<i>违约</i></button>" +
    '<button class="sp' + (S.level === "都不评" ? " on" : "") + '" data-act="lv" data-v="都不评">' +
      "都不评</button>";
    var dev = devText();
    var wt = CO.fin * 0.6, wtn = CO.nonfin * 0.4;

    return '<div class="fl-page">' +
      '<div class="fl-ph"><div><h1>模型级别认定</h1>' +
        "<p>先看模型打分，再决定是否定性调整，最后在级别底稿上落一个级别。</p></div></div>" +

      '<div class="fl-meta">' +
        "<span><em>发行人</em><b>" + esc(CO.name) + "</b></span>" +
        "<span><em>任务编号</em><b>" + CO.task + "</b></span>" +
        "<span><em>评级模型</em><b>" + CO.model + "</b></span>" +
        "<span><em>财报期</em><b>" + CO.year + " 年报</b></span>" +
        "<span><em>分析师</em><b>" + CO.analyst + "</b></span>" +
      "</div>" +

      '<div class="fl-grid"><div class="fl-main">' +
        '<section class="fl-card"><h3>模型打分<i>内评模型 v4.2 · 合并口径</i>' +
          '<span class="r">六项指标 · 满分 100</span></h3>' +
          '<div class="fl-eq">' +
            "<em>财务</em><b>" + CO.fin.toFixed(2) + '</b><span class="ar">×</span><span>60%</span>' +
            '<span class="ar">＋</span>' +
            "<em>非财务</em><b>" + CO.nonfin.toFixed(2) + '</b><span class="ar">×</span><span>40%</span>' +
            '<span class="ar">＝</span><b data-count="' + MODEL_SCORE + '">0.00</b>' +
            '<span class="ar">→</span><span class="lv">' + MODEL_LEVEL + "</span>" +
            '<span class="t">加权 ' + wt.toFixed(2) + " ＋ " + wtn.toFixed(2) + "</span>" +
          "</div>" +
          scoreTable() +
        "</section>" +

        '<section class="fl-card"><h3>定性调整底稿<i>模型之外，人还能动一档</i></h3>' +
          '<table><thead><tr><th>调整项</th><th class="num">调整</th><th>依据</th></tr></thead><tbody>' +
            '<tr><td>区域重要性</td><td class="num">0</td><td>主城区唯一市级城投平台，但区域财力一般</td></tr>' +
            '<tr><td>政府支持</td><td class="num">0</td><td>上年收到财政补贴 1.86 亿元，与前期基本持平</td></tr>' +
            '<tr><td>治理与信息质量</td><td class="num">0</td><td>审计意见为标准无保留，未发现重大会计差错</td></tr>' +
            '<tr class="fl-sum"><td><b>调整后级别</b></td>' +
              '<td class="num"><b data-adjout>' + adjLevel() + "</b></td>" +
              "<td>调整分合计 " + (S.adj > 0 ? "+" : "") + S.adj + "</td></tr>" +
          "</tbody></table>" +
          '<div class="fl-bar" style="margin-top:10px">' +
            ADJ.map(function(a){
              return '<button class="fl-btn' + (S.adj === a.v ? " primary" : "") +
                     '" data-act="adj" data-v="' + a.v + '">' + a.t + "</button>";
            }).join("") +
          "</div>" +
        "</section>" +

        '<section class="fl-card"><h3>选择模型级别<i>1A 最好，9D 最差，DDD 为违约</i></h3>' +
          scaleBar() +
          '<div class="fl-lv" style="margin-top:12px">' + tiles + "</div>" +

          '<div class="fl-pick">本次认定 <b>' + levelWord(S.level) + "</b>" +
            '<span class="fl-dev ' + dev.c + '">' + dev.t + "</span></div>" +
          '<p class="fl-hint">点一格就落定，上面那根指针会跟着走。</p>' +
          '<textarea id="flReason" data-field="reason" rows="3" ' +
            'placeholder="认定理由：与模型不一致时写明依据（区域支持、资产质量、治理等）">' +
            esc(S.reason) + "</textarea>" +
        "</section>" +
      "</div>" +

      '<aside class="fl-side">' +
        '<div class="fl-card"><h3>认定摘要</h3>' +
          '<div class="fl-kv"><span>发行人</span><b>' + esc(CO.name) + "</b></div>" +
          '<div class="fl-kv"><span>评级模型</span><b>' + CO.model + "</b></div>" +
          '<div class="fl-kv"><span>模型级别</span><b>' + MODEL_LEVEL + "</b></div>" +
          '<div class="fl-kv"><span>外部评级</span><b>' + CO.ext.level + "（" + CO.ext.org + "）</b></div>" +
          '<div class="fl-kv"><span>本次认定</span><b class="big">' + levelWord(S.level) + "</b></div>" +
          '<div class="fl-kv"><span>评级展望</span><b>稳定</b></div>' +
        "</div>" +
        '<div class="fl-card"><h3>正面与关注<i>摘自报告</i></h3>' +
          '<div style="font-size:11.5px;color:var(--muted);margin:0 0 4px">正面</div>' +
          '<ul class="fl-list">' +
            "<li>区域专营地位较强，土地指标业务为全市唯一实施主体</li>" +
            "<li>公共交通亏损由市财政按成本规制全额补贴</li>" +
            "<li>银行授信未使用额度充足，备用流动性尚可</li>" +
          "</ul>" +
          '<div style="font-size:11.5px;color:var(--muted);margin:9px 0 4px">关注</div>' +
          '<ul class="fl-list">' +
            "<li>收入高度依赖园区开发及土地转让，回款依赖财政结算</li>" +
            "<li>有息债务持续增长，非受限货币资金不足以覆盖短期债务</li>" +
            "<li>2026 年上半年由盈转亏，对外担保与未决诉讼形成或有压力</li>" +
          "</ul>" +
        "</div>" +
        '<div class="fl-card"><h3>操作</h3>' +
          '<div class="fl-bar">' +
            '<button class="fl-btn" data-act="go" data-n="2">上一步</button>' +
            '<button class="fl-btn primary" data-act="next" data-n="4">下一步：债项评级</button>' +
          "</div>" +
          '<p class="fl-hint">级别选定后进入债项评级；债项级别默认与主体一致。</p>' +
        "</div>" +
      "</aside></div>" +
    "</div>";
  }

  /* ── 第 4 步：债项评级 ─────────────────────────────────────── */
  function pageBonds(){
    var rows = CO.bonds.map(function(b, i){
      var own = S.bonds[i].level;
      var eff = bondLevel(i);
      var idx = lvIndex(eff), midx = lvIndex(S.level);
      var hi = own && idx >= 0 && midx >= 0 && idx < midx;
      return "<tr>" +
        '<td class="num">' + b.code + "</td>" +
        "<td><b style=\"font-weight:500\">" + b.short + "</b></td>" +
        "<td>" + b.type + "</td>" +
        '<td class="num">' + b.amount + "</td>" +
        '<td class="num">' + b.due + "</td>" +
        "<td>" + b.gtype + (b.gman !== "—" ? "<br><span style=\"color:var(--muted);font-size:11px\">" +
          b.gman + "</span>" : "") + "</td>" +
        "<td>" +
          '<select class="fl-sel" data-i="' + i + '" data-field="bond">' +
            '<option value=""' + (own ? "" : " selected") + ">同主体（" + levelWord(S.level) + "）</option>" +
            LEVELS.map(function(l){
              return '<option value="' + l + '"' + (own === l ? " selected" : "") + ">" + l + "</option>";
            }).join("") +
          "</select>" +
          (hi ? '<span class="fl-warn">高于主体 ' + (midx - idx) +
                " 档：需说明增信安排</span>" : "") +
        "</td>" +
      "</tr>";
    }).join("");

    var ownLevels = CO.bonds.map(function(b, i){ return bondWord(i); });
    var summary = ownLevels.join(" / ");

    return '<div class="fl-page">' +
      '<div class="fl-ph"><div><h1>债项评级</h1>' +
        "<p>" + esc(CO.name) + " · 主体级别 <b>" + levelWord(S.level) +
        "</b> 已认定 · 存续债券 " + CO.bonds.length + " 只</p></div></div>" +
      '<div class="fl-grid"><div class="fl-main">' +
        '<section class="fl-card"><h3>存续债券<i>级别默认与主体一致，可逐只调整</i></h3>' +
          '<div class="fl-tw"><table><thead><tr><th class="num">债券代码</th><th>债券简称</th>' +
            "<th>类型</th><th class=\"num\">余额（亿元）</th><th class=\"num\">到期日</th>" +
            "<th>担保</th><th>债项级别</th></tr></thead><tbody>" + rows + "</tbody></table></div>" +
          '<p class="fl-hint">债项级别一般不高于主体级别；由第三方提供全额担保的债券可在主体级别基础上上调，' +
            "并须在评级意见中写明增信安排与实际代偿能力。</p>" +
        "</section>" +
        '<section class="fl-card"><h3>债项评级意见<i>选填</i></h3>' +
          '<textarea id="flBondOp" data-field="bondOpinion" rows="3" ' +
            'placeholder="两只债券清偿顺序一致、无特殊条款时可不填；上调需写明担保方与代偿能力">' +
            esc(S.bondOpinion) + "</textarea>" +
        "</section>" +
      "</div>" +
      '<aside class="fl-side">' +
        '<div class="fl-card"><h3>评级结果</h3>' +
          '<div class="fl-kv"><span>主体级别</span><b class="big">' + levelWord(S.level) + "</b></div>" +
          '<div class="fl-kv"><span>债项级别</span><b>' + summary + "</b></div>" +
          '<div class="fl-kv"><span>评级展望</span><b>稳定</b></div>' +
          '<div class="fl-kv"><span>评级类型</span><b>主体 + 债项</b></div>' +
        "</div>" +
        '<div class="fl-card"><h3>操作</h3>' +
          '<div class="fl-bar">' +
            '<button class="fl-btn" data-act="go" data-n="3">上一步</button>' +
            '<button class="fl-btn primary" data-act="next" data-n="5">下一步：提交审批</button>' +
          "</div>" +
          '<p class="fl-hint">下一步汇总主体与债项级别，填评级意见后提交审核。</p>' +
        "</div>" +
      "</aside></div>" +
    "</div>";
  }

  /* ── 第 5 步：提交审批 ─────────────────────────────────────── */
  function defaultOpinion(){
    return "公司是中山市城市建设投资管理服务运营商，收入以园区开发及土地转让为主，" +
      "2025 年营业总收入 30.04 亿元、净利润 0.55 亿元，2026 年上半年营业总收入 4.08 亿元、净利润 -1.64 亿元。\n" +
      "正面看，公司土地指标业务为全市唯一实施主体，公共交通业务由市财政按成本规制全额补贴，" +
      "银行授信未使用额度对到期债务接续形成支撑。\n" +
      "关注的是，园区开发及土地转让的回款依赖市土地储备中心与镇级财政结算，存货以土地开发成本为主，" +
      "有息债务持续增长、总资本化比率升至 63.03%，另有对外担保与未决诉讼形成的或有压力。\n" +
      "综合考虑区域专营地位与自身经营、财务表现，本次给予主体 " + levelWord(S.level) +
      "、债项 " + bondWord(0) + " 的级别，评级展望稳定。";
  }

  function timeline(){
    var st = S.status;
    var n1 = st === "draft" ? "on" : "done";
    var n2 = st === "review" ? "on" : (st === "done" ? "done" : "");
    var n3 = st === "done" ? "done" : "";
    return '<div class="fl-flow">' +
      '<div class="fl-n ' + n1 + '"><b>提交评级结论</b>' +
        "<span>" + CO.analyst + " · 分析师" + (st === "draft" ? " · 当前" : " · " + (S.at || "")) + "</span></div>" +
      '<div class="fl-n ' + n2 + '"><b>审核</b>' +
        "<span>" + CO.reviewer + " · " + CO.reviewerRole +
        (st === "review" ? " · 待处理" : st === "done" ? " · 已同意" : " · 未开始") + "</span></div>" +
      '<div class="fl-n ' + n3 + '"><b>归档并生成意见书</b><span>系统 · ' +
        (st === "done" ? "已完成" : "未开始") + "</span></div>" +
    "</div>";
  }

  function summaryTable(){
    var rows = CO.bonds.map(function(b, i){
      return "<tr><td>" + b.short + "</td><td>" + b.type + "</td>" +
        '<td class="num">' + b.amount + "</td>" +
        '<td class="num"><b>' + bondWord(i) + "</b></td></tr>";
    }).join("");
    return '<table><tbody>' +
      '<tr><td style="width:96px;color:var(--muted)">发行人</td><td><b>' + esc(CO.name) + "</b></td>" +
        '<td style="width:96px;color:var(--muted)">任务编号</td><td class="num">' + CO.task + "</td></tr>" +
      '<tr><td style="color:var(--muted)">主体级别</td><td><b>' + levelWord(S.level) + "</b>" +
        '（模型 ' + MODEL_LEVEL + (S.adj ? "，定性调整 " + (S.adj > 0 ? "+" : "") + S.adj : "，未作定性调整") + "）</td>" +
        '<td style="color:var(--muted)">外部评级</td><td class="num">' + CO.ext.level + "（" + CO.ext.org + "）</td></tr>" +
      '<tr><td style="color:var(--muted)">评级展望</td><td>稳定</td>' +
        '<td style="color:var(--muted)">评级有效期</td><td class="num">2026-11-10 至 2027-11-09</td></tr>' +
    "</tbody></table>" +
    '<table style="margin-top:10px"><thead><tr><th>债券简称</th><th>类型</th>' +
      '<th class="num">余额（亿元）</th><th class="num">债项级别</th></tr></thead><tbody>' +
      rows + "</tbody></table>";
  }

  function pageSubmit(){
    if(S.status === "done"){
      return '<div class="fl-page"><div class="fl-done">' +
        '<div class="fl-ck">✓</div>' +
        "<h2>评级已完成</h2>" +
        "<p>" + esc(CO.name) + " 的主体与债项评级已经审核通过，已归档到评级档案。</p>" +
        '<div class="fl-kv"><span>主体级别</span><b class="big">' + levelWord(S.level) + "</b></div>" +
        '<div class="fl-kv"><span>债项级别</span><b>' +
          CO.bonds.map(function(b, i){ return bondWord(i); }).join(" / ") + "</b></div>" +
        '<div class="fl-kv"><span>审核人</span><b>' + CO.reviewer + " · " + CO.reviewerRole + "</b></div>" +
        '<div class="fl-kv"><span>完成时间</span><b>' + (S.at || nowText()) + "</b></div>" +
        '<div class="fl-bar" style="justify-content:center;margin-top:20px">' +
          '<button class="fl-btn" data-act="reset">再看一遍流程</button>' +
          '<button class="fl-btn primary" data-act="go" data-n="1">回到任务列表</button>' +
        "</div>" +
      "</div></div>";
    }

    var review = S.status === "review";
    var head = '<div class="fl-ph"><div><h1>' + (review ? "评级提交 · 审核" : "评级提交") + "</h1>" +
      "<p>" + esc(CO.name) + " · 主体 " + levelWord(S.level) + " · 债项 " +
      CO.bonds.map(function(b, i){ return bondWord(i); }).join(" / ") + "</p></div></div>";

    var banner = review
      ? '<div class="fl-sumb"><b>已提交</b><span>' + (S.at || nowText()) +
        " 由 " + CO.analyst + " 提交，等待 " + CO.reviewer + " 审核。演示里你可以直接扮演审核人。</span></div>"
      : (S.status === "returned"
        ? '<div class="fl-sumb bad"><b>已退回</b><span>审核意见：' + esc(S.reviewerNote) +
          "。修改级别或意见后可重新提交。</span></div>"
        : "");

    var opinionBox = review
      ? '<div class="fl-card"><h3>评级意见<i>已提交，不可修改</i></h3>' +
        '<div style="white-space:pre-wrap;font-size:12.5px;line-height:1.85;color:var(--ink2)">' +
          esc(S.opinion) + "</div></div>" +
        '<div class="fl-card"><h3>审批意见</h3>' +
          '<textarea id="flRv" data-field="reviewerNote" rows="3" ' +
            'placeholder="同意可直接通过；退回请写明需要补充或修改的地方">' +
            esc(S.reviewerNote) + "</textarea>" +
          '<div class="fl-bar" style="margin-top:12px">' +
            '<button class="fl-btn" data-act="reject">退回修改</button>' +
            '<button class="fl-btn primary" data-act="approve">同意，完成评级</button>' +
          "</div>" +
          '<p class="fl-hint">演示里这一步由你直接扮演 ' + CO.reviewer + "（" + CO.reviewerRole + "）。</p>" +
        "</div>"
      : '<div class="fl-card"><h3>评级意见<i>会写入评级意见书</i></h3>' +
        '<textarea id="flOp" data-field="opinion" rows="8">' +
          esc(S.opinion || defaultOpinion()) + "</textarea>" +
        '<div class="fl-bar" style="margin-top:12px">' +
          '<button class="fl-btn" data-act="go" data-n="4">上一步</button>' +
          '<button class="fl-btn primary" data-act="submit">提交审批</button>' +
        "</div></div>";

    return '<div class="fl-page">' + head + banner +
      '<div class="fl-grid"><div class="fl-main">' +
        '<section class="fl-card"><h3>评级结论<i>提交后进入审核</i></h3>' + summaryTable() + "</section>" +
        opinionBox +
      "</div><aside class=\"fl-side\">" +
        '<div class="fl-card"><h3>审批流</h3>' + timeline() + "</div>" +
        '<div class="fl-card"><h3>提交前检查</h3>' +
          '<div class="fl-chk"><s>✓</s><span>报告正文已生成并复核（5,534 字）</span></div>' +
          '<div class="fl-chk"><s>✓</s><span>级别认定' +
            (S.adj ? "已作定性调整并说明" : "与模型一致，未作定性调整") + "</span></div>" +
          '<div class="fl-chk"><s>✓</s><span>债项级别不高于主体级别</span></div>' +
          '<div class="fl-chk"><s>✓</s><span>评级意见已填写</span></div>' +
        "</div>" +
      "</aside></div></div>";
  }

  /* ── 渲染 ─────────────────────────────────────────────────── */
  /* 算式里那个总分从 0 滚到 40.92，比直接甩一个数出来更像刚算完 */
  function countUp(){
    body.querySelectorAll("[data-count]").forEach(function(el){
      var to = +el.getAttribute("data-count"), t0 = null;
      function step(ts){
        if(t0 === null) t0 = ts;
        var p = Math.min(1, (ts - t0) / 620);
        el.textContent = (to * (1 - Math.pow(1 - p, 3))).toFixed(2);
        if(p < 1) requestAnimationFrame(step);
      }
      requestAnimationFrame(step);
    });
  }

  function paintBody(keepScroll){
    var html = "";
    if(S.step === 1) html = pageTasks();
    else if(S.step === 3) html = pageLevel();
    else if(S.step === 4) html = pageBonds();
    else if(S.step === 5) html = pageSubmit();
    var top = keepScroll ? body.scrollTop : 0;
    body.innerHTML = html;
    var pg = body.querySelector(".fl-page");
    if(pg){
      // 换页才滑；同页重绘（选级别、切标签）滑一次会像闪屏
      if(keepScroll) pg.classList.add("quiet");
      else pg.classList.add(S.dir < 0 ? "dirL" : "dirR");
    }
    body.scrollTop = top;
    if(keepScroll){
      body.querySelectorAll("[data-count]").forEach(function(el){
        el.textContent = (+el.getAttribute("data-count")).toFixed(2);
      });
    }else{
      countUp();
    }
  }

  /* 窄屏底部操作条：长表单里「下一步」在页面最下面，手机上要滑很久才够得到。
     宽屏不显示（那边按钮就在右侧卡片里，一屏之内）。 */
  function paintAct(){
    var act = document.getElementById("flAct");
    if(!act) return;
    var html = "";
    if(S.step === 3){
      html = '<button data-act="go" data-n="2">上一步</button>' +
             '<button class="primary" data-act="next" data-n="4">下一步：债项评级</button>';
    }else if(S.step === 4){
      html = '<button data-act="go" data-n="3">上一步</button>' +
             '<button class="primary" data-act="next" data-n="5">下一步：提交审批</button>';
    }else if(S.step === 5 && S.status === "draft"){
      html = '<button data-act="go" data-n="4">上一步</button>' +
             '<button class="primary" data-act="submit">提交审批</button>';
    }else if(S.step === 5 && S.status === "review"){
      html = '<button data-act="reject">退回修改</button>' +
             '<button class="primary" data-act="approve">同意，完成评级</button>';
    }
    act.innerHTML = html;
  }

  function paint(){
    paintNav();
    paintProg();
    paintBody();
    paintAct();
  }

  function showFlow(){
    root.style.display = "";
    document.body.classList.add("flow-open");
    document.body.classList.remove("flow-bar");
    bar.classList.remove("show");
  }

  function hideFlow(){
    root.style.display = "none";
    document.body.classList.remove("flow-open");
    document.body.classList.add("flow-bar");
    bar.classList.add("show");
  }

  /* 新开一轮：级别、债项、意见、审批状态全部回到起点。
     走完审批再点「处理」就是在同一家上再走一遍，不该接着上一轮的结论。 */
  function resetRound(){
    S.level = MODEL_LEVEL; S.adj = 0; S.reason = "";
    S.bonds = CO.bonds.map(function(){ return { level: "", note: "" }; });
    S.bondOpinion = ""; S.opinion = ""; S.reviewerNote = ""; S.at = "";
    S.status = "draft";
  }

  function go(n){
    if(n < 1 || n > 5) return;
    S.dir = n < S.step ? -1 : 1;
    S.step = n;
    if(n > S.max) S.max = n;
    if(n === 2){
      S.max = Math.max(S.max, 2);
      hideFlow();
      // 工作台是第 2 步的正文：先把示例铺上，再露出来，免得露出一张空白台
      try{
        if(typeof DEMO !== "undefined" && !DEMO.on) demoEnter();
        if(typeof demoStay === "function") demoStay();
      }catch(e){ /* 铺不上也只是空工作台，不拦流程 */ }
      paintBar();
      paintNav();
      paintProg();
      return;
    }
    showFlow();
    paint();
  }

  /* ── 交互 ─────────────────────────────────────────────────── */
  function onClick(e){
    var t = e.target;
    if(!t || !t.closest) return;
    var b = t.closest("[data-act]");
    if(!b) return;
    var act = b.getAttribute("data-act");

    if(act === "go"){ go(+b.getAttribute("data-n")); return; }

    if(act === "workbench"){
      go(2);
      return;
    }

    if(act === "open"){
      var i = +b.getAttribute("data-i");
      if(!TASKS[i].live){
        toastMsg("演示只预置了中山城建一家的材料与报告；其余任务点开是空壳，就不铺了");
        return;
      }
      if(S.status === "done") resetRound();   // 完成的再点开就是新的一轮
      go(2);
      return;
    }

    if(act === "tab"){ S.tab = b.getAttribute("data-v"); paintBody(true); return; }

    if(act === "lv"){
      S.level = b.getAttribute("data-v");
      applyLevel();
      return;
    }

    if(act === "adj"){
      S.adj = +b.getAttribute("data-v");
      paintBody(true);
      return;
    }

    if(act === "next"){
      var n = +b.getAttribute("data-n");
      if(n === 4 && S.level === "都不评"){ go(5); return; }
      go(n);
      return;
    }

    if(act === "submit"){
      if(!S.level){ toastMsg("先选一个认定级别"); return; }
      // 与模型偏离两档以上却不写理由，实务上过不了审核，这里也拦一下
      if(lvIndex(S.level) >= 0 && Math.abs(lvIndex(S.level) - lvIndex(MODEL_LEVEL)) >= 2 && !S.reason.trim()){
        toastMsg("认定级别与模型相差 2 档以上，请先填写认定理由");
        return;
      }
      if(!S.opinion.trim()) S.opinion = defaultOpinion();
      S.status = "review";
      S.at = nowText();
      paint();
      toastMsg("已提交，等待 " + CO.reviewer + " 审核");
      return;
    }

    if(act === "approve"){
      S.status = "done";
      S.at = nowText();
      paint();
      return;
    }

    if(act === "reject"){
      if(!S.reviewerNote.trim()){
        toastMsg("退回要写明原因，审核人得知道改哪儿");
        return;
      }
      S.status = "returned";
      S.step = 5;
      paint();
      return;
    }

    if(act === "reset"){
      S.step = 1; S.max = 1; S.tab = "全部"; S.kw = "";
      resetRound();
      paint();
    }
  }

  function onInput(e){
    var t = e.target;
    if(!t || !t.getAttribute) return;
    var f = t.getAttribute("data-field");
    if(!f) return;
    if(f === "reason") S.reason = t.value;
    else if(f === "opinion") S.opinion = t.value;
    else if(f === "bondOpinion") S.bondOpinion = t.value;
    else if(f === "reviewerNote") S.reviewerNote = t.value;
    else if(f === "kw"){ S.kw = t.value; }
  }

  function onChange(e){
    var t = e.target;
    if(!t || !t.getAttribute) return;
    var f = t.getAttribute("data-field");
    if(f === "bond"){
      var i = +t.getAttribute("data-i");
      S.bonds[i].level = t.value;
      paintBody(true);
    }
  }

  /* 任务列表的搜索框：边打边筛，但不能整页重绘（会丢焦点），
     所以只重画表格那一段 */
  function bindSearch(){
    body.addEventListener("input", function(e){
      var t = e.target;
      if(t && t.id === "flKw"){
        S.kw = t.value;
        var tb = body.querySelector("tbody");
        if(tb){ tb.innerHTML = taskRows(); }
      }
    }, true);
  }

  /* ── 启动 ─────────────────────────────────────────────────── */
  function boot(){
    build();
    bindSearch();
    showFlow();
    paint();
  }

  if(document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", boot);
  }else{
    boot();
  }

  window.FLOW = {
    go: go,
    state: S,
    subject: CO,
    levels: LEVELS
  };
})();
