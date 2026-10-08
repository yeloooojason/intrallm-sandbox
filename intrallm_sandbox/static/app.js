"use strict";

const REFRESH_MS = 5000;
const $ = (id) => document.getElementById(id);
const state = { token: null, me: null, sandboxes: [], summary: null, openId: null, openTemplate: null };
const TEMPLATE = { code: "代码", desktop: "桌面" };

// ---------------------------------------------------------------- helpers
function getToken() { try { return localStorage.getItem("isb_token"); } catch { return null; } }
function setToken(t) { try { t ? localStorage.setItem("isb_token", t) : localStorage.removeItem("isb_token"); } catch {} }

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}`, ...(opts.headers || {}) },
  });
  if (res.status === 401) { logout(); throw new Error("未授权"); }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(Math.floor(Math.log(b) / Math.log(1024)), u.length - 1);
  const v = b / 1024 ** i;
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
}
function fmtDur(sec) {
  sec = Math.max(0, Math.floor(sec));
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
  if (d) return `${d}d ${h % 24}h`;
  if (h) return `${h}h ${m % 60}m`;
  return `${m}m`;
}
const fmtPct = (v) => `${(v ?? 0).toFixed(1)}%`;
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
const fmtDateTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
const now = () => Date.now() / 1000;
const STATUS = { running: "运行中", creating: "创建中", terminated: "已销毁", error: "异常" };
const meterClass = (p) => (p >= 90 ? "bad" : p >= 70 ? "warn" : "");

// ---------------------------------------------------------------- chart
function lineChart(el, points, { max, color = "var(--series-1)", fmt = fmtPct } = {}) {
  if (!points.length) { el.innerHTML = `<div class="placeholder">采集中…</div>`; return; }
  const W = el.clientWidth || 400, H = el.clientHeight || 160;
  const pad = { l: 40, r: 8, t: 8, b: 20 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const t0 = points[0].ts, t1 = points[points.length - 1].ts || t0 + 1;
  const vmax = max ?? Math.max(1, ...points.map((p) => p.v)) * 1.15;
  const x = (t) => pad.l + (t1 === t0 ? iw : ((t - t0) / (t1 - t0)) * iw);
  const y = (v) => pad.t + ih - (Math.min(v, vmax) / vmax) * ih;
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(p.ts).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const area = `${path}L${x(t1).toFixed(1)},${pad.t + ih}L${x(t0).toFixed(1)},${pad.t + ih}Z`;
  let grid = "";
  for (let i = 0; i <= 4; i++) {
    const v = (vmax / 4) * i, yy = y(v).toFixed(1);
    grid += `<line class="gridline" x1="${pad.l}" x2="${W - pad.r}" y1="${yy}" y2="${yy}"/>`;
    grid += `<text class="axis" x="${pad.l - 6}" y="${+yy + 3.5}" text-anchor="end">${esc(fmt(v))}</text>`;
  }
  const xt = [t0, (t0 + t1) / 2, t1].map((t, i) =>
    `<text class="axis" x="${x(t)}" y="${H - 4}" text-anchor="${["start", "middle", "end"][i]}">${fmtTime(t)}</text>`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${grid}${xt}
    <path class="area" d="${area}" fill="${color}"/>
    <path class="line" d="${path}" stroke="${color}"/>
    <line class="cross" x1="0" x2="0" y1="${pad.t}" y2="${pad.t + ih}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>
    <circle class="hover-dot" r="3.5" fill="${color}" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
  </svg>`;
  const svg = el.querySelector("svg"), cross = svg.querySelector(".cross"), dot = svg.querySelector(".hover-dot");
  const tip = $("tooltip");
  svg.onmousemove = (e) => {
    const r = svg.getBoundingClientRect();
    const mx = ((e.clientX - r.left) / r.width) * W;
    let best = points[0];
    for (const p of points) if (Math.abs(x(p.ts) - mx) < Math.abs(x(best.ts) - mx)) best = p;
    cross.setAttribute("x1", x(best.ts)); cross.setAttribute("x2", x(best.ts)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(best.ts)); dot.setAttribute("cy", y(best.v)); dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.textContent = `${fmtTime(best.ts)}  ${fmt(best.v)}`;
    tip.style.left = `${e.clientX + 12}px`; tip.style.top = `${e.clientY - 28}px`;
  };
  svg.onmouseleave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); };
}

