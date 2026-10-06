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

  function call(path, method, body) {
    if (!bridge || typeof bridge.apiGet !== "function") {
      return Promise.reject(new Error(BRIDGE_MISSING));
    }
    var p = (method === "POST") ? bridge.apiPost(path, body || {})
                                : bridge.apiGet(path, body || undefined);
    return Promise.resolve(p).then(function (data) {
      if (data && data.error) { throw new Error(data.error); }
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
   *   1. 在 QQ 里发 /绑定 拿到一个 6 位绑定码
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
  function setToken(t) {
    try {
      if (t) { window.localStorage.setItem(WT_KEY, t); }
      else { window.localStorage.removeItem(WT_KEY); }
    } catch (e) { /* 隐私模式下可能写不了，那就每次都要重新关联 */ }
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
      note: "需要登录。若账号开了两步验证，提交后会再要一次验证码。",
      fields: [
        { k: "username", label: "用户名", ph: "", type: "text" },
        { k: "password", label: "密码", ph: "", type: "password" }
      ]
    },
    {
      id: "luogu", name: "洛谷",
      note: "需要登录。站点有 CDN 反爬，自动登录还没打通 —— "
          + "请用「导入 Cookie」：浏览器登录洛谷后，从开发者工具里复制 Cookie 贴进来。",
      fields: [
        { k: "cookies", label: "Cookie", ph: "C3VK=...; __client_id=...",
          type: "text", wide: true }
      ],
      importOnly: true
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
      var wrap = el("div", "grow");
      wrap.appendChild(el("label", null, f.label));
      var inp = el("input");
      inp.type = f.type || "text";
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
            + "在 QQ 里发 /绑定 就能拿到。"));
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
        box.textContent = "读日志失败：" + (e.message || e);
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
          setToken(r.web_token);
          inp.value = "";
          msg.textContent = "已关联到 QQ " + r.user_id;
          msg.className = "msg ok";
          load();
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
