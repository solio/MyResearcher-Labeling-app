"use strict";

/* MyResearcher 单人标注工具 — 前端（原生 JS，零依赖） */

const HEADS = [
  { id: "target_mode", name: "目标对象" },
  { id: "stance", name: "方向立场" },
  { id: "emotion_primary", name: "主情绪" },
  { id: "emotion_target", name: "情绪指向" },
  { id: "action_tendency", name: "动作倾向" },
  { id: "context_dependency", name: "上下文依赖" },
  { id: "reasoning_tags", name: "推理依据（多选）" },
];
const TERMINAL_DISPOSITIONS = ["跳过", "无法判断", "缺少上下文"];
const DISPOSITIONS = ["无法判断", "缺少上下文", "跳过", "稍后再看"];
const SESSION_KEY = "mr_labeler_session_v1";

const state = {
  session: null,       // { batchId, head }
  batches: [],
  assignments: [],
  idx: 0,
  details: new Map(),  // assignment_id -> /api/assignment 响应
};

const $ = (sel) => document.querySelector(sel);
const headName = (id) => (HEADS.find(h => h.id === id) || {}).name || id;
const isMultiHead = (head) => head === "reasoning_tags";
const isDone = (a) => !!(a.is_final || TERMINAL_DISPOSITIONS.includes(a.disposition));

/* ---------- 基础设施 ---------- */

let toastTimer = null;
function toast(msg, isErr) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.toggle("err", !!isErr);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 1600);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body;
}

function openSheet(html) {
  $("#sheet-content").innerHTML = html;
  $("#sheet").classList.remove("hidden");
  $("#sheet-overlay").classList.remove("hidden");
}
function closeSheet() {
  $("#sheet").classList.add("hidden");
  $("#sheet-overlay").classList.add("hidden");
}

/* ---------- 会话 ---------- */

function loadSession() {
  try { return JSON.parse(localStorage.getItem(SESSION_KEY)) || null; }
  catch (e) { return null; }
}
function saveSession() {
  localStorage.setItem(SESSION_KEY, JSON.stringify(state.session));
}

/* ---------- 开始屏 ---------- */

function renderStart() {
  $("#screen-main").classList.add("hidden");
  $("#screen-start").classList.remove("hidden");
  const bl = $("#batch-list");
  bl.innerHTML = "";
  if (!state.batches.length) {
    bl.innerHTML = '<p class="muted">暂无 batch：先用 tools/import_batch.py 或 tools/seed_demo.py 导入。</p>';
  }
  for (const b of state.batches) {
    const btn = document.createElement("button");
    btn.className = "pick-item";
    btn.innerHTML = `<span>${b.id}</span><span class="sub">完成 ${b.done || 0} / ${b.total}</span>`;
    if (state.session && state.session.batchId === b.id) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      state.session = { batchId: b.id, head: state.session && state.session.batchId === b.id ? state.session.head : null };
      renderStart();
    });
    bl.appendChild(btn);
  }
  const hl = $("#head-list");
  hl.innerHTML = "";
  for (const h of HEADS) {
    const btn = document.createElement("button");
    btn.className = "pick-item";
    btn.textContent = h.name;
    if (state.session && state.session.head === h.id) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      if (!state.session) state.session = { batchId: null, head: null };
      state.session.head = h.id;
      renderStart();
    });
    hl.appendChild(btn);
  }
  const ready = !!(state.session && state.session.batchId && state.session.head);
  $("#btn-enter").disabled = !ready;
  $("#start-hint").textContent = ready
    ? `将进入 ${state.session.batchId} · ${headName(state.session.head)}（会话内锁定该 head）`
    : "先选择 batch 和 head";
}

/* ---------- 主屏 ---------- */

async function enterMain(batchId, head) {
  state.session = { batchId, head };
  saveSession();
  try {
    state.assignments = await api(`/api/assignments?batch_id=${encodeURIComponent(batchId)}&head=${encodeURIComponent(head)}`);
  } catch (e) {
    toast("加载任务失败: " + e.message, true);
    return;
  }
  if (!state.assignments.length) {
    toast("该 batch/head 没有任何任务", true);
    return;
  }
  state.idx = firstUnfinishedIndex();
  $("#screen-start").classList.add("hidden");
  $("#screen-main").classList.remove("hidden");
  $("#head-name").textContent = headName(head);
  await showAssignment(state.idx);
}

function firstUnfinishedIndex() {
  const i = state.assignments.findIndex((a) => !isDone(a));
  return i >= 0 ? i : 0;
}

async function getDetail(assignmentId) {
  if (!state.details.has(assignmentId)) {
    const d = await api(`/api/assignment?id=${encodeURIComponent(assignmentId)}`);
    state.details.set(assignmentId, d);
  }
  return state.details.get(assignmentId);
}