// ---------------------------------------------------------------- render
function kpi(label, value, sub = "", pct = null) {
  const meter = pct == null ? "" :
    `<div class="meter ${meterClass(pct)}"><span style="width:${Math.min(100, pct).toFixed(1)}%"></span></div>`;
  return `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ""}${meter}</div>`;
}

function renderSummary() {
  const s = state.summary;
  if (!s) return;
  $("runtime-badge").textContent = s.runtime;
  const u = s.usage;
  const cpuOfAlloc = u.allocated_cpu ? u.cpu_percent / u.allocated_cpu : 0;
  const memOfAlloc = u.allocated_memory_bytes ? (u.memory_bytes / u.allocated_memory_bytes) * 100 : 0;
  let html =
    kpi("运行中沙箱", `${s.running}<small> / ${s.quota.max_total}</small>`, `每用户上限 ${s.quota.max_per_owner}`, (s.active / s.quota.max_total) * 100) +
    kpi("使用用户", s.owners.length, s.owners.length ? `最多：${esc(s.owners[0].owner)}（${s.owners[0].sandboxes}）` : "—") +
    kpi("沙箱 CPU 占用", fmtPct(u.cpu_percent), `已分配 ${u.allocated_cpu} 核 · 利用率 ${fmtPct(cpuOfAlloc)}`, cpuOfAlloc) +
    kpi("沙箱内存", fmtBytes(u.memory_bytes), `已分配 ${fmtBytes(u.allocated_memory_bytes)}`, memOfAlloc);
  if (s.host) {
    html +=
      kpi("宿主机 CPU", fmtPct(s.host.cpu_percent), `${s.host.cpu_count} 核 · load ${s.host.load1.toFixed(2)}`, s.host.cpu_percent) +
      kpi("宿主机内存", fmtPct(s.host.memory_percent), `${fmtBytes(s.host.memory_used)} / ${fmtBytes(s.host.memory_total)}`, s.host.memory_percent);
  }
  $("kpis").innerHTML = html;

  $("host-charts").hidden = !s.host;
  if (s.host) {
    const h = s.host_history;
    lineChart($("chart-host-cpu"), h.map((p) => ({ ts: p.ts, v: p.cpu_percent })), { max: 100 });
    lineChart($("chart-host-mem"), h.map((p) => ({ ts: p.ts, v: p.memory_percent })), { max: 100, color: "var(--series-2)" });
    $("host-cpu-now").textContent = fmtPct(s.host.cpu_percent);
    $("host-mem-now").textContent = `${fmtBytes(s.host.memory_used)} / ${fmtBytes(s.host.memory_total)}`;
  }

  const maxN = Math.max(1, ...s.owners.map((o) => o.sandboxes));
  $("owners").innerHTML = s.owners.length ? s.owners.map((o) => `
    <div class="owner-row">
      <div class="top"><span class="name">${esc(o.owner)}</span><span class="meta">${o.sandboxes} 个沙箱</span></div>
      <div class="meta">CPU ${fmtPct(o.cpu_percent)} · 内存 ${fmtBytes(o.memory_bytes)}</div>
      <div class="meter"><span style="width:${(o.sandboxes / maxN) * 100}%"></span></div>
    </div>`).join("") : `<div class="muted">暂无分配</div>`;
}

