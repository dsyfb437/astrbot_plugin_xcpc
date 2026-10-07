/* 账号绑定页的前端。
 *
 * 与 AstrBot Dashboard 通信只有一条路：window.AstrBotPluginPage bridge。
 * 页面被装在沙箱 iframe 里，**不能直接 fetch 后端** —— 必须走 bridge，
 * 由 Dashboard 转发到插件用 context.register_web_api() 注册的路由。
 *
 * 这一层刻意做得薄：只负责收集输入、调 bridge、把结果画出来。
 * 任何"这个平台该怎么登录"的知识都在后端（core/accounts.py），不在这里。
 */

(function () {
  "use strict";

  /* ---------------------------------------------------------------- bridge
   *
   * 真实 API（对照官方文档 + 参考插件核实过，**不要凭印象改**）：
   *
   *   bridge.ready()                  -> 上下文 {pluginName, pageName, locale, i18n, ...}
   *   bridge.apiGet(path, query?)     -> Promise<any>
   *   bridge.apiPost(path, body?)     -> Promise<any>
   *   bridge.subscribeSSE(path, cb)   -> Promise<id>
   *   bridge.unsubscribeSSE(id)
   *
   * 两个容易踩的点：
   *   1. `path` 是**相对插件**的（"accounts/status"），
   *      不是完整路由（不是 "/astrbot_plugin_xcpc/accounts/status"）。
   *   2. 页面必须先加载 `/api/plugin/page/bridge-sdk.js`，否则这里拿到 undefined。
   *
   * 我第一版凭印象写成了 `bridge.request({path, method, body})` —— 那是错的，
   * 页面会直接不可用。所以下面显式检查，宁可给一句人话错误也不要静默失败。
   */

  var bridge = window.AstrBotPluginPage;
  var BRIDGE_MISSING =
    "拿不到 AstrBotPluginPage —— 这个页面必须通过 AstrBot 的 WebUI 打开。" +
    "如果确实是从 WebUI 进来的，检查 pages/accounts/index.html 有没有加载 bridge-sdk.js。";
  var NOT_EMBEDDED =
    "这个页面是从浏览器标签直接打开的，请求送不回 AstrBot（bridge 靠 postMessage " +
    "跟父窗口说话）。请在 AstrBot 的 WebUI 里打开这个插件页面。";

  /* 一次请求最多等多久。
   *
   * AstrBot 的 bridge SDK（plugin_page_bridge.js）里**一个超时都没有**：
   * 它把 promise 按 requestId 塞进 pendingRequests，只有收到父窗口的回应才
   * 会落地。父窗口那边一旦没回（iframe 被重建、消息丢了、后端卡住），这个
   * promise **永远不会落地** —— 页面上就是按钮一直灰着、"正在验证…"一直转，
   * 既不报错也不结束。
   *
   * 所以这一层必须自己掐表。宁可给一句"等了 15 秒没回应"，也不要让人对着
   * 一个转圈的界面猜发生了什么。
   */
  var TIMEOUT_MS = 15000;

  /* 登录接口要连对面站点（Codeforces / QOJ），慢是正常的，给宽一点。
     别把"对面本来就慢"报成"卡死"。 */
  var TIMEOUT_SLOW_MS = 60000;

  function timeoutFor(path) {
    return /(^|\/)login(\/|$)/.test(path) ? TIMEOUT_SLOW_MS : TIMEOUT_MS;
  }

  /** 给 bridge 的 promise 掐表；超时了就把话说清楚，不装死。 */
  function withTimeout(p, path) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var ms = timeoutFor(path);
      var timer = setTimeout(function () {
        if (done) { return; }
        done = true;
        var e = new Error(
          "等了 " + (ms / 1000) + " 秒，AstrBot 没有回应（" + path + "）。\n"
          + "去插件的 data/logs/xcpc.log 看有没有 web.req " + path + "：\n"
          + "  有 → 请求到了，是后端没答完（注意 web.route_slow / web.route_fail）；\n"
          + "  没有 → 请求根本没送到，问题在浏览器这一侧。");
        e.timeout = true;
        reject(e);
      }, ms);
      var settle = function (fn) {
        return function (v) {
          if (done) { return; }
          done = true;
          clearTimeout(timer);
          fn(v);
        };
      };
      Promise.resolve(p).then(settle(resolve), settle(reject));
    });
  }

  function call(path, method, body) {
    if (!bridge || typeof bridge.apiGet !== "function") {
      return Promise.reject(new Error(BRIDGE_MISSING));
    }
    if (window.parent === window) {
      // 没有父窗口 → postMessage 发给自己的 window，永远等不到回应
      return Promise.reject(new Error(NOT_EMBEDDED));
    }
    var p = (method === "POST") ? bridge.apiPost(path, body || {})
                                : bridge.apiGet(path, body || undefined);
    return withTimeout(p, path).then(function (data) {
      if (data && data.error) {
        var err = new Error(data.error);
        // 标记出来，好让调用方把"还没关联"和"真出错了"分开说
        err.needLink = !!(data && data.need_link);
        throw err;
      }
      return data;
    });
  }

  /* 路由前缀。
   *
   * ⚠️ **必须拼上。** bridge 只会加**插件名**前缀
   * （`accounts/status` → `/astrbot_plugin_xcpc/accounts/status`），
   * **不会**自动加这一层。
   *
   * 我重构出 callGet/callPost 的时候把 BASE 丢掉了，结果页面调的是
   * `login` 而不是 `accounts/login`，后端根本没有
   * `/astrbot_plugin_xcpc/login` 这条路由 —— 一装就白屏，
   * 而且不一定有明显报错。
   *
   * 这个 bug 是 `test_pages.py` 的「页面路径 vs 后端路由」交叉检查抓到的：
   * 它发现页面调的路径**一条都没对上**后端注册的路由。
   */
  var BASE = "accounts";

  /* 所有请求都带上令牌，并拼上前缀 */
  function callGet(path, query) {
    return call(BASE + "/" + path, "GET", withToken("GET", query));
  }
  function callPost(path, body) {
    return call(BASE + "/" + path, "POST", withToken("POST", body));
  }

  /* ------------------------------------------------------------- 网页令牌
   *
   * 网页本身不知道你是谁 —— AstrBot 的插件页面 token 只绑到「插件+页面」，
   * **不带用户身份**（读 AstrBot 源码确认的）。所以流程是：
   *   1. 在 QQ 里发 /xcpc 绑定 拿到一个 6 位绑定码
   *   2. 在页面上输入它，后端验证后发一个网页令牌
   *   3. 之后每次请求都带上这个令牌
   *
   * 令牌存在 localStorage。它等价于一个 session cookie：
   * 32 字节随机串，猜不到。
   */
  var WT_KEY = "xcpc_web_token";

  function getToken() {
    try { return window.localStorage.getItem(WT_KEY) || ""; } catch (e) { return ""; }
  }
  /**
   * 存令牌，**如实返回成没成功**。
   *
   * AstrBot 把插件页面放进带 sandbox、没有 allow-same-origin 的 iframe 里，
   * 页面处于"不透明源"，`localStorage` 一读一写都抛 SecurityError。
   * 旧版这里把异常吞了就完事，于是界面说"已关联"、实际什么都没记住。
   *
   * 现在真正让关联活下来的是服务端：/link 会把「Dashboard 账号 = QQ 号」
   * 写进数据库，之后每次请求按账号查回来。这里的返回值为假时，
   * 上层会**照实说明**，而不是继续显示"已关联"。
   */
  function setToken(t) {
    try {
      if (t) { window.localStorage.setItem(WT_KEY, t); }
      else { window.localStorage.removeItem(WT_KEY); }
      return true;
    } catch (e) {
      return false;
    }
  }

  /** 把令牌塞进请求体 / 查询串。GET 用查询串，POST 用请求体。 */
  function withToken(method, body) {
    var t = getToken();
    if (!t) { return body; }
    var out = {};
    for (var k in (body || {})) {
      if (Object.prototype.hasOwnProperty.call(body, k)) { out[k] = body[k]; }
    }
    out._wt = t;
    return out;
  }
  var PLATFORMS = [
    {
      id: "codeforces", name: "Codeforces",
      note: "不需要密码，公开 API 只读。填 handle 即可。",
      fields: [{ k: "handle", label: "Handle", ph: "例如 tourist", type: "text" }]
    },
    {
      id: "atcoder", name: "AtCoder",
      note: "不需要密码。提交记录走 AtCoder Problems 公开 API。",
      fields: [{ k: "handle", label: "Handle", ph: "例如 chokudai", type: "text" }]
    },
    {
      id: "qoj", name: "QOJ",
      note: "两条路选一条。填用户名和密码登录（账号开了两步验证的话，"
          + "提交后会再要一次验证码）；**或者**走下面的 Cookie —— "
          + "先在自己的浏览器里登录 QOJ，从开发者工具的 Application → "
          + "Cookies 里把下面几个的名字和值抄过来，这样密码一次都不用"
          + "经过这台服务器。QOJ 没有第三方登录可跳，填 Cookie 就是"
          + "那个\"不用输密码\"的办法。"
          + "只有第一个是必须的，会话就靠它；后面两个登录时会一起下发，"
          + "抄上更耐用，找不到就留空。"
          + "上面那个用户名框填了的话，也能当作用户 ID。",
      fields: [
        { k: "username", label: "用户名", ph: "", type: "text" },
        { k: "password", label: "密码", ph: "", type: "password" },
        { k: "__Host-UOJSESSID",
          label: "Cookie：__Host-UOJSESSID（必填，会话就靠它）",
          ph: "一长串随机字符", type: "textarea", wide: true },
        { k: "uoj_username", label: "Cookie：uoj_username（你的用户名）",
          ph: "就是你的 QOJ 用户名", type: "text" },
        { k: "uoj_remember_token", label: "Cookie：uoj_remember_token",
          ph: "60 个字符，让插件在会话过期后自己恢复登录", type: "text" }
      ]
    },
    {
      id: "luogu", name: "洛谷",
      note: "需要登录。洛谷的自动登录还没打通（被 CDN 的第二层挑战页挡住），"
          + "只能在浏览器里登录洛谷之后，从开发者工具的 Application → "
          + "Cookies 里把下面这两个抄过来，一个框填一个，不用自己拼分号。"
          + "_uid 就是你的用户 ID，它在你的洛谷主页地址里，"
          + "形如 luogu.com.cn/user/123456。"
          + "C3VK 不用填 —— 那个 5 分钟就过期，插件自己会解。",
      fields: [
        { k: "__client_id", label: "Cookie：__client_id（必填，会话就靠它）",
          ph: "一长串随机字符", type: "textarea", wide: true },
        { k: "_uid", label: "Cookie：_uid（你的用户 ID）",
          ph: "例如 123456", type: "text" }
      ]
    }
  ];

  /* ------------------------------------------------------------------ 工具 */

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  var STATUS_TEXT = {
    valid: "已绑定",
    expired: "已过期",
    error: "异常",
    unbound: "未绑定"
  };

  function renderStatus(pf, st) {
    var s = (st && st.status) || "unbound";
    var b = el("span", "badge " + s, STATUS_TEXT[s] || s);
    if (st && st.updated_at) { b.title = "更新于 " + st.updated_at; }
    return b;
  }

  function renderCard(pf, st) {
    var card = el("div", "card");
    var h = el("h2");
    h.appendChild(el("span", null, pf.name));
    h.appendChild(renderStatus(pf, st));
    card.appendChild(h);
    card.appendChild(el("p", "hint", pf.note));

    var row = el("div", "row");
    var inputs = {};

    pf.fields.forEach(function (f) {
      var wrap = el("div", f.wide ? "full" : "grow");
      wrap.appendChild(el("label", null, f.label));
      var inp;
      if (f.type === "textarea") {
        // Cookie 是一长串，用单行输入框看不全也贴不利索
        inp = el("textarea");
        inp.rows = 3;
        inp.spellcheck = false;
      } else {
        inp = el("input");
        inp.type = f.type || "text";
      }
      inp.placeholder = f.ph || "";
      inp.autocomplete = "off";
      // handle 已绑定时预填（后端只回 handle，不回任何凭据）
      if (st && f.k === "handle" && st.handle) { inp.value = st.handle; }
      inputs[f.k] = inp;
      wrap.appendChild(inp);
      row.appendChild(wrap);
    });

    var btn = el("button", null,
                 st && st.status === "valid" ? "重新绑定" : "登录 / 绑定");
    row.appendChild(btn);

    var logoutBtn = null;
    if (st && st.status === "valid") {
      logoutBtn = el("button", "ghost", "解绑");
      row.appendChild(logoutBtn);
    }
    card.appendChild(row);

    // 两步验证的输入框（只有后端说 need_2fa 时才出现）
    var twofaRow = el("div", "row");
    twofaRow.style.display = "none";
    twofaRow.appendChild(el("label", null, "两步验证码"));
    var twofaInput = el("input");
    twofaInput.type = "text";
    twofaInput.autocomplete = "one-time-code";
    twofaInput.placeholder = "6 位数字";
    var twofaWrap = el("div", "grow");
    twofaWrap.appendChild(twofaInput);
    twofaRow.appendChild(twofaWrap);
    var twofaBtn = el("button", null, "提交验证码");
    twofaRow.appendChild(twofaBtn);
    card.appendChild(twofaRow);

    var msg = el("div", "msg");
    card.appendChild(msg);

    var currentSession = null;

    function say(text, cls) {
      msg.textContent = text || "";
      msg.className = "msg" + (cls ? " " + cls : "");
    }

    function doLogin() {
      var body = {};
      for (var k in inputs) {
        if (Object.prototype.hasOwnProperty.call(inputs, k)) {
          body[k] = inputs[k].value.trim();
        }
      }
      // 密码不留在 DOM 里
      if (inputs.password) { inputs.password.value = ""; }

      btn.disabled = true;
      twofaRow.style.display = "none";
      say("正在处理…");
      callPost("login", { platform: pf.id, fields: body })
        .then(function (r) {
          btn.disabled = false;
          if (!r) { say("没有返回", "err"); return; }
          if (r.error) { say(r.error, "err"); return; }

          currentSession = r.session_id;
          if (r.state === "need_2fa" || r.need_2fa) {
            // 后端明确说要两步验证 —— 出输入框，**不假装成功**
            twofaRow.style.display = "";
            twofaInput.focus();
            say(r.message || "需要两步验证，请输入验证码", "");
            return;
          }
          if (r.state === "ok") {
            say(r.message || "完成", "ok");
            load();
            return;
          }
          say(r.message || "失败", "err");
        })
        .catch(function (e) {
          btn.disabled = false;
          say(e.message || String(e), "err");
        });
    }

    btn.onclick = doLogin;

    twofaBtn.onclick = function () {
      var code = twofaInput.value.trim();
      twofaInput.value = "";
      twofaBtn.disabled = true;
      callPost("login/2fa",
           { session_id: currentSession, code: code })
        .then(function (r) {
          twofaBtn.disabled = false;
          if (!r) { say("没有返回", "err"); return; }
          if (r.error) { say(r.error, "err"); return; }
          if (r.state === "ok") {
            twofaRow.style.display = "none";
            say(r.message || "登录成功", "ok");
            load();
          } else {
            say(r.message || "验证码不对", "err");
          }
        })
        .catch(function (e) {
          twofaBtn.disabled = false;
          say(e.message || String(e), "err");
        });
    };

    if (logoutBtn) {
      logoutBtn.onclick = function () {
        if (!window.confirm("解绑 " + pf.name + "？\n\n"
            + "只删登录凭据，**不会删你的做题记录**。")) { return; }
        logoutBtn.disabled = true;
        callPost("logout", { platform: pf.id })
          .then(function (r) {
            logoutBtn.disabled = false;
            say((r && r.ok) ? "已解绑" : ((r && r.error) || "解绑失败"),
                (r && r.ok) ? "ok" : "err");
            load();
          })
          .catch(function (e) {
            logoutBtn.disabled = false;
            say(e.message || String(e), "err");
          });
      };
    }

    return card;
  }

  /* ------------------------------------------------------------------ 主流程 */

  function load() {
    var box = document.getElementById("cards");
    var linkCard = document.getElementById("linkCard");
    callGet("status")
      .then(function (data) {
        box.innerHTML = "";
        // 还没关联 QQ 号 → 只显示关联表单，别显示四个平台卡片
        // （显示了也没用，绑了也落不到你名下）
        if (data && data.need_link) {
          if (linkCard) { linkCard.style.display = ""; }
          var c0 = el("div", "card");
          c0.appendChild(el("div", "msg",
            "先在上面「关联 QQ 号」输入绑定码 —— "
            + "在 QQ 里发 /绑定 就能拿到。\n"
            + "关联成功之后，这里才会出现 Codeforces / AtCoder / QOJ / 洛谷 "
            + "四个平台的填写框（绑在 QQ 号名下，所以得先知道你是谁）。"));
          box.appendChild(c0);
          var sub0 = document.querySelector(".sub");
          if (sub0) { sub0.textContent = "还没有关联 QQ 号。"; }
          return;
        }
        if (linkCard) { linkCard.style.display = "none"; }
        var byPlatform = {};
        ((data && data.platforms) || []).forEach(function (p) {
          byPlatform[p.platform] = p;
        });
        PLATFORMS.forEach(function (pf) {
          box.appendChild(renderCard(pf, byPlatform[pf.id]));
        });
        var who = (data && data.user_id) || "未知";
        var sub = document.querySelector(".sub");
        if (sub) {
          sub.textContent = sub.textContent.split("｜")[0] +
            "｜当前身份：" + who;
        }
      })
      .catch(function (e) {
        box.innerHTML = "";
        var c = el("div", "card");
        c.appendChild(el("div", "msg err", "读状态失败：" + (e.message || e)));
        box.appendChild(c);
      });
  }

  function loadLog() {
    var box = document.getElementById("log");
    // query 走 bridge 的参数位，不要拼进 path（拼进去可能被当成路径的一部分）
    callGet("log", { n: 40 })
      .then(function (data) {
        var lines = (data && data.lines) || [];
        box.textContent = lines.length ? lines.join("\n") : "（暂无）";
        box.scrollTop = box.scrollHeight;
      })
      .catch(function (e) {
        // 「还没关联」不是故障，别拿一坨多行的内部说明吓人
        box.textContent = e && e.needLink
          ? "还没关联 QQ 号 —— 先在上面输入绑定码（在 QQ 里发 /绑定 拿）。"
          : "读日志失败：" + (e.message || e);
      });
  }

  document.getElementById("refreshLog").onclick = loadLog;

  /* ---- 关联 QQ 号 ---------------------------------------------------- */
  var linkBtn = document.getElementById("linkBtn");
  if (linkBtn) {
    linkBtn.onclick = function () {
      var inp = document.getElementById("linkCode");
      var msg = document.getElementById("linkMsg");
      var code = (inp.value || "").trim();
      if (!code) {
        msg.textContent = "先填绑定码（在 QQ 里发 /绑定）";
        msg.className = "msg err";
        return;
      }
      linkBtn.disabled = true;
      msg.textContent = "正在验证…";
      msg.className = "msg";
      call(BASE + "/link", "POST", { code: code })   // 这一步还没有令牌，不带
        .then(function (r) {
          linkBtn.disabled = false;
          if (!r || !r.ok) {
            msg.textContent = (r && r.error) || "关联失败";
            msg.className = "msg err";
            return;
          }
          var kept = setToken(r.web_token);
          inp.value = "";
          if (r.remembered === false && !kept) {
            // 服务端没记住、浏览器也存不住 —— 那就照实说，别显示"已关联"
            msg.textContent = "绑定码验证通过（QQ " + r.user_id + "），但这次关联"
              + "没能记住：服务端拿不到 Dashboard 账号，这个页面又存不住令牌"
              + "（AstrBot 的沙箱 iframe 里 localStorage 不可用）。请把这句话发给我。";
            msg.className = "msg err";
          } else {
            msg.textContent = "已关联到 QQ " + r.user_id;
            msg.className = "msg ok";
          }
          load();
          loadLog();   // 关联前日志面板一直显示"还没关联"，这里顺手刷掉
        })
        .catch(function (e) {
          linkBtn.disabled = false;
          msg.textContent = e.message || String(e);
          msg.className = "msg err";
        });
    };
    // 回车即提交
    var codeInput = document.getElementById("linkCode");
    if (codeInput) {
      codeInput.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") { linkBtn.click(); }
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { load(); loadLog(); });
  } else {
    load(); loadLog();
  }
})();
