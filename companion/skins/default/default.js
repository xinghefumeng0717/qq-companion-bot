/* 默认皮肤的七页：同一个外壳，按 <body data-page> 画不同内容。只用 Skin.* 取数。 */
(function () {
  "use strict";
  var S = window.Skin, E = S.esc;
  var NAV = [["/", "此刻"], ["/memory", "记忆"], ["/debug", "调试"], ["/costs", "费用"], ["/stickers", "表情包"], ["/logs", "日志"], ["/admin", "管理"]];

  function shell(name) {
    document.body.insertAdjacentHTML("afterbegin",
      '<header class="top"><div class="top-in"><span class="brand" id="brand">' + E(name || "看板") + '</span><nav class="nav" aria-label="看板页面">' +
      NAV.map(function (n) { return '<a data-page="' + n[0] + '">' + n[1] + "</a>"; }).join("") + "</nav></div></header>");
    S.linkify(document);
  }
  function fail(e) { document.getElementById("app").innerHTML = '<p class="err">取数失败：' + E(e.message) + "</p>"; }
  function rows(list, fn) { return list.length ? list.map(fn).join("") : '<p class="muted">暂无</p>'; }

  var R = {};
  R.home = function () {
    return S.api("now").then(function (d) {
      document.getElementById("brand").textContent = d.name;
      document.title = d.name + " · 此刻";
      var st = d.stage;
      return '<h1>' + E(d.name) + ' 此刻</h1><div class="grid">' +
        '<section class="card"><h2>她在做什么</h2><p class="big">' + E(d.activity) + '</p><p>' + E(d.mood.label) + ' · ' + E(d.mood.desc) + '</p>' +
        '<p class="muted">上次聊天 ' + E(d.last_chat) + (d.days_known ? ' · 认识第 ' + d.days_known + ' 天' : '') + '</p></section>' +
        '<section class="card"><h2>她心里在想</h2><p>' + E(d.state_note || "（还没有对话）") + '</p></section>' +
        '<section class="card"><h2>关系</h2><p class="big">' + E(st.name) + ' <span class="pill">' + E(st.tone) + '</span></p>' +
        (st.next ? '<div class="bar" aria-label="到「' + E(st.next) + '」的进度"><i style="width:' + Math.round(st.progress * 100) + '%"></i></div><p class="muted">离「' + E(st.next) + '」' + Math.round(st.progress * 100) + '%</p>' : '') + '</section>' +
        '<section class="card"><h2>今天的作息</h2><div class="tl">' + d.routine.map(function (b) {
          return '<div class="row' + (b.current ? " now" : b.past ? " past" : "") + '"><span>' + S.pad(b.start) + ':00</span><span style="flex:1">' + E(b.activity) + "</span></div>";
        }).join("") + '</div></section>' +
        '<section class="card"><h2>约定</h2>' + rows(d.meetups, function (m) { return '<div class="row"><span>' + E(m.when) + ' ' + E(m.time) + '</span><span>' + E(m.place) + ' · ' + E(m.plan) + '</span></div>'; }) + '</section>' +
        '<section class="card"><h2>记得问他</h2>' + rows(d.followups, function (f) { return '<div class="row"><span>' + E(f.topic) + '</span><span class="muted">' + E(f.when) + '</span></div>'; }) + '</section>' +
        '<section class="card"><h2>憋着没说</h2>' + rows(d.desires, function (t) { return '<div class="row"><span>' + E(t) + '</span></div>'; }) + '</section>' +
        '<section class="card"><h2>运行</h2><div class="row"><span>QQ</span><span>' + (d.onebot ? "在线" : "没连上") + '</span></div><div class="row"><span>今日 / 本月</span><span>' + S.money(d.cost.today) + ' / ' + S.money(d.cost.month) + '</span></div><div class="row"><span>最近备份</span><span>' + E(d.backup) + '</span></div></section>' +
        "</div>";
    });
  };
  R.memory = function () {
    return S.api("memory").then(function (d) {
      function layer(title, L) {
        var all = (L.core || []).map(function (x) { return [x, true]; }).concat((L.recent || []).map(function (x) { return [x, false]; }));
        return '<section class="card"><h2>' + title + '</h2>' + rows(all, function (p) {
          return '<div class="row"><span>' + (p[1] ? '<span class="pill">一直记着</span> ' : '') + E(p[0].t) + (p[0].prev ? ' <s class="muted">' + E(p[0].prev) + '</s>' : '') + '</span><span class="muted">' + E(p[0].d) + '</span></div>';
        }) + '</section>';
      }
      return '<h1>记忆</h1><div class="grid">' + layer("关于他", d.about) + layer("她自己说过的", d.self) +
        '<section class="card"><h2>日记</h2>' + rows(d.diaries, function (x) { return '<div class="row" style="opacity:' + x.op + '"><span>' + E(x.t) + '</span><span class="muted">' + E(x.d) + ' · ' + E(x.mood) + '</span></div>'; }) + '</section>' +
        '<section class="card"><h2>见面</h2>' + rows(d.meetups, function (m) { return '<div class="row"><span>' + E(m.place) + ' · ' + E(m.recap || m.plan) + '</span><span class="muted">' + E(m.when) + '</span></div>'; }) + '</section>' +
        '<section class="card"><h2>憋着没说</h2>' + rows(d.unsaid, function (t) { return '<div class="row"><span>' + E(t) + '</span></div>'; }) + '</section></div>';
    });
  };
  R.debug = function () {
    var turn = new URLSearchParams(location.search).get("turn") || "";
    return S.api("debug", { turn: turn }).then(function (d) {
      var t = d.turn;
      var pick = '<label class="muted">轮次 <select id="turnSel">' + d.turns.map(function (x) {
        return '<option value="' + E(x.id) + '"' + (t && x.id === t.id ? " selected" : "") + '>' + E(S.hm(x.at)) + ' · ' + E(x.text.slice(0, 24)) + '</option>';
      }).join("") + '</select></label>';
      if (!t) return '<h1>调试</h1><p class="muted">还没有对话追踪记录。</p>';
      return '<h1>调试</h1>' + pick + '<div class="grid" style="margin-top:16px">' +
        '<section class="card"><h2>读到 · ' + S.num(t.read.chars) + ' 字</h2>' + t.read.sections.map(function (s) { return '<details><summary><span>' + E(s.title) + '</span><span class="muted">' + S.num(s.chars) + '</span></summary><pre>' + E(s.text) + '</pre></details>'; }).join("") + '</section>' +
        '<section class="card"><h2>写下 · ' + E(t.write.model) + '</h2><pre>' + E(t.write.text) + '</pre></section>' +
        '<section class="card"><h2>发出 · ' + E(t.send.status) + '</h2>' + rows(t.send.sent, function (m) { return '<div class="row"><span>' + E(m.channel) + ' · ' + E(m.text) + '</span><span class="muted">#' + E(m.message_id) + '</span></div>'; }) + (t.send.errors.length ? '<p class="err">' + E(t.send.errors.join("；")) + '</p>' : '') + '</section>' +
        '<section class="card"><h2>记下 · ' + (t.check.passed === true ? "通过" : (t.check.passed === false ? "未通过" : "未校验")) + '</h2>' + (d.observer.latest ? '<pre>' + E(JSON.stringify(d.observer.latest, null, 2)) + '</pre>' : '<p class="muted">观察者还没打过分</p>') + '</section></div>';
    }).then(function (html) {
      setTimeout(function () {
        var sel = document.getElementById("turnSel");
        if (sel) sel.addEventListener("change", function () { location.href = S.url("/debug", { turn: sel.value }); });
      });
      return html;
    });
  };
  R.costs = function () {
    return S.api("costs").then(function (d) {
      var max = Math.max.apply(null, d.groups.map(function (g) { return g.cost; }).concat([0.0001]));
      return '<h1>费用</h1><div class="grid">' +
        '<section class="card"><h2>今日</h2><p class="big">' + S.money(d.cost_today) + '</p></section>' +
        '<section class="card"><h2>本月</h2><p class="big">' + S.money(d.cost_month) + '</p></section>' +
        '<section class="card"><h2>累计</h2><p class="big">' + S.money(d.total_cost) + '</p><p class="muted">' + S.num(d.total_calls) + ' 次调用 · 缓存命中 ' + Math.round(d.cache_rate) + '%</p></section></div>' +
        '<div class="grid" style="margin-top:16px"><section class="card"><h2>按用途</h2>' + d.groups.map(function (g) { return '<div class="row"><span style="width:120px">' + E(g.purpose) + '</span><span style="flex:1" class="bar"><i style="width:' + (g.cost / max * 100).toFixed(1) + '%"></i></span><span>' + S.money(g.cost) + '</span></div>'; }).join("") + '</section>' +
        '<section class="card"><h2>最近调用</h2>' + d.details.map(function (r) { return '<div class="row"><span>' + E(S.hm(r.at)) + ' ' + E(r.purpose) + '</span><span class="muted">' + S.num(r.p_tok) + ' / ' + S.num(r.c_tok) + '</span><span>' + S.money(r.cost, 4) + '</span></div>'; }).join("") + '</section></div>';
    });
  };
  R.stickers = function () {
    return S.api("stickers").then(function (d) {
      return '<h1>表情包 <span class="muted">' + d.count + '</span></h1><div class="stk">' + d.items.map(function (s) {
        return '<figure class="card" style="margin:0;padding:10px"><img loading="lazy" src="' + E(s.url) + '" alt="' + E(s.name) + '"><figcaption><strong>' + E(s.name) + '</strong><br><span class="muted">' + E(s.meaning || s.desc) + '</span></figcaption></figure>';
      }).join("") + '</div>';
    });
  };
  R.logs = function () {
    var lv = new URLSearchParams(location.search).get("level") || "";
    return S.api("logs", { level: lv }).then(function (d) {
      var tabs = [["", "全部"], ["info", "信息"], ["warn", "警告"], ["error", "错误"]].map(function (t) {
        return '<a class="btn" style="display:inline-flex;align-items:center;text-decoration:none;' + (t[0] === lv ? "background:var(--soft)" : "") + '" href="' + E(S.url("/logs", { level: t[0] })) + '">' + t[1] + '</a>';
      }).join(" ");
      if (d.missing) return '<h1>日志</h1><p class="muted">还没有日志文件。</p>';
      return '<h1>日志</h1><p>' + tabs + '</p><section class="card">' + d.lines.map(function (l) {
        return '<div class="logline lv-' + E(l.level) + '"><span class="muted">' + E(l.date + " " + l.time) + '</span><span>' + E(l.level) + '</span><span>' + E(l.module) + '</span><span>' + E(l.msg) + '</span></div>';
      }).join("") + '</section>';
    });
  };
  R.admin = function () {
    return S.api("admin").then(function (d) {
      setTimeout(bindAdmin);
      return '<h1>管理</h1><div class="grid">' +
        '<section class="card"><h2>备份</h2><p>最近一次：' + E(d.last_backup) + '</p><button id="bk" type="button">立即备份</button> <span id="bkMsg" class="muted"></span></section>' +
        '<section class="card"><h2>重启</h2><p>已运行 ' + (d.uptime_minutes === null ? "—" : Math.floor(d.uptime_minutes / 60) + " 小时 " + (d.uptime_minutes % 60) + " 分") + '</p><button id="rs" type="button">重启服务</button> <span id="rsMsg" class="muted"></span></section>' +
        '<section class="card"><h2>重置</h2><p>清空聊天、记忆、好感与心情；表情包和花销记录保留。输入 YES 才能点。</p><input id="yes" autocomplete="off" aria-label="输入 YES"> <button id="rz" class="danger" type="button" disabled>重置</button> <span id="rzMsg" class="muted"></span></section></div>';
    });
  };
  function bindAdmin() {
    function act(btn, msg, path, body, ok) {
      document.getElementById(btn).addEventListener("click", function () {
        var m = document.getElementById(msg);
        m.textContent = "进行中…";
        S.post(path, body ? body() : {}).then(function (j) { m.textContent = ok(j); }).catch(function (e) { m.textContent = "失败：" + e.message; });
      });
    }
    act("bk", "bkMsg", "/admin/backup", null, function () { return "完成"; });
    act("rs", "rsMsg", "/admin/restart", null, function (j) { return j.message || "已收到"; });
    var yes = document.getElementById("yes"), rz = document.getElementById("rz");
    yes.addEventListener("input", function () { rz.disabled = yes.value !== "YES"; });
    act("rz", "rzMsg", "/admin/reset", function () { return { confirm: yes.value }; }, function () { return "已重置"; });
  }

  window.DefaultSkin = {
    render: function (page) {
      shell("");
      var app = document.getElementById("app");
      var go = function () { return R[page]().then(function (html) { app.innerHTML = html; S.linkify(app); }).catch(fail); };
      if (page === "home") S.every(30000, go); else go();
      if (page !== "home") S.api("now").then(function (d) { document.getElementById("brand").textContent = d.name; }).catch(function () {});
    },
  };
})();