function renderRows() {
  const q = $("filter").value.trim().toLowerCase();
  const rows = state.sandboxes.filter((sb) =>
    !q || [sb.id, sb.name, sb.owner, sb.created_by].some((v) => String(v).toLowerCase().includes(q)));
  $("empty").hidden = rows.length > 0;
  const t = now();
  $("rows").innerHTML = rows.map((sb) => {
    const st = sb.stats;
    const cpuPct = st ? st.cpu_percent : 0;
    const cpuOfLimit = sb.cpu ? Math.min(100, cpuPct / sb.cpu) : 0;
    const memPct = st && st.memory_limit_bytes ? (st.memory_bytes / st.memory_limit_bytes) * 100 : 0;
    const active = sb.status === "running" || sb.status === "creating";
    const end = sb.terminated_at || t;
    return `<tr data-id="${esc(sb.id)}">
      <td><div class="sb-name">${esc(sb.name)}${sb.template === "desktop" ? `<span class="tag">桌面</span>` : ""}</div><div class="sb-id mono">${esc(sb.id)} · ${esc(sb.image)}</div></td>
      <td>${esc(sb.owner)}</td>
      <td class="muted">${esc(sb.created_by)}</td>
      <td><span class="pill ${esc(sb.status)}" title="${esc(sb.terminate_reason || "")}">${STATUS[sb.status] || esc(sb.status)}</span></td>
      <td class="num">${st ? `<span class="bar-cell">${fmtPct(cpuPct)}<span class="mini-bar"><span style="width:${cpuOfLimit}%"></span></span></span>` : "—"}</td>
      <td class="num">${st ? `<span class="bar-cell">${fmtBytes(st.memory_bytes)}<span class="mini-bar mem"><span style="width:${memPct}%"></span></span></span>` : "—"}</td>
      <td class="num">${fmtDur(end - sb.created_at)}</td>
      <td class="num">${active ? fmtDur(sb.expires_at - t) : "—"}</td>
      <td class="num">${active ? `<button class="btn sm danger" data-stop="${esc(sb.id)}">停止</button>` : ""}</td>
    </tr>`;
  }).join("");
}

// ---------------------------------------------------------------- drawer
async function openDrawer(id) {
  state.openId = id;
  state.openTemplate = null;
  $("drawer").hidden = false;
  $("drawer").classList.remove("wide");
  $("d-desktop").hidden = true;
  $("d-output").innerHTML = "";
  $("d-takeover").checked = false;
  setTakeover(false);
  await refreshDrawer();
}

async function refreshDrawer() {
  const id = state.openId;
  if (!id) return;
  const [sb, hist, audit] = await Promise.all([
    api(`/api/v1/sandboxes/${id}`), api(`/api/v1/sandboxes/${id}/metrics`), api(`/api/v1/sandboxes/${id}/audit?limit=50`),
  ]);
  if (state.openId !== id) return;
  const active = sb.status === "running";
  $("d-title").textContent = sb.name;
  $("d-id").textContent = sb.id;
  const facts = [
    ["状态", `<span class="pill ${esc(sb.status)}">${STATUS[sb.status] || esc(sb.status)}</span>${sb.terminate_reason ? ` <span class="muted">${esc(sb.terminate_reason)}</span>` : ""}`],
    ["分配给", esc(sb.owner)],
    ["创建者", esc(sb.created_by)],
    ["类型", TEMPLATE[sb.template] || esc(sb.template)],
    ["镜像", `<span class="mono">${esc(sb.image)}</span>`],
    ["资源限制", `${sb.cpu} 核 · ${sb.memory_mb} MB`],
    ["创建时间", fmtDateTime(sb.created_at)],
    ["到期时间", fmtDateTime(sb.expires_at)],
    ["最近活动", fmtDateTime(sb.last_active_at)],
    ["执行次数", sb.exec_count],
  ];
  if (Object.keys(sb.labels || {}).length) facts.push(["标签", esc(Object.entries(sb.labels).map(([k, v]) => `${k}=${v}`).join(", "))]);
  $("d-facts").innerHTML = facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  lineChart($("chart-sb-cpu"), hist.map((p) => ({ ts: p.ts, v: p.cpu_percent })), { max: Math.max(100, sb.cpu * 100) });
  const memLimit = sb.memory_mb * 1024 * 1024;
  lineChart($("chart-sb-mem"), hist.map((p) => ({ ts: p.ts, v: p.memory_bytes })), { max: memLimit, color: "var(--series-2)", fmt: fmtBytes });
  const last = hist[hist.length - 1];
  $("d-cpu-now").textContent = last ? `${fmtPct(last.cpu_percent)} / ${sb.cpu * 100}%` : "";
  $("d-mem-now").textContent = last ? `${fmtBytes(last.memory_bytes)} / ${fmtBytes(memLimit)}` : "";

  $("d-audit").innerHTML = audit.map((a) => {
    const d = a.detail || {};
    const desc = d.command ? `$ ${d.command} → ${d.exit_code}`
      : d.reason || d.path || d.url || (d.owner ? `→ ${d.owner}` : "")
      || (d.coordinate ? `@${d.coordinate.join(",")}` : "") || (d.ref ? `ref ${d.ref}` : "")
      || (d.text_len != null ? `${d.text_len} 个字符` : "");
    return `<li><span class="t">${fmtTime(a.ts)}</span><span class="a">${esc(a.action)}</span><span class="d" title="${esc(desc)}">${esc(a.actor)} ${esc(desc)}</span></li>`;
  }).join("") || `<li class="muted">无记录</li>`;

  const isDesktop = sb.template === "desktop";
  $("d-desktop").hidden = !(isDesktop && active);
  $("drawer").classList.toggle("wide", isDesktop && active);
  if (state.openTemplate !== sb.template || !active) {
    state.openTemplate = sb.template;
    if (isDesktop && active) startScreen(); else stopScreen();
  }
  $("d-console-card").hidden = !active;
  $("d-extend").hidden = !active;
  $("d-stop").hidden = !active;
}

