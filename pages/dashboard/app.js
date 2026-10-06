// 宿舍电费监控仪表盘 v1.1.7（只读）
// 数据来源：插件注册的 WebUI API（dashboard/overview、dashboard/history）。
// 本页面不含任何凭证信息，接口侧也只返回 cookie_ok 布尔值。
var B = window.AstrBotPluginPage;
var chart = null, days = 14, timer = null, overview = null;

function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function fmt(v) { return (v != null) ? Number(v).toFixed(2) : "—"; }
function isDark() { return !!(B && B.getContext && B.getContext() && B.getContext().isDark); }

function showError(msg) {
  var er = document.getElementById("error");
  er.textContent = "⚠️ " + msg;
  er.classList.remove("hidden");
  setTimeout(function () { er.classList.add("hidden"); }, 5000);
}

function fmtTime(ts) {
  if (!ts) return "—";
  var d = new Date(ts * 1000);
  return (d.getMonth() + 1) + "/" + d.getDate() + " " +
    String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function muteText(until) {
  if (!until || until <= Date.now() / 1000) return "";
  return "🔕 至 " + fmtTime(until);
}

async function loadOverview() {
  try {
    var r = await B.apiGet("dashboard/overview");
    if (!r || !r.success) throw new Error((r && r.message) || "接口返回异常");
    overview = r.data;
    render();
  } catch (e) {
    showError("数据加载失败：" + (e.message || e));
  }
}

function render() {
  var d = overview;
  var binds = d.bindings || [];

  var low = 0, alerting = 0;
  binds.forEach(function (b) {
    Object.keys(b.fees).forEach(function (k) {
      var f = b.fees[k];
      if (f.latest_value != null && f.latest_value <= f.warn) alerting++;
    });
  });
  var muted = binds.filter(function (b) { return b.alert_muted_until > Date.now() / 1000; }).length;

  var cards = [
    { icon: "🔗", label: "绑定会话", value: binds.length, cls: "" },
    { icon: alerting > 0 ? "🚨" : "✅", label: "低于预警线的费种", value: alerting, cls: alerting > 0 ? "card-alert" : "card-success" },
    { icon: "🔑", label: "学校凭证", value: d.cookie_ok ? "已配置" : "未配置", cls: d.cookie_ok ? "card-success" : "card-alert" },
    { icon: "🔕", label: "预警静音中", value: muted, cls: muted > 0 ? "card-warn" : "" }
  ];
  document.getElementById("cards").innerHTML = cards.map(function (c) {
    return '<div class="card-mini ' + c.cls + '"><div class="card-icon">' + c.icon + '</div>' +
      '<div class="card-value">' + esc(c.value) + '</div><div class="card-label">' + esc(c.label) + '</div></div>';
  }).join("");

  var poll = d.poll_interval_minutes;
  document.getElementById("global-meta").textContent =
    "轮询 " + (poll ? poll + " 分钟" : "已关闭") + " · 每日播报 " + (d.daily_report ? d.daily_time : "已关闭");

  // 会话选择器
  var sel = document.getElementById("session-selector");
  var prev = sel.value;
  sel.innerHTML = binds.length
    ? binds.map(function (b, i) {
        return '<option value="' + esc(b.umo) + '">' + esc(b.label) + "（" + esc(b.umo) + "）</option>";
      }).join("")
    : "";
  if (prev && binds.some(function (b) { return b.umo === prev; })) sel.value = prev;
  if (!sel.dataset.init && binds.length) { sel.dataset.init = "1"; }
  if (!sel.value && binds.length) sel.value = binds[0].umo;

  // 绑定表
  document.getElementById("binding-tbody").innerHTML = binds.length ? binds.map(function (b) {
    var ac = b.fees.ac || {}, el = b.fees.elec || {};
    function feeCell(f) {
      if (f.latest_value == null) return '<span class="muted">暂无数据</span>';
      var low2 = f.latest_value <= f.warn;
      var cls = f.latest_value <= f.critical ? "val-critical" : (low2 ? "val-warn" : "val-ok");
      var custom = f.custom ? ' <span class="tag">自定义线</span>' : "";
      var day = f.per_day > 0 ? '<div class="meta-line">日均 ' + fmt(f.per_day) + " " + esc(f.unit) + "</div>" : "";
      return '<span class="' + cls + '">' + fmt(f.latest_value) + " " + esc(f.unit) + "</span>" + custom + day;
    }
    var warnLine = "空调 " + fmt(ac.warn) + " 度 / " + fmt(ac.critical) + " 度<br>电费 " + fmt(el.warn) + " 元 / " + fmt(el.critical) + " 元";
    var states = [];
    var m1 = muteText(b.alert_muted_until);
    states.push(m1 ? "预警" + m1 : "预警正常");
    var m2 = muteText(b.daily_muted_until);
    states.push(m2 ? "播报" + m2 : "");
    return "<tr>" +
      '<td class="mono">' + esc(b.umo) + "</td>" +
      "<td>" + esc(b.label) + "</td>" +
      "<td>" + feeCell(ac) + "</td>" +
      "<td>" + feeCell(el) + "</td>" +
      '<td class="meta-cell">' + warnLine + "</td>" +
      "<td>" + states.filter(Boolean).map(esc).join("<br>") + "</td>" +
      "</tr>";
  }).join("") : '<tr><td colspan="6" class="muted" style="text-align:center;padding:24px">还没有会话绑定宿舍，去 QQ 里跟机器人说「春雪楼817 电费多少」试试</td></tr>';

  loadChart();
}

async function loadChart() {
  var umo = document.getElementById("session-selector").value;
  var cv = document.getElementById("chart"), empty = document.getElementById("chart-empty");
  if (!umo) { empty.classList.remove("hidden"); cv.style.display = "none"; return; }
  try {
    var r = await B.apiGet("dashboard/history", { umo: umo, days: days });
    if (!r || !r.success) throw new Error((r && r.message) || "接口返回异常");
    renderChart(r.data);
  } catch (e) {
    empty.classList.remove("hidden"); cv.style.display = "none";
  }
}

function renderChart(data) {
  var ce = document.getElementById("chart-empty"), cv = document.getElementById("chart");
  var ac = (data.series && data.series.ac) || [];
  var el = (data.series && data.series.elec) || [];
  if (!ac.length && !el.length) {
    ce.classList.remove("hidden"); cv.style.display = "none";
    if (chart) { chart.destroy(); chart = null; }
    return;
  }
  ce.classList.add("hidden"); cv.style.display = "";
  var dark = isDark();
  var grid = dark ? "rgba(148,163,184,0.08)" : "rgba(100,116,139,0.08)";
  var tick = dark ? "#94a3b8" : "#64748b";
  var labels = (ac.length ? ac : el).map(function (p) { return p.date.slice(5); });

  function ds(name, arr, color, fill) {
    return {
      label: name, data: arr.map(function (p) { return p.value; }),
      borderColor: color, backgroundColor: fill, fill: true, tension: 0.35,
      pointRadius: 0, pointHoverRadius: 5, spanGaps: true, borderWidth: 2
    };
  }
  var datasets = [];
  if (ac.length) datasets.push(ds("空调费（度）", ac, dark ? "#60a5fa" : "#2563eb", dark ? "rgba(96,165,250,0.12)" : "rgba(59,130,246,0.08)"));
  if (el.length) datasets.push(ds("宿舍电费（元）", el, dark ? "#fbbf24" : "#d97706", dark ? "rgba(251,191,36,0.10)" : "rgba(217,119,6,0.07)"));

  if (chart) chart.destroy();
  chart = new Chart(cv, {
    type: "line",
    data: { labels: labels, datasets: datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { labels: { color: tick, usePointStyle: true, padding: 16, font: { size: 11 } } },
        tooltip: { callbacks: { label: function (ctx) { return ctx.dataset.label + " " + (ctx.parsed.y == null ? "无记录" : ctx.parsed.y.toFixed(2)); } } }
      },
      scales: {
        x: { ticks: { color: tick, maxTicksLimit: 10, font: { size: 10 } }, grid: { color: grid } },
        y: { ticks: { color: tick, font: { size: 10 } }, grid: { color: grid } }
      }
    }
  });
}

function applyTheme() {
  document.documentElement.setAttribute("data-theme", isDark() ? "dark" : "light");
  if (chart) { loadChart(); }
}

async function main() {
  await B.ready();
  applyTheme();
  B.onContext(applyTheme);

  document.querySelectorAll(".group-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      document.querySelectorAll(".group-btn").forEach(function (x) { x.classList.remove("active"); });
      btn.classList.add("active");
      days = parseInt(btn.dataset.days, 10);
      loadChart();
    });
  });
  document.getElementById("session-selector").addEventListener("change", loadChart);

  await loadOverview();
  timer = setInterval(loadOverview, 30000);
  window.addEventListener("beforeunload", function () {
    if (timer) clearInterval(timer);
    if (chart) chart.destroy();
  });
}

main();
