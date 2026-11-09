/* 离线演示版的启动垫片。
   make_demo_offline.py 把它内联进 site/demo.html，只有那一份产物会跑；
   app/static/index.html 与线上版不受影响。

   要做三件事：
   1) 掐掉所有 /api 请求——单文件里没有后端，任何一个请求打到
      file:///api/... 都会在控制台留一条红字；
   2) 跳过口令页，直接铺演示那一份；
   3) 收掉在单文件里没有去处的入口（历史、版本、设置、锁定、退出演示）。

   不做的：不改正文、不改渲染、不另画一套界面。离线版就是工作台本身，
   唯一差别是拿不到服务端。 */
(function(){
  window.OFFLINE_DEMO = true;

  /* 有的浏览器（尤其 Safari）对 file:// 直接关掉 localStorage，访问即抛。
     主脚本里 `localStorage.getItem("apiBase")` 不带 try，抛一次整页就白屏。
     这里先探一下，不通就换一份内存版顶上去——单文件不指望持久化。 */
  try{
    window.localStorage.getItem("__offline_probe");
  }catch(e){
    var mem = {};
    try{
      Object.defineProperty(window, "localStorage", {
        configurable: true,
        value: {
          getItem: function(k){ return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null; },
          setItem: function(k, v){ mem[k] = String(v); },
          removeItem: function(k){ delete mem[k]; },
          clear: function(){ mem = {}; },
          key: function(i){ return Object.keys(mem)[i] || null; },
          get length(){ return Object.keys(mem).length; }
        }
      });
    }catch(e2){
      window.localStorage = {
        getItem: function(k){ return Object.prototype.hasOwnProperty.call(mem, k) ? mem[k] : null; },
        setItem: function(k, v){ mem[k] = String(v); },
        removeItem: function(k){ delete mem[k]; },
        clear: function(){ mem = {}; },
        key: function(i){ return Object.keys(mem)[i] || null; },
        length: 0
      };
    }
  }

  /* 不自动播引导，直接落在演示态。
     线上版首次进入会弹引导（TOUR_KEY 为空时），单文件是拿去给别人自己点的，
     一上来一段遮罩反而挡路；想看引导点右上角 ? 里的「重播引导」。
     这个键名与 index.html 的 TOUR_KEY 是同一个字符串，make_demo_offline.py
     会在打包时核对它没改过——改了这里就失效，退回自动播。 */
  try{ localStorage.setItem("nr_tour_v1", "1"); }catch(e){}

  var NO_BACKEND = "离线演示版没有后端：生成、导出 Word、发信都要在线上版或本机做";

  function reply(obj, code){
    return new Response(JSON.stringify(obj), {
      status: code || 200,
      headers: { "Content-Type": "application/json" }
    });
  }

  /* 开机那几个心跳要给一条像样的回答，否则页面上会写「未连上本机服务」。
     其余一律 503 + 一句人话：导出、生成、发信在单文件里确实做不到，
     与其返回空对象让人看到「已导出 undefined」，不如把话说清楚。 */
  function canned(url){
    if(/\/api\/health/.test(url)) return { ok: true };
    if(/\/api\/session/.test(url)) return { authed: true };
    if(/\/api\/models/.test(url))
      return { models: [
        { id: "deepseek-v4.0", name: "deepseek v4.0" },
        { id: "deepseek-v4.1", name: "deepseek v4.1" }
      ], default: "deepseek-v4.1" };
    if(/\/api\/stats/.test(url)) return { total: {}, mcp: {}, money: {} };
    if(/\/api\/versions/.test(url)) return { majors: [], versions: [] };
    if(/\/api\/mail\/config/.test(url)) return { ready: false, default_to: "" };
    return null;
  }

  var rawFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function(u, o){
    var url = (u && u.url) ? u.url : String(u);
    if(url.indexOf("/api/") < 0){
      // 非接口请求照旧（单文件里本来也没有）
      return rawFetch ? rawFetch(u, o) : Promise.reject(new Error(NO_BACKEND));
    }
    var hit = canned(url);
    return Promise.resolve(hit ? reply(hit) : reply({ error: NO_BACKEND }, 503));
  };

  /* 上传走 XHR，拦在 send：直接回一个失败，让调用方的 onerror 走原来的提示 */
  var xSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function(){
    if(this.__offlineApi){
      var xhr = this;
      setTimeout(function(){
        try{
          xhr.status = 0;
          xhr.onerror && xhr.onerror(new Event("error"));
        }catch(e){}
      }, 0);
      return;
    }
    return xSend.apply(this, arguments);
  };
  var xOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(m, url){
    if(String(url || "").indexOf("/api/") >= 0) this.__offlineApi = true;
    return xOpen.apply(this, arguments);
  };

  /* 单文件里没有去处的几个入口：按钮留着只会点出一片空白 */
  var css = document.createElement("style");
  css.textContent =
    "body.offline #usageBtn,body.offline #histBtn,body.offline #verBtn," +
    "body.offline #setBtn,body.offline #lockBtn,body.offline header .sep{display:none}" +
    "body.offline #demoBar .dbtn.exit{display:none}" +
    // 收尾卡与 ? 菜单里各有一项是「回到空白工作台」，单文件没有那个去处
    "body.offline #tourEnd .opt:last-of-type{display:none}" +
    "body.offline #tourMenu [data-act='normal']{display:none}" +
    "@media (max-width:760px){body.offline #demoBar .dbtn{display:inline-flex}}";
  (document.head || document.documentElement).appendChild(css);

  function markOffline(){
    var bar = document.getElementById("demoBar");
    if(!bar || bar.querySelector(".doff")) return;
    var txt = document.createElement("span");
    txt.className = "dtext doff";
    txt.textContent = "离线单文件";
    var sep = document.createElement("span");
    sep.className = "dsep doff";
    sep.textContent = "·";
    var first = bar.querySelector(".dbtn");
    // 顺序是「…可以随便改 · 离线单文件 · 重播」：先插字，再在字前面插那个间隔点
    bar.insertBefore(txt, first);
    bar.insertBefore(sep, txt);
  }

  window.addEventListener("load", function(){
    try{
      document.body.classList.remove("locked");
      var gate = document.getElementById("gate");
      if(gate) gate.classList.remove("show");

      // 说明页是站内另一个文件，单文件版里没有
      var doc = document.getElementById("docLink");
      if(doc){
        doc.setAttribute("href", "javascript:void 0");
        doc.addEventListener("click", function(e){
          e.preventDefault();
          if(window.toast) toast("单文件里只有这一页；说明与方法在线上版的「说明」里");
        });
      }

      // 「退出演示」在单文件里没有下落：退了就是一张空白工作台，回不去
      var de = window.demoExit;
      if(typeof de === "function"){        window.demoExit = function(){
          if(window.toast) toast("离线演示版只有这一份示例，不退出演示");
        };
      }

      /* 导出与打包背后是排版与压缩，没有后端就是没有。
         不拦的话会走到 setStep("export", "error")，左栏那颗「导出报告」
         变成红的——看演示的人会以为这是坏的。拦在动作入口，给一句话。
         按钮都是 onclick="名字()"，覆盖 window 上的同名函数就生效。 */
      function standin(name, msg){
        if(typeof window[name] !== "function") return;
        window[name] = function(){ if(window.toast) toast(msg); };
      }
      standin("exportDocx", "离线演示版不能排 Word：线上版点一下就会下载排版好的 docx");
      standin("runBundle", "离线演示版不能打包：清单可以翻，下载要到线上版");
      // 邮件面板里那颗「去设置里配发件邮箱」在单文件里没有落点
      standin("openSettings", "离线演示版不带设置：SMTP、模型与口令都在线上版里配");

      /* 两处文案要按单文件改口：原文都提到单文件里没有的去处。
         收尾卡那句写着「看完点顶部退出演示」，那颗按钮已收掉；
         最后一步说 ? 里「三个去处」，这里只剩两个。 */
      var stay = document.querySelector("#tourEnd .opt");
      if(stay){
        var it = stay.querySelector("i");
        if(it) it.textContent = "可以翻段、改字、加减条目、拖顺序；不调模型、不发信、不导出。";
      }
      try{
        if(typeof TOUR_STEPS !== "undefined" && TOUR_STEPS.length){
          TOUR_STEPS[TOUR_STEPS.length - 1].body =
            "点右上角 <b>?</b> 有两个去处：<b>重播引导</b>、<b>进入演示模式</b>（自己点这份示例）。← → 翻页，Esc 退出。";
        }
      }catch(e){}

      markOffline();

      /* 直接铺示例。引导那条路已经被上面的 TOUR_KEY 掐掉了，页面不会再来抢，
         所以这里不用等、也不用判 TOUR.on——早铺一步，打开就没有中间那张空白台。 */
      if(typeof demoEnter === "function" && demoEnter()){
        if(typeof demoStay === "function") demoStay();
      }
      markOffline();
    }catch(e){ /* 垫片出问题也不该把页面卡死在口令页 */ }
  });
})();