function closeDrawer() { state.openId = null; state.openTemplate = null; stopScreen(); $("drawer").hidden = true; }

// ---------------------------------------------------------------- live desktop
const SCREEN_MS = 1200;
const screen = { timer: null, inflight: false, url: null, w: 1280, h: 800, typeBuf: "", typeTimer: null };

function startScreen() {
  stopScreen();
  pollScreen();
  screen.timer = setInterval(() => { if (!document.hidden) pollScreen(); }, SCREEN_MS);
}
function stopScreen() { clearInterval(screen.timer); screen.timer = null; }

async function pollScreen() {
  const id = state.openId;
  if (!id || screen.inflight) return;
  screen.inflight = true;
  const t0 = performance.now();
  try {
    const res = await fetch(`/api/v1/sandboxes/${id}/desktop/screen?format=jpeg&quality=60`, {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!res.ok || state.openId !== id) return;
    const url = URL.createObjectURL(await res.blob());
    const img = $("d-screen-img");
    img.onload = () => { screen.w = img.naturalWidth; screen.h = img.naturalHeight; };
    img.src = url;
    if (screen.url) URL.revokeObjectURL(screen.url);
    screen.url = url;
    $("d-screen-meta").textContent = `${Math.round(performance.now() - t0)} ms`;
  } catch { /* transient; next tick retries */ } finally { screen.inflight = false; }
}

function desktop(tool, body) {
  return api(`/api/v1/sandboxes/${state.openId}/desktop/${tool}`, {
    method: "POST", body: JSON.stringify({ screenshot: false, ...body }),
  }).then(() => pollScreen()).catch((e) => { $("d-screen-meta").textContent = e.message; });
}

function setTakeover(on) {
  $("d-screen").classList.toggle("control", on);
  $("d-type-form").hidden = !on;
  if (on) $("d-screen").focus();
}

function screenXY(e) {
  const img = $("d-screen-img"), r = img.getBoundingClientRect();
  // object-fit: contain may letterbox the image inside the element.
  const scale = Math.min(r.width / screen.w, r.height / screen.h);
  const ox = (r.width - screen.w * scale) / 2, oy = (r.height - screen.h * scale) / 2;
  const x = Math.round((e.clientX - r.left - ox) / scale), y = Math.round((e.clientY - r.top - oy) / scale);
  return x >= 0 && y >= 0 && x < screen.w && y < screen.h ? [x, y] : null;
}

const KEYMAP = { Enter: "Enter", Escape: "Escape", Backspace: "BackSpace", Tab: "Tab", Delete: "Delete",
  ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right", Home: "Home", End: "End",
  PageUp: "Prior", PageDown: "Next", " ": "space" };

function flushTyping() {
  clearTimeout(screen.typeTimer);
  if (!screen.typeBuf) return;
  const text = screen.typeBuf; screen.typeBuf = "";
  desktop("computer", { action: "type", text });
}

async function stopSandbox(id) {
  if (!confirm(`确认停止并销毁沙箱 ${id}？工作区数据将被删除。`)) return;
  try { await api(`/api/v1/sandboxes/${id}`, { method: "DELETE" }); } catch (e) { alert(e.message); }
  await refresh();
  if (state.openId === id) refreshDrawer();
}

// ---------------------------------------------------------------- data loop
let timer = null;
async function refresh() {
  try {
    const status = $("status-filter").value;
    const [summary, sandboxes] = await Promise.all([
      api("/api/v1/dashboard/summary"),
      api(`/api/v1/sandboxes?active=${status === "active"}`),
    ]);
    state.summary = summary;
    state.sandboxes = sandboxes;
    renderSummary();
    renderRows();
    $("live").classList.remove("stale");
    $("updated").textContent = `更新于 ${fmtTime(now())}`;
    if (state.openId) refreshDrawer().catch(() => {});
  } catch (e) {
    $("live").classList.add("stale");
    $("updated").textContent = `连接失败：${e.message}`;
  }
}

function startLoop() {
  clearInterval(timer);
  refresh();
  timer = setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
}

async function login(token) {
  state.token = token;
  const me = await api("/api/v1/whoami");
  state.me = me;
  setToken(token);
  $("who").textContent = `${me.name} · ${me.role}`;
  $("login").hidden = true;
  $("app").hidden = false;
  startLoop();
}

function logout() {
  clearInterval(timer);
  setToken(null);
  state.token = null;
  $("app").hidden = true;
  closeDrawer();
  $("login").hidden = false;
}

// ---------------------------------------------------------------- events
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try { await login($("token-input").value.trim()); } catch (err) { $("login-error").textContent = err.message; }
};
$("logout").onclick = logout;
$("filter").oninput = renderRows;
$("status-filter").onchange = refresh;
$("rows").onclick = (e) => {
  const stop = e.target.closest("[data-stop]");
  if (stop) { e.stopPropagation(); stopSandbox(stop.dataset.stop); return; }
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDrawer(tr.dataset.id).catch((err) => alert(err.message));
};
$("d-close").onclick = closeDrawer;
$("d-stop").onclick = () => stopSandbox(state.openId);
$("d-extend").onclick = async () => {
  try { await api(`/api/v1/sandboxes/${state.openId}/extend`, { method: "POST", body: JSON.stringify({ seconds: 3600 }) }); }
  catch (e) { alert(e.message); }
  refreshDrawer(); refresh();
};
$("d-console").onsubmit = async (e) => {
  e.preventDefault();
  const cmd = $("d-cmd").value.trim();
  if (!cmd) return;
  const out = $("d-output");
  out.insertAdjacentHTML("beforeend", `<span class="meta">$ ${esc(cmd)}</span>\n`);
  try {
    const r = await api(`/api/v1/sandboxes/${state.openId}/exec`, { method: "POST", body: JSON.stringify({ command: cmd }) });
    out.insertAdjacentHTML("beforeend",
      `${esc(r.stdout)}${r.stderr ? `<span class="err">${esc(r.stderr)}</span>` : ""}<span class="meta">[exit ${r.exit_code} · ${r.duration_ms}ms${r.timed_out ? " · 超时" : ""}]</span>\n`);
  } catch (err) {
    out.insertAdjacentHTML("beforeend", `<span class="err">${esc(err.message)}</span>\n`);
  }
  out.scrollTop = out.scrollHeight;
  $("d-cmd").value = "";
};
$("d-takeover").onchange = (e) => setTakeover(e.target.checked);
const scr = $("d-screen");
let clickTimer = null;
scr.addEventListener("click", (e) => {
  if (!$("d-takeover").checked) return;
  const xy = screenXY(e); if (!xy) return;
  scr.focus();
  clearTimeout(clickTimer);  // wait briefly so a double click isn't sent as two clicks
  clickTimer = setTimeout(() => desktop("computer", { action: "left_click", coordinate: xy }), 220);
});
scr.addEventListener("dblclick", (e) => {
  if (!$("d-takeover").checked) return;
  clearTimeout(clickTimer);
  const xy = screenXY(e); if (xy) desktop("computer", { action: "double_click", coordinate: xy });
});
scr.addEventListener("contextmenu", (e) => {
  if (!$("d-takeover").checked) return;
  e.preventDefault();
  const xy = screenXY(e); if (xy) desktop("computer", { action: "right_click", coordinate: xy });
});
scr.addEventListener("wheel", (e) => {
  if (!$("d-takeover").checked) return;
  e.preventDefault();
  const xy = screenXY(e); if (!xy) return;
  desktop("computer", { action: "scroll", coordinate: xy, direction: e.deltaY < 0 ? "up" : "down", amount: 2 });
}, { passive: false });
scr.addEventListener("keydown", (e) => {
  if (!$("d-takeover").checked) return;
  e.preventDefault();
  const mods = [e.ctrlKey && "ctrl", e.altKey && "alt", e.metaKey && "super"].filter(Boolean);
  if (e.key.length === 1 && !mods.length) {
    screen.typeBuf += e.key;
    clearTimeout(screen.typeTimer);
    screen.typeTimer = setTimeout(flushTyping, 150);
    return;
  }
  const name = KEYMAP[e.key] || (/^F\d+$/.test(e.key) ? e.key : e.key.length === 1 ? e.key.toLowerCase() : null);
  if (!name) return;  // lone modifier keys
  flushTyping();
  if (e.shiftKey && e.key.length !== 1) mods.push("shift");
  desktop("computer", { action: "key", text: [...mods, name].join("+") });
});
$("d-type-form").onsubmit = (e) => {
  e.preventDefault();
  const text = $("d-type").value;
  if (text) desktop("computer", { action: "type", text });
  $("d-type").value = "";
};
document.querySelectorAll("[data-key]").forEach((b) => (b.onclick = () => desktop("computer", { action: "key", text: b.dataset.key })));
$("c-template").onchange = (e) => {
  const f = $("create-form"), desk = e.target.value === "desktop";
  f.cpu.value = desk ? 2 : 1;
  f.memory_mb.value = desk ? 2048 : 1024;
};

$("new-btn").onclick = () => { $("create-error").textContent = ""; $("create").hidden = false; };
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => ($("create").hidden = true)));
$("create-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    template: f.get("template"),
    owner: f.get("owner") || null,
    name: f.get("name") || null,
    image: f.get("image") || null,
    cpu: Number(f.get("cpu")) || null,
    memory_mb: Number(f.get("memory_mb")) || null,
    ttl_seconds: (Number(f.get("ttl_min")) || 60) * 60,
  };
  try {
    const sb = await api("/api/v1/sandboxes", { method: "POST", body: JSON.stringify(body) });
    $("create").hidden = true;
    e.target.reset();
    await refresh();
    openDrawer(sb.id);
  } catch (err) { $("create-error").textContent = err.message; }
};
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !(document.activeElement === scr && $("d-takeover").checked)) {
    $("create").hidden = true; closeDrawer();
  }
});
window.addEventListener("resize", () => { renderSummary(); if (state.openId) refreshDrawer().catch(() => {}); });

// ---------------------------------------------------------------- boot
(async () => {
  const saved = getToken();
  if (saved) {
    try { await login(saved); return; } catch { setToken(null); }
  }
  $("login").hidden = false;
})();
