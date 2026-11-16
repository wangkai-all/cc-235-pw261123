/* 采集工作台前端（原生 JS，无依赖） */
"use strict";

let TOKEN = localStorage.getItem("wb_token") || null;
let STATE = null;
let SELECTED = localStorage.getItem("wb_selected") || null;
let EVENTS = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STATUS_CN = {
  queued: "排队中", running: "后台执行中", takeover: "人工接管中",
  needs_human: "等待人工接管", done: "已完成", failed: "失败",
};
const FIELD_CN = { org: "组织", period: "账期", caliber: "口径", note: "补充说明" };

function orgName(id) {
  const o = (STATE?.meta.orgs || []).find((x) => x.id === id);
  return o ? o.name : (id || "—");
}
function caliberName(id) {
  const c = (STATE?.meta.calibers || []).find((x) => x.id === id);
  return c ? c.name : (id || "—");
}
function condsText(c) {
  if (!c) return "—";
  let t = `${orgName(c.org)} / ${c.period || "—"} / ${caliberName(c.caliber)}`;
  if (c.note) t += ` / 说明:「${c.note}」`;
  return t;
}
function ts(t) { return t ? new Date(t * 1000).toLocaleTimeString("zh-CN") : "—"; }

/* ---------------- API ---------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: {
      "Content-Type": "application/json",
      ...(TOKEN ? { Authorization: "Bearer " + TOKEN } : {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* 文件下载等 */ }
  if (!res.ok) {
    const err = (data && data.error) || { code: "HTTP_" + res.status, message: res.statusText };
    const e = new Error(err.message); e.code = err.code; e.extra = err; e.status = res.status;
    throw e;
  }
  return data;
}

function toast(msg, kind = "info") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "show " + kind;
  setTimeout(() => { el.className = ""; }, 3600);
}

async function refresh() {
  if (!TOKEN) return;
  try {
    STATE = await api("/api/state");
    render();
  } catch (e) {
    if (e.code === "NOT_LOGGED_IN") { logout(); }
  }
}

/* ---------------- 登录 ---------------- */

async function login() {
  try {
    const d = await api("/api/login", {
      method: "POST",
      body: { user: $("login-user").value, password: $("login-pass").value },
    });
    TOKEN = d.token;
    localStorage.setItem("wb_token", TOKEN);
    $("login-overlay").style.display = "none";
    connectEvents();
    await refresh();
  } catch (e) {
    $("login-err").textContent = e.message;
  }
}
function logout() {
  TOKEN = null;
  localStorage.removeItem("wb_token");
  if (EVENTS) EVENTS.close();
  location.reload();
}

function connectEvents() {
  if (EVENTS) EVENTS.close();
  EVENTS = new EventSource("/api/events");
  EVENTS.onmessage = () => refresh();
}

/* ---------------- 渲染 ---------------- */

function render() {
  if (!STATE) return;
  $("who").innerHTML = `当前用户：<b>${esc(STATE.user)}</b>`;
  $("chaos-receipt").checked = !!STATE.chaos.lose_receipt;
  $("chaos-notify").checked = !!STATE.chaos.lose_notify;
  renderCreateForm();
  renderTaskList();
  renderDetail();
  renderPortal();
}

function renderCreateForm() {
  const fill = (id, items, label) => {
    const el = $(id);
    if (el.options.length) return;
    for (const it of items) {
      const o = document.createElement("option");
      o.value = it.id || it; o.textContent = it.name || it;
      el.appendChild(o);
    }
  };
  fill("new-org", STATE.meta.orgs);
  fill("new-period", STATE.meta.periods);
  fill("new-caliber", STATE.meta.calibers);
}

function renderTaskList() {
  const el = $("task-list");
  el.innerHTML = "";
  for (const t of STATE.tasks) {
    const div = document.createElement("div");
    div.className = "task-item" + (t.id === SELECTED ? " selected" : "");
    const holder = t.lock.owner === "bot-01" ? "🤖 后台" : `👤 ${t.lock.owner}`;
    div.innerHTML = `
      <div class="ti-head"><b>${esc(t.name)}</b>
        <span class="badge st-${t.status}">${STATUS_CN[t.status] || t.status}</span></div>
      <div class="ti-sub">${esc(condsText(t.current))}</div>
      <div class="ti-sub muted">控制权：${esc(holder)} · 令牌 #${t.lock.token}</div>`;
    div.onclick = () => { SELECTED = t.id; localStorage.setItem("wb_selected", t.id); render(); };
    el.appendChild(div);
  }
  if (!STATE.tasks.length) el.innerHTML = '<p class="muted">暂无任务</p>';
}