async function showAssignment(idx) {
  if (idx < 0 || idx >= state.assignments.length) return;
  state.idx = idx;
  const a = state.assignments[idx];
  let detail;
  try {
    detail = await getDetail(a.id);
  } catch (e) {
    toast("加载内容失败: " + e.message, true);
    return;
  }
  $("#card-title").textContent = detail.sample.title || "（无标题）";
  $("#card-content").textContent = detail.sample.content;
  $("#card-sample-id").textContent = detail.sample.id;
  $("#question-text").textContent = detail.glossary ? detail.glossary.question : "";
  renderLabels(detail);
  renderDispositions();
  updateFinalBtn();
  renderProgress();
  $("#btn-prev").disabled = idx === 0;
  $("#btn-next").disabled = idx === state.assignments.length - 1;
}

function hasAnswer(a) {
  return isMultiHead(state.session.head)
    ? Array.isArray(a.answer) && a.answer.length > 0
    : typeof a.answer === "string" && a.answer.length > 0;
}

function updateFinalBtn() {
  const a = state.assignments[state.idx];
  const show = a && !isDone(a) && hasAnswer(a);
  $("#btn-final").classList.toggle("hidden", !show);
}

function renderDispositions() {
  const wrap = $("#disposition-row");
  wrap.innerHTML = "";
  const a = state.assignments[state.idx];
  for (const d of DISPOSITIONS) {
    const btn = document.createElement("button");
    btn.className = "disp-btn";
    btn.textContent = d;
    if (a.disposition === d) btn.classList.add("selected");
    btn.addEventListener("click", () => onDispositionClick(d));
    wrap.appendChild(btn);
  }
}

function nextUnfinishedIndex(fromIdx) {
  const n = state.assignments.length;
  for (let i = fromIdx + 1; i < n; i++) if (!isDone(state.assignments[i])) return i;
  for (let i = 0; i <= fromIdx && i < n; i++) if (!isDone(state.assignments[i])) return i;
  return null;
}

async function finalizeCurrent() {
  const a = state.assignments[state.idx];
  if (!hasAnswer(a)) return;
  const ok = await saveAnnotation({ answer: a.answer, disposition: a.disposition, is_final: true });
  if (!ok) return;
  toast("已完成本条");
  const nxt = nextUnfinishedIndex(state.idx);
  if (nxt === null) {
    toast("本 head 全部完成");
    updateFinalBtn();
  } else {
    showAssignment(nxt);
  }
}

async function onDispositionClick(d) {
  const a = state.assignments[state.idx];
  const ok = await saveAnnotation({ answer: a.answer, disposition: d, is_final: false });
  if (!ok) return;
  if (d === "稍后再看") {
    toast("已标记稍后再看");
  } else {
    toast(`已标记：${d}`);
    const nxt = nextUnfinishedIndex(state.idx);
    if (nxt !== null) showAssignment(nxt);
  }
}

function renderLabels(detail) {
  const wrap = $("#labels");
  wrap.innerHTML = "";
  const a = state.assignments[state.idx];
  const head = state.session.head;
  const current = a.answer;
  for (const label of detail.glossary.labels) {
    const btn = document.createElement("button");
    btn.className = "label-btn";
    btn.dataset.label = label.id;
    btn.innerHTML =
      `<span class="row"><span class="zh">${label.name_zh}</span>` +
      `<i class="info-dot" data-info="${label.id}">ⓘ</i></span>` +
      `<span class="en">${label.id}</span>`;
    const selected = isMultiHead(head)
      ? Array.isArray(current) && current.includes(label.id)
      : current === label.id;
    if (selected) btn.classList.add("selected");
    btn.querySelector(".info-dot").addEventListener("click", (ev) => {
      ev.stopPropagation();
      showLabelSheet(detail, label);
    });
    btn.addEventListener("click", () => onLabelClick(label.id));
    wrap.appendChild(btn);
  }
}

function onLabelClick(labelId) {
  const a = state.assignments[state.idx];
  let answer;
  if (isMultiHead(state.session.head)) {
    const cur = Array.isArray(a.answer) ? a.answer.slice() : [];
    const i = cur.indexOf(labelId);
    if (i >= 0) cur.splice(i, 1); else cur.push(labelId);
    answer = cur;
  } else {
    answer = a.answer === labelId ? a.answer : labelId;
  }
  saveAnnotation({ answer, disposition: a.disposition, is_final: false });
}

