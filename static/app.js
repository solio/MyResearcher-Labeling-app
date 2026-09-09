"use strict";

/* MyResearcher 单人标注工具 — 前端（原生 JS，零依赖）
 * 保存链路：点击 → localStorage 重试队列 → POST /api/annotations → 已入库；
 * 失败则"本机已保存·待同步 N 条"，网络恢复（online 事件 / 10s 定时 / 启动）自动重试。
 */

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
const QUEUE_KEY = "mr_queue_v1";
const DETAILS_KEY = "mr_details_v1";
const DETAILS_CAP = 200;

const state = {
  session: null,       // { batchId, head }
  batches: [],
  batchesError: null,
  headCounts: {},      // headId -> {total, left}（当前选中 batch 的未完成量）
  completionShown: {}, // batchId -> true（完成页每 batch 每会话只弹一次）
  assignments: [],
  idx: 0,
  details: new Map(),  // assignment_id -> {sample, glossary, invariants}
  syncFailToastShown: false,
  retryTimer: null,
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

// API 基址按页面所在目录解析：挂在任意路径前缀（如 nginx /labeler/）下都成立。
// 入口 URL 必须以 / 结尾（nginx 侧用 location = /labeler {return 301 …} 兜底）。
const API_BASE = new URL("api/", document.baseURI).href;

async function api(path, opts) {
  const url = API_BASE + path.replace(/^\/api\//, "");
  const res = await fetch(url, opts);
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

function lsGet(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch (e) { return fallback; }
}
function lsSet(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

/* ---------- localStorage 重试队列 ---------- */

function readQueue() {
  const q = lsGet(QUEUE_KEY, []);
  return Array.isArray(q) ? q : [];
}

function enqueueSave(assignmentId, st) {
  const q = readQueue().filter((item) => item.assignment_id !== assignmentId);
  q.push({
    assignment_id: assignmentId,
    queued_at: new Date().toISOString(),
    payload: {
      assignment_id: assignmentId,
      answer: st.answer,
      disposition: st.disposition,
      is_final: !!st.is_final,
    },
  });
  lsSet(QUEUE_KEY, q);
  updateSyncBanner();
}

function dequeueSave(assignmentId) {
  lsSet(QUEUE_KEY, readQueue().filter((item) => item.assignment_id !== assignmentId));
}

function updateSyncBanner() {
  const n = readQueue().length;
  const b = $("#sync-banner");
  if (n > 0) {
    b.textContent = `本机已保存 · 待同步 ${n} 条`;
    b.classList.remove("hidden");
  } else {
    b.classList.add("hidden");
  }
}

let flushBusy = false;
async function flushQueue() {
  if (flushBusy) return;
  flushBusy = true;
  try {
    for (;;) {
      const q = readQueue();
      if (!q.length) break;
      const item = q[0];
      let rec;
      try {
        rec = await api("/api/annotations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(item.payload),
        });
      } catch (e) {
        if (String(e.message || "").includes("已归档")) {
          // 批次被归档后拒绝写入：丢弃队列里这条并继续，不进入重试
          dequeueSave(item.assignment_id);
          state.syncFailToastShown = false;
          toast("批次已归档，未同步的点击已丢弃", true);
          continue;
        }
        if (!state.syncFailToastShown) {
          toast(`本机已保存 · 待同步 ${q.length} 条`, true);
          state.syncFailToastShown = true;
        }
        clearTimeout(state.retryTimer);
        state.retryTimer = setTimeout(flushQueue, 5000);
        break;
      }
      dequeueSave(item.assignment_id);
      state.syncFailToastShown = false;
      const row = state.assignments.find((a) => a.id === item.assignment_id);
      if (row) {
        row.revision = rec.revision;
        row.updated_at = rec.updated_at;
        row.status = rec.is_final ? "completed" : row.status;
      }
      toast("已入库");
      // 当前 head 全部完成 → 查一次批次完成度（离线补传的场景也会在这里触发）
      if (state.assignments.length && state.assignments.every(isDone)) {
        maybeShowBatchComplete(state.session && state.session.batchId);
      }
    }
  } finally {
    flushBusy = false;
  }
  updateSyncBanner();
}

/* ---------- 本地缓存（列表 + 详情），用于离线恢复 ---------- */

function listKey(batchId, head) {
  return `mr_list_v1__${batchId}__${head}`;
}
function cacheList(batchId, head, rows) {
  try { lsSet(listKey(batchId, head), rows); } catch (e) { /* 存储满则放弃 */ }
}
function cachedList(batchId, head) {
  const rows = lsGet(listKey(batchId, head), null);
  return Array.isArray(rows) ? rows : null;
}

function cachedDetail(assignmentId) {
  const store = lsGet(DETAILS_KEY, {});
  const d = store[assignmentId];
  return d ? { sample: d.sample, glossary: d.glossary, invariants: d.invariants || [] } : null;
}
function persistDetail(assignmentId, detail) {
  try {
    const store = lsGet(DETAILS_KEY, {});
    store[assignmentId] = {
      sample: detail.sample,
      glossary: detail.glossary,
      invariants: detail.invariants || [],
    };
    const keys = Object.keys(store);
    if (keys.length > DETAILS_CAP) {
      for (const k of keys.slice(0, keys.length - DETAILS_CAP)) delete store[k];
    }
    lsSet(DETAILS_KEY, store);
  } catch (e) { /* 存储满则放弃 */ }
}

/* 把未同步的队列 payload 合并进任务列表（断网/刷新后不丢已点击答案） */
function mergePendingQueue(rows) {
  const pending = {};
  for (const item of readQueue()) pending[item.assignment_id] = item.payload;
  return rows.map((r) => {
    const p = pending[r.id];
    if (!p) return r;
    return Object.assign({}, r, {
      answer: p.answer,
      disposition: p.disposition,
      is_final: p.is_final,
    });
  });
}

/* ---------- 会话 ---------- */

function loadSession() {
  return lsGet(SESSION_KEY, null);
}
function saveSession() {
  lsSet(SESSION_KEY, state.session);
}

/* ---------- 开始屏 ---------- */

function headSubText(batchId, headId) {
  if (!batchId) return "";
  const c = state.headCounts[headId];
  return c && c.total ? `剩 ${c.left} / ${c.total}` : "";
}

function batchSubText(b) {
  const done = b.done || 0;
  return done >= b.total ? `已完成 ✓ ${done} / ${b.total}` : `完成 ${done} / ${b.total}`;
}

function renderStart() {
  $("#screen-main").classList.add("hidden");
  $("#screen-start").classList.remove("hidden");
  const bl = $("#batch-list");
  bl.innerHTML = "";
  if (!state.batches.length) {
    bl.innerHTML = state.batchesError
      ? '<p class="muted">无法连接服务且没有本地会话：请启动 python3 server.py 后刷新。</p>'
      : '<p class="muted">暂无 batch：先用 tools/import_batch.py 或 tools/seed_demo.py 导入。</p>';
  }
  for (const b of state.batches) {
    const row = document.createElement("div");
    row.className = "pick-item batch-row";
    row.innerHTML =
      `<span class="batch-info"><span>${b.id}</span>` +
      `<span class="sub">${batchSubText(b)}</span></span>`;
    if (state.session && state.session.batchId === b.id) row.classList.add("selected");
    row.addEventListener("click", () => {
      state.session = { batchId: b.id, head: state.session && state.session.batchId === b.id ? state.session.head : null };
      state.headCounts = {};
      renderStart();
      refreshStartData();
    });
    const arch = document.createElement("button");
    arch.className = "arch-btn";
    arch.textContent = "归档";
    arch.addEventListener("click", (ev) => {
      ev.stopPropagation();
      confirmArchiveBatch(b.id);
    });
    row.appendChild(arch);
    bl.appendChild(row);
  }
  const hl = $("#head-list");
  hl.innerHTML = "";
  const selBatchId = state.session && state.session.batchId;
  for (const h of HEADS) {
    const btn = document.createElement("button");
    btn.className = "pick-item";
    btn.innerHTML = `<span>${h.name}</span><span class="sub">${headSubText(selBatchId, h.id)}</span>`;
    if (state.session && state.session.head === h.id) btn.classList.add("selected");
    btn.addEventListener("click", () => {
      if (!state.session) state.session = { batchId: null, head: null };
      state.session.head = h.id;
      renderStart();
      refreshStartData();
    });
    hl.appendChild(btn);
  }
  const ready = !!(state.session && state.session.batchId && state.session.head);
  $("#btn-enter").disabled = !ready;
  $("#start-hint").textContent = ready
    ? `将进入 ${state.session.batchId} · ${headName(state.session.head)}（会话内锁定该 head）`
    : "先选择 batch 和 head";
}

/* 拉取选择页数据：batch 完成度 + 选中 batch 各 head 的剩余量（离线回退本地缓存） */
async function fetchHeadCount(batchId, headId) {
  let rows = cachedList(batchId, headId);
  try {
    rows = await api(`/api/assignments?batch_id=${encodeURIComponent(batchId)}&head=${encodeURIComponent(headId)}`);
    cacheList(batchId, headId, rows);
  } catch (e) { /* 离线用缓存 */ }
  if (!Array.isArray(rows)) return null;
  return { total: rows.length, left: rows.filter((r) => !isDone(r)).length };
}

let startRefreshBusy = false;
async function refreshStartData() {
  if (startRefreshBusy) return;
  startRefreshBusy = true;
  try {
    const batchId = state.session && state.session.batchId;
    const jobs = [
      api("/api/batches").then((bs) => { state.batches = bs; }).catch(() => {}),
    ];
    if (batchId) {
      for (const h of HEADS) {
        jobs.push(
          fetchHeadCount(batchId, h.id).then((c) => { if (c) state.headCounts[h.id] = c; })
        );
      }
    }
    await Promise.all(jobs);
  } finally {
    startRefreshBusy = false;
  }
  if ($("#screen-start").classList.contains("hidden")) return; // 已进入标注屏
  renderStart();
}

/* ---------- 主屏 ---------- */

async function enterMain(batchId, head, jumpToId) {
  state.session = { batchId, head };
  saveSession();
  let rows = null;
  try {
    rows = await api(`/api/assignments?batch_id=${encodeURIComponent(batchId)}&head=${encodeURIComponent(head)}`);
    cacheList(batchId, head, rows);
  } catch (e) {
    if (String(e.message || "").includes("已归档")) {
      toast("该批次已归档，不可进入", true);
      state.session = { batchId: null, head: null };
      saveSession();
      renderStart();
      refreshStartData();
      return false;
    }
    rows = cachedList(batchId, head);
    if (!rows) {
      toast("无法加载任务且无本地缓存", true);
      return false;
    }
    toast("离线：使用本地缓存");
  }
  state.assignments = mergePendingQueue(rows);
  if (!state.assignments.length) {
    toast("该 batch/head 没有任何任务", true);
    return false;
  }
  if (state.assignments.every(isDone)) maybeShowBatchComplete(batchId);
  state.idx = 0;
  if (jumpToId) {
    const i = state.assignments.findIndex((a) => a.id === jumpToId);
    if (i >= 0 && !isDone(state.assignments[i])) state.idx = i;
  }
  if (!jumpToId || state.idx === 0) state.idx = firstUnfinishedIndex();
  $("#screen-start").classList.add("hidden");
  $("#screen-main").classList.remove("hidden");
  $("#head-name").textContent = headName(head);
  await showAssignment(state.idx);
  flushQueue();
  return true;
}

function firstUnfinishedIndex() {
  const i = state.assignments.findIndex((a) => !isDone(a));
  return i >= 0 ? i : 0;
}

async function getDetail(assignmentId) {
  if (state.details.has(assignmentId)) return state.details.get(assignmentId);
  const local = cachedDetail(assignmentId);
  if (local) {
    state.details.set(assignmentId, local);
    return local;
  }
  const d = await api(`/api/assignment?id=${encodeURIComponent(assignmentId)}`);
  const slim = { sample: d.sample, glossary: d.glossary, invariants: d.invariants || [] };
  state.details.set(assignmentId, slim);
  persistDetail(assignmentId, slim);
  return slim;
}

async function showAssignment(idx) {
  if (idx < 0 || idx >= state.assignments.length) return;
  state.idx = idx;
  const a = state.assignments[idx];
  let detail;
  try {
    detail = await getDetail(a.id);
  } catch (e) {
    $("#card-title").textContent = a.title || "（无标题）";
    $("#card-content").textContent = "（离线且无该条内容缓存）";
    $("#card-sample-id").textContent = a.sample_id;
    $("#question-text").textContent = "";
    $("#labels").innerHTML = '<p class="muted">内容不可用：恢复网络后重试。</p>';
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

function finalizeCurrent() {
  const a = state.assignments[state.idx];
  if (!hasAnswer(a)) return;
  saveAnnotation({ answer: a.answer, disposition: a.disposition, is_final: true });
  toast("已完成本条");
  const nxt = nextUnfinishedIndex(state.idx);
  if (nxt === null) {
    toast("本 head 全部完成");
    updateFinalBtn();
  } else {
    showAssignment(nxt);
  }
}

function onDispositionClick(d) {
  const a = state.assignments[state.idx];
  if (a.disposition === d) {
    // 再点一次 = 取消：清除 disposition，保留已有答案与 final 状态
    saveAnnotation({ answer: a.answer, disposition: null, is_final: a.is_final });
    toast("已取消标记");
    return;
  }
  saveAnnotation({ answer: a.answer, disposition: d, is_final: false });
  if (d === "稍后再看") {
    toast("已标记稍后再看");
  } else {
    toast(`已标记：${d}`);
    const nxt = nextUnfinishedIndex(state.idx);
    if (nxt !== null) showAssignment(nxt);
  }
}

/* 保存链路：先落 localStorage 队列，再尝试 POST（幂等 upsert） */
function saveAnnotation(st) {
  const a = state.assignments[state.idx];
  a.answer = st.answer;
  a.disposition = st.disposition;
  a.is_final = st.is_final;
  enqueueSave(a.id, st);
  const detail = state.details.get(a.id);
  if (detail) renderLabels(detail);
  renderDispositions();
  updateFinalBtn();
  renderProgress();
  flushQueue();
}

function renderProgress() {
  const n = state.assignments.length;
  const done = state.assignments.filter(isDone).length;
  $("#progress-text").textContent = `${state.idx + 1}/${n} · 完成 ${done}`;
  $("#progress-fill").style.width = n ? `${(done / n) * 100}%` : "0";
}

/* ---------- 元数据 sheet ---------- */

function currentDetail() {
  const a = state.assignments[state.idx];
  return a ? state.details.get(a.id) : null;
}

function showMetaSheet() {
  const detail = currentDetail();
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
    '<p class="sheet-sub">Head Lock：一个会话锁定一个 head 连续标注。返回后当前进度保留，可另选 batch/head 重新进入。</p>' +
    '<button id="btn-switch-yes" class="ghost-btn" style="width:100%;height:44px;margin-top:8px;">返回选择页</button>';
  openSheet(html);
  $("#btn-switch-yes").addEventListener("click", () => {
    closeSheet();
    state.session = { batchId: state.session.batchId, head: null };
    renderStart();
    refreshStartData();
  });
}

/* ---------- 批次归档 ---------- */

function confirmArchiveBatch(batchId) {
  const html =
    '<h3 class="sheet-title">归档该批次？</h3>' +
    `<p class="sheet-sub">${esc(batchId)}</p>` +
    '<p class="gloss-def">归档后：批次从列表隐藏、不可再标注；数据保留不变，' +
    '并导出一份 CSV 快照到服务器 data/exports/。</p>' +
    '<button id="btn-archive-yes" class="ghost-btn" style="width:100%;height:44px;margin-top:8px;">确定归档</button>';
  openSheet(html);
  $("#btn-archive-yes").addEventListener("click", async () => {
    try {
      const r = await api("/api/batch/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ batch_id: batchId }),
      });
      closeSheet();
      toast(`已归档 · CSV ${r.export_rows} 行已导出`);
      if (state.session && state.session.batchId === batchId) {
        state.session = { batchId: null, head: null };
      }
      renderStart();
      refreshStartData();
    } catch (e) {
      toast(`归档失败：${e.message}`, true);
    }
  });
}

/* ---------- 批次完成页 ---------- */

async function maybeShowBatchComplete(batchId) {
  if (!batchId || state.completionShown[batchId]) return;
  try {
    const bs = await api("/api/batches");
    state.batches = bs;
    const b = bs.find((x) => x.id === batchId);
    if (b && b.total > 0 && (b.done || 0) >= b.total) {
      state.completionShown[batchId] = true;
      const html =
        '<h3 class="sheet-title">本批次已全部填完</h3>' +
        `<p class="sheet-sub">${esc(batchId)} · ${b.done} / ${b.total}</p>` +
        '<p class="gloss-def">数据已在服务端，本机（GPT）可随时拉取结果。</p>' +
        '<button id="btn-complete-back" class="ghost-btn" style="width:100%;height:44px;margin-top:8px;">返回选择页</button>';
      openSheet(html);
      $("#btn-complete-back").addEventListener("click", () => {
        closeSheet();
        state.session = { batchId: state.session.batchId, head: null };
        renderStart();
        refreshStartData();
      });
    }
  } catch (e) { /* 离线不弹，等下次触发 */ }
}

/* ---------- 启动 / resume ---------- */

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
  window.addEventListener("online", () => flushQueue());
}

async function init() {
  bindEvents();
  try {
    state.batches = await api("/api/batches");
  } catch (e) {
    state.batches = [];
    state.batchesError = e.message;
  }

  // 恢复路径：/api/resume（服务端最近未完成） + localStorage 会话/队列/缓存
  let target = null;
  const saved = loadSession();
  if (saved && saved.batchId && saved.head) {
    target = { batchId: saved.batchId, head: saved.head, jumpToId: null };
  }
  try {
    const resume = await api("/api/resume");
    if (resume && resume.assignment_id) {
      const d = await api(`/api/assignment?id=${encodeURIComponent(resume.assignment_id)}`);
      const rid = d.assignment.id;
      const rbatch = d.assignment.batch_id;
      const rhead = d.assignment.head;
      if (target && target.batchId === rbatch && target.head === rhead) {
        target.jumpToId = rid;
      } else {
        target = { batchId: rbatch, head: rhead, jumpToId: rid };
      }
    }
  } catch (e) { /* 离线：完全走本地 */ }

  if (target && target.batchId && target.head) {
    const ok = await enterMain(target.batchId, target.head, target.jumpToId);
    if (ok) return;
  }
  renderStart();
  refreshStartData();
}

setInterval(() => {
  if (readQueue().length) flushQueue();
}, 10000);

init();