function task() { return STATE.tasks.find((t) => t.id === SELECTED); }
function iHold(t) { return t.lock.owner === STATE.user; }

function renderDetail() {
  const t = task();
  $("no-task").style.display = t ? "none" : "block";
  $("task-detail").style.display = t ? "block" : "none";
  if (!t) return;
  renderHead(t); renderConds(t); renderLockBar(t);
  renderPageMirror(t); renderResults(t); renderTimeline(t);
}

function renderHead(t) {
  let stuck = t.stuck_reason
    ? `<div class="stuck">⚠ 卡住原因：${esc(t.stuck_reason)}</div>` : "";
  $("task-head").innerHTML = `
    <h3>${esc(t.name)} <span class="badge st-${t.status}">${STATUS_CN[t.status]}</span></h3>
    <div class="exec-note">${esc(t.executor_note)}</div>
    ${stuck}`;
}

function condCell(label, c, base) {
  const diff = base && c && JSON.stringify([c.org, c.period, c.caliber, c.note || ""])
    !== JSON.stringify([base.org, base.period, base.caliber, base.note || ""]);
  return `<td class="${diff ? "cond-diff" : ""}"><div class="cond-label">${label}</div>
    ${esc(condsText(c))}${diff ? '<div class="diff-tag">≠ 目标</div>' : ""}</td>`;
}

function renderConds(t) {
  const pageConds = t.page ? {
    org: t.page.org, period: t.page.period,
    caliber: t.page.caliber, note: t.page.note,
  } : null;
  $("conds").innerHTML = `
    <h4>条件对照（交接前后归属一目了然）</h4>
    <table class="grid"><tr>
      ${condCell("目标条件（建单）", t.target, null)}
      ${condCell("当前条件（生效）", t.current, t.target)}
      ${condCell("页面条件（门户实时）", pageConds, t.current)}
    </tr></table>
    ${t.page_org_mismatch
      ? '<div class="warn">⚠ 绑定页面当前组织与任务组织不一致——该页面状态不计入任务进度，请调整或交回后由后台纠正。</div>'
      : ""}`;
}

function renderLockBar(t) {
  const holder = t.lock.owner === "bot-01" ? "后台执行器(bot-01)" : t.lock.owner;
  let btns = "";
  if (iHold(t)) {
    btns = `<button class="primary" onclick="doHandback()">交回后台</button>`;
  } else if (t.lock.owner === "bot-01") {
    btns = `<button class="primary" onclick="doTakeover(false)">接管（暂停后台）</button>`;
  } else {
    btns = `<button onclick="doTakeover(false)">请求接管</button>
            <button class="danger" onclick="doTakeover(true)">强制接管</button>`;
  }
  $("lock-bar").innerHTML = `
    <div class="lock-row">
      <span>控制权：<b>${esc(holder)}</b> · 栅栏令牌 #${t.lock.token}</span>
      <span class="btns">${btns}
        <button onclick="doReconcile()">对账</button>
        <button onclick="doRelogin()">重新登录门户</button>
      </span>
    </div>`;
}

function renderPageMirror(t) {
  const p = t.page;
  const editable = iHold(t);
  const dis = editable ? "" : "disabled";
  let rows = "";
  if (p) {
    const opt = (list, cur) => list.map((x) => {
      const v = x.id || x, n = x.name || x;
      return `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(n)}</option>`;
    }).join("");
    rows = `
      <label>组织 <select ${dis} onchange="pageSet('org',this.value)">${opt(STATE.meta.orgs, p.org)}</select></label>
      <label>账期 <select ${dis} onchange="pageSet('period',this.value)">${opt(STATE.meta.periods, p.period)}</select></label>
      <label>口径 <select ${dis} onchange="pageSet('caliber',this.value)">${opt(STATE.meta.calibers, p.caliber)}</select></label>`;
    const needNote = (t.controls || []).some((c) => c.field === "note" && c.required);
    if (needNote || p.note) {
      rows += `<label class="note-field">补充说明${needNote ? '<span class="req">*新增必填</span>' : ""}
        <input ${dis} value="${esc(p.note || "")}" placeholder="门户要求填写"
        onchange="pageSet('note',this.value)"></label>`;
    }
  }
  const job = t.job;
  let jobRow = "";
  if (job) {
    const bar = `<div class="bar"><div style="width:${job.progress}%"></div></div>`;
    jobRow = `<div class="job-row">当前生成：${esc(job.id)} · ${esc(job.status)}
      ${bar} ${job.status === "running" && editable
        ? `<button onclick="doNudge('${esc(job.id)}')">催办</button>` : ""}
      ${job.status === "done" && editable
        ? `<button class="primary" onclick="doDownload('${esc(job.id)}')">下载归档</button>` : ""}
      ${job.error ? `<span class="err">${esc(job.error)}</span>` : ""}</div>`;
  }
  $("page-mirror").innerHTML = `
    <h4>门户页面镜像 ${p ? `<span class="muted">（页面 ${esc(p.id)} · 绑定任务 · 属主 ${esc(p.owner)}）</span>` : ""}</h4>
    ${p ? "" : '<p class="muted">尚未打开绑定页面</p>'}
    <div class="page-controls">${rows}</div>
    ${editable ? `<div class="btns">
        <button class="primary" onclick="doSubmit()">提交生成</button>
        <span class="muted">提交使用幂等键，重复点击/重试不会重复建单</span></div>` : ""}
    ${jobRow}`;
}

