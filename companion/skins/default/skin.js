/* 皮肤公共脚本 (skins/default/skin.js)：所有皮肤页面都可以 <script src="/theme/skin.js"> 引它。
 * 角色卡自己的 theme/pages/skin.js 存在时会被优先使用，所以别的皮肤也可以整份换掉。
 *
 *   Skin.api("now")                → GET /api/v1/now（自动带 token，失败抛错）
 *   Skin.api("debug", {turn: id})  → 带查询参数
 *   Skin.post("/admin/backup")     → 写操作（自动带 X-Admin-Token，要 JSON 回包）
 *   Skin.link("/memory")           → 站内链接带上 token；页面里写 <a data-page="/memory"> 会被自动补好
 *   Skin.every(30000, fn)          → 立刻跑一次，之后定时跑；页面在后台时暂停
 *   Skin.esc / Skin.hm / Skin.ago / Skin.money …
 */
(function () {
  "use strict";
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";

  function url(path, query) {
    var u = new URL(path, location.origin);
    if (token) u.searchParams.set("token", token);
    Object.keys(query || {}).forEach(function (k) {
      var v = query[k];
      if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
    });
    return u.pathname + u.search;
  }

  function api(name, query) {
    return fetch(url("/api/v1/" + name, query), { headers: { Accept: "application/json" } }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }

  function post(path, body) {
    var headers = { "Content-Type": "application/json", Accept: "application/json" };
    if (token) headers["X-Admin-Token"] = token;
    return fetch(path, { method: "POST", headers: headers, body: JSON.stringify(body || {}) }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    });
  }

  function linkify(root) {
    (root || document).querySelectorAll("a[data-page]").forEach(function (a) {
      a.href = url(a.getAttribute("data-page"));
      if (a.getAttribute("data-page") === location.pathname) a.setAttribute("aria-current", "page");
    });
  }

  function every(ms, fn) {
    var busy = false;
    function tick() {
      if (busy || document.hidden) return;
      busy = true;
      Promise.resolve().then(fn).catch(function (e) { console.warn(e); }).then(function () { busy = false; });
    }
    tick();
    document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
    return setInterval(tick, ms);
  }

  function esc(s) {
    return String(s === undefined || s === null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function parse(iso) {
    if (!iso) return null;
    var d = new Date(String(iso).replace(" ", "T"));
    return isNaN(d) ? null : d;
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function hm(iso) { var d = parse(iso); return d ? pad(d.getHours()) + ":" + pad(d.getMinutes()) : ""; }
  function ago(iso) {
    var d = parse(iso);
    if (!d) return "";
    var m = Math.round((Date.now() - d.getTime()) / 60000);
    if (m < 1) return "刚刚";
    if (m < 60) return m + " 分钟前";
    var h = Math.floor(m / 60);
    if (h < 24) return h + " 小时前";
    var days = Math.floor(h / 24);
    return days + " 天" + (h % 24 ? " " + (h % 24) + " 小时" : "") + "前";
  }
  function quiet(hours) {
    if (hours === null || hours === undefined) return "";
    var h = Math.floor(hours), d = Math.floor(h / 24);
    if (h < 1) return "刚刚";
    return (d ? d + " 天 " : "") + (h % 24 ? (h % 24) + " 小时" : "");
  }
  function money(x, digits) { return "¥" + Number(x || 0).toFixed(digits === undefined ? 2 : digits); }
  function num(x) { return Number(x || 0).toLocaleString("en-US"); }

  window.Skin = {
    token: token, url: url, api: api, post: post, link: function (p) { return url(p); },
    linkify: linkify, every: every, esc: esc, parse: parse, hm: hm, ago: ago, quiet: quiet,
    money: money, num: num, pad: pad,
  };
  document.addEventListener("DOMContentLoaded", function () { linkify(document); });
})();