async function saveAnnotation(part) {
  const a = state.assignments[state.idx];
  const payload = Object.assign({ assignment_id: a.id }, part);
  try {
    const rec = await api("/api/annotations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    a.answer = rec.answer;
    a.disposition = rec.disposition;
    a.is_final = rec.is_final;
    a.revision = rec.revision;
    a.updated_at = rec.updated_at;
    toast("已入库");
    renderLabels(await getDetail(a.id));
    renderDispositions();
    updateFinalBtn();
    renderProgress();
    return true;
  } catch (e) {
    toast("保存失败: " + e.message, true);
    return false;
  }
}

function renderProgress() {
  const n = state.assignments.length;
  const done = state.assignments.filter(isDone).length;
  $("#progress-text").textContent = `${state.idx + 1}/${n} · 完成 ${done}`;
  $("#progress-fill").style.width = n ? `${(done / n) * 100}%` : "0";
}

/* ---------- 元数据 sheet ---------- */

function showMetaSheet() {
  const a = state.assignments[state.idx];
  const detail = state.details.get(a.id);
  if (!detail) return;
  const m = detail.sample.metadata || {};
  const rows = [
    ["date", m.date], ["stock_code", m.stock_code], ["stock_name", m.stock_name],
    ["source", m.source], ["split_provenance", m.split_provenance],
    ["sample_id", detail.sample.id], ["title", detail.sample.title],
  ];
  const html =
    '<h3 class="sheet-title">更多信息</h3>' +
    '<p class="sheet-sub">仅元数据，不包含任何模型预测</p>' +
    '<table class="meta-table">' +
    rows.map(([k, v]) => `<tr><th>${k}</th><td>${v == null || v === "" ? "—" : String(v)}</td></tr>`).join("") +
    "</table>";
  openSheet(html);
}

/* ---------- 释义 sheets ---------- */

function currentDetail() {
  const a = state.assignments[state.idx];
  return a ? state.details.get(a.id) : null;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function showHeadSheet() {
  const detail = currentDetail();
  if (!detail || !detail.glossary) return;
  const g = detail.glossary;
  const items = (arr) => (arr || []).map((x) => `<p class="gloss-item">${esc(x)}</p>`).join("");
  const html =
    `<h3 class="sheet-title">${esc(headName(state.session.head))} <span class="muted">(${esc(state.session.head)})</span></h3>` +
    `<p class="gloss-q">${esc(g.question)}</p>` +
    `<p class="gloss-def">${esc(g.definition_zh || "")}</p>` +
    (g.select_when ? `<p class="gloss-sec">✓ 该选</p>${items([g.select_when])}` : "") +
    (g.not_select_when ? `<p class="gloss-sec">✗ 不该选</p>${items([g.not_select_when])}` : "") +
    (g.positive_examples && g.positive_examples.length ? `<p class="gloss-sec">正例</p>${items(g.positive_examples)}` : "") +
    (g.confusable && g.confusable.length ? `<p class="gloss-sec">易混淆</p>${items(g.confusable)}` : "");
  openSheet(html);
}

function showLabelSheet(detail, label) {
  const g = detail.glossary;
  const html =
    `<h3 class="sheet-title">${esc(label.name_zh)} <span class="muted">(${esc(label.id)})</span></h3>` +
    `<p class="gloss-def">${esc(label.description)}</p>` +
    (g.confusable && g.confusable.length
      ? `<p class="gloss-sec">本 head 易混淆提示</p>` +
        g.confusable.map((x) => `<p class="gloss-item">${esc(x)}</p>`).join("")
      : "") +
    (detail.invariants && detail.invariants.length
      ? `<p class="gloss-sec">全局不变量</p><ul class="invariant-list">` +
        detail.invariants.map((x) => `<li>${esc(x)}</li>`).join("") + "</ul>"
      : "");
  openSheet(html);
}

function showSwitchSheet() {
  const html =
    '<h3 class="sheet-title">切换 head？</h3>' +
    '<p class="sheet-sub">Head Lock：一个会话锁定一个 head 连续标注。返回后当前进度保留，可另选 batch/head 重新进入。</p>';
  openSheet(html +
    '<button id="btn-switch-yes" class="ghost-btn" style="width:100%;height:44px;margin-top:8px;">返回选择页</button>');
  $("#btn-switch-yes").addEventListener("click", () => {
    closeSheet();
    state.session = { batchId: state.session.batchId, head: null };
    renderStart();
  });
}

/* ---------- 启动 ---------- */

function bindEvents() {
  $("#btn-enter").addEventListener("click", () => {
    if (state.session && state.session.batchId && state.session.head) {
      enterMain(state.session.batchId, state.session.head);
    }
  });
  $("#btn-prev").addEventListener("click", () => showAssignment(state.idx - 1));
  $("#btn-next").addEventListener("click", () => showAssignment(state.idx + 1));
  $("#btn-final").addEventListener("click", finalizeCurrent);
  $("#btn-meta").addEventListener("click", showMetaSheet);
  $("#sheet-close").addEventListener("click", closeSheet);
  $("#sheet-overlay").addEventListener("click", closeSheet);
  $("#btn-head-help").addEventListener("click", showHeadSheet);
  $("#btn-question-help").addEventListener("click", showHeadSheet);
  $("#btn-switch").addEventListener("click", showSwitchSheet);
}

async function init() {
  bindEvents();
  try {
    state.batches = await api("/api/batches");
  } catch (e) {
    $("#start-hint").textContent = "无法连接服务: " + e.message + "（请确认 python3 server.py 已启动）";
    return;
  }
  const saved = loadSession();
  if (saved && saved.batchId && saved.head && state.batches.some((b) => b.id === saved.batchId)) {
    await enterMain(saved.batchId, saved.head);
    return;
  }
  renderStart();
}

init();