function renderResults(t) {
  let rows = (t.results || []).map((r) => `
    <tr class="${r.status}">
      <td>${esc(r.job_id)}</td><td><code>${esc(r.cond_hash)}</code></td>
      <td>${esc(condsText(r.conditions))}</td>
      <td>${r.by === "bot-01" ? "🤖 后台" : "👤 " + esc(r.by)}</td>
      <td>${r.status === "valid" ? "✅ 有效" : "🗂 已被取代"}</td>
      <td>${ts(r.ts)}</td>
      <td>${r.file_id ? `<a href="/api/files/${esc(r.file_id)}?token=${encodeURIComponent(TOKEN)}" target="_blank">下载CSV</a>` : "—"}</td>
    </tr>`).join("");
  $("results").innerHTML = `
    <h4>结果归属（有效结果必须与当前条件指纹一致）</h4>
    <table class="grid"><tr><th>生成单</th><th>条件指纹</th><th>条件</th>
      <th>产生者</th><th>状态</th><th>时间</th><th>文件</th></tr>
      ${rows || '<tr><td colspan="7" class="muted">暂无结果</td></tr>'}</table>`;
}

function renderTimeline(t) {
  const items = STATE.audit.filter((a) => a.task_id === t.id).slice(0, 30);
  $("timeline").innerHTML = `<h4>交接与操作时间线</h4>` + items.map((a) => `
    <div class="audit-row"><span class="muted">${ts(a.ts)}</span>
      <b>${esc(a.actor)}</b> ${esc(a.action)}
      <span class="muted">${esc(a.detail)}</span></div>`).join("");
}

function renderPortal() {
  const p = STATE.portal;
  const sessions = p.sessions.map((s) =>
    `<tr><td>${esc(s.user)}</td><td>${esc(s.kind)}</td><td>${s.expires_in}s</td></tr>`).join("");
  const pages = p.pages.map((pg) =>
    `<tr class="${pg.task_id ? "" : "free-page"}"><td>${esc(pg.id)}</td>
     <td>${pg.task_id ? "绑定 " + esc(pg.task_id) : "🟣 游离（不计入任何任务）"}</td>
     <td>${esc(pg.owner)}</td><td>${esc(condsText(pg))}</td></tr>`).join("");
  const jobs = p.jobs.slice(-8).map((j) =>
    `<tr><td>${esc(j.id)}</td><td>${esc(j.task_id || "—")}</td>
     <td><code>${esc(j.cond_hash)}</code></td><td>${esc(j.status)} ${j.progress}%</td>
     <td>${esc(j.created_by)}</td></tr>`).join("");
  const ntfs = p.notifications.slice(-8).map((n) =>
    `<tr><td>${esc(n.job_id)}</td><td>${esc(n.task_id || "—")}</td>
     <td class="${n.delivered ? "" : "err"}">${n.delivered ? "已送达" : "✗ 已丢失"}</td></tr>`).join("");
  $("portal-content").innerHTML = `
    <div class="portal-cols">
      <div><h5>门户会话</h5><table class="grid">
        <tr><th>用户</th><th>类型</th><th>剩余</th></tr>${sessions || '<tr><td colspan=3 class=muted>无（已过期）</td></tr>'}</table></div>
      <div><h5>页面</h5><table class="grid">
        <tr><th>页面</th><th>绑定</th><th>属主</th><th>条件</th></tr>${pages || '<tr><td colspan=4 class=muted>无</td></tr>'}</table>
        <button onclick="openFreePage()">以我的身份打开游离页面</button>
        <span class="muted">游离页面可随便选别的组织，不会计入任务进度</span></div>
      <div><h5>生成任务</h5><table class="grid">
        <tr><th>job</th><th>任务</th><th>指纹</th><th>状态</th><th>提交者</th></tr>${jobs || '<tr><td colspan=5 class=muted>无</td></tr>'}</table></div>
      <div><h5>下载完成通知</h5><table class="grid">
        <tr><th>job</th><th>任务</th><th>送达</th></tr>${ntfs || '<tr><td colspan=3 class=muted>无</td></tr>'}</table></div>
    </div>`;
}

/* ---------------- 动作 ---------------- */

async function guard(fn) {
  try { await fn(); await refresh(); }
  catch (e) {
    if (e.code === "SESSION_EXPIRED") {
      toast("门户会话已过期，请点击「重新登录门户」", "warn");
    } else if (e.code === "LOCK_HELD") {
      toast(e.message + "（可强制接管）", "warn");
    } else if (e.code === "STALE_TOKEN") {
      toast(e.message, "err"); await refresh();
    } else if (e.code === "NOT_LOGGED_IN") { logout(); }
    else { toast(e.message, "err"); }
  }
}

window.doTakeover = (force) => guard(async () => {
  if (force && !confirm("强制接管会夺取对方控制权，对方后续操作将被拒绝。确认？")) return;
  await api(`/api/tasks/${SELECTED}/takeover`, { method: "POST", body: { force } });
  toast(force ? "已强制接管" : "已接管，后台执行器已暂停", "ok");
});
window.doHandback = () => guard(async () => {
  await api(`/api/tasks/${SELECTED}/handback`, { method: "POST", body: {} });
  toast("已交回后台，执行器将按页面当前条件继续", "ok");
});
window.pageSet = (field, value) => guard(async () => {
  await api(`/api/tasks/${SELECTED}/page`, { method: "POST", body: { field, value } });
});
window.doSubmit = () => guard(async () => {
  await api(`/api/tasks/${SELECTED}/submit`, { method: "POST", body: {} });
  toast("已提交生成", "ok");
});
window.doNudge = (jobId) => guard(async () => {
  await api(`/api/tasks/${SELECTED}/nudge`, { method: "POST", body: { job_id: jobId } });
  toast("已催办", "ok");
});
window.doDownload = (jobId) => guard(async () => {
  await api(`/api/tasks/${SELECTED}/download`, { method: "POST", body: { job_id: jobId } });
  toast("已下载归档", "ok");
});
window.doReconcile = () => guard(async () => {
  await api(`/api/tasks/${SELECTED}/reconcile`, { method: "POST", body: {} });
  toast("已对账", "ok");
});
window.doRelogin = () => guard(async () => {
  await api(`/api/tasks/${SELECTED}/relogin_portal`, { method: "POST", body: {} });
  toast("门户会话已恢复", "ok");
});
window.openFreePage = () => guard(async () => {
  await api("/api/portal/free_page", { method: "POST", body: {} });
  toast("已打开游离页面（见门户透视）", "ok");
});

/* ---------------- 启动 ---------------- */

$("login-btn").onclick = login;
$("login-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
$("logout-btn").onclick = logout;
$("create-btn").onclick = () => guard(async () => {
  const d = await api("/api/tasks", {
    method: "POST",
    body: { org: $("new-org").value, period: $("new-period").value,
            caliber: $("new-caliber").value },
  });
  SELECTED = d.task_id; localStorage.setItem("wb_selected", SELECTED);
  toast("任务已创建，后台执行器开始工作", "ok");
});
$("chaos-receipt").onchange = (e) => guard(async () => {
  await api("/api/chaos", { method: "POST", body: { key: "lose_receipt", value: e.target.checked } });
  toast("故障注入：提交回执丢失 " + (e.target.checked ? "开" : "关"), "warn");
});
$("chaos-notify").onchange = (e) => guard(async () => {
  await api("/api/chaos", { method: "POST", body: { key: "lose_notify", value: e.target.checked } });
  toast("故障注入：下载通知丢失 " + (e.target.checked ? "开" : "关"), "warn");
});
$("chaos-expire").onclick = () => guard(async () => {
  await api("/api/chaos/expire_sessions", { method: "POST", body: {} });
  toast("已使全部门户会话过期", "warn");
});

(async function boot() {
  if (TOKEN) {
    try {
      STATE = await api("/api/state");
      $("login-overlay").style.display = "none";
      connectEvents();
      render();
      return;
    } catch (e) { /* token 失效，走登录 */ }
  }
  $("login-overlay").style.display = "flex";
})();
