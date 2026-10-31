// swsdb console. Talks only to this server's /v1 API. Every API value is rendered as text (textContent), never HTML.

const $ = (sel) => document.querySelector(sel);
const NS = "http://www.w3.org/2000/svg";
const state = {
  tenant: localStorage.getItem("swsdb.tenant") || "default",
  collections: [],
  latency: {},          // engine -> recent probe latencies, for the sparklines
  timers: [],           // per-page timers, cleared on navigation
  offline: false,
};

// ------------------------------------------------------------------ DOM helpers (text only)

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  return append(el, children);
}
function append(el, children) {
  for (const c of [children].flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
// replace an element's children; arrays are flattened and empty values skipped (like h())
function fill(el, ...children) { el.replaceChildren(); return append(el, children); }
function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) el.setAttribute(k, String(v));
  for (const c of children.flat()) if (c) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

// ------------------------------------------------------------------ icons (stroke, 24x24)

const ICONS = {
  overview: [["rect", { x: 3, y: 3, width: 7.5, height: 7.5, rx: 2 }], ["rect", { x: 13.5, y: 3, width: 7.5, height: 7.5, rx: 2 }],
             ["rect", { x: 3, y: 13.5, width: 7.5, height: 7.5, rx: 2 }], ["rect", { x: 13.5, y: 13.5, width: 7.5, height: 7.5, rx: 2 }]],
  database: [["ellipse", { cx: 12, cy: 5.5, rx: 8, ry: 2.8 }], ["path", { d: "M4 5.5v6.5c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8V5.5" }],
             ["path", { d: "M4 12v6.5c0 1.5 3.6 2.8 8 2.8s8-1.3 8-2.8V12" }]],
  search: [["circle", { cx: 11, cy: 11, r: 7 }], ["path", { d: "M20.5 20.5 16 16" }]],
  graph: [["circle", { cx: 6, cy: 6, r: 2.5 }], ["circle", { cx: 18, cy: 7, r: 2.5 }], ["circle", { cx: 12, cy: 18, r: 2.5 }],
          ["path", { d: "M8.3 7.1 15.6 7.6M7.3 8.2l3.5 7.6M16.8 9.3l-3.5 6.6" }]],
  eval: [["path", { d: "M3 21h18" }], ["rect", { x: 5, y: 11, width: 3.5, height: 7, rx: 1 }],
         ["rect", { x: 10.25, y: 6, width: 3.5, height: 12, rx: 1 }], ["rect", { x: 15.5, y: 3, width: 3.5, height: 15, rx: 1 }]],
  doctor: [["path", { d: "M3 12h4l2.5-6 5 12 2.5-6h4" }]],
  check: [["path", { d: "m5 12.5 4.5 4.5L19 7.5" }]],
  x: [["path", { d: "M6 6l12 12M18 6 6 18" }]],
  alert: [["path", { d: "M12 3.5 21.5 20h-19z" }], ["path", { d: "M12 10v4.5M12 17.4v.1" }]],
  info: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 11v5.5M12 7.6v.1" }]],
  upload: [["path", { d: "M12 15.5V4M7 8.5 12 4l5 4.5M4.5 15v3.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V15" }]],
  trash: [["path", { d: "M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5.5M14 11v5.5" }]],
  refresh: [["path", { d: "M20 12a8 8 0 1 1-2.6-5.9M20 4.5V9h-4.5" }]],
  key: [["circle", { cx: 8, cy: 15, r: 4 }], ["path", { d: "m11 12 9-9M16.5 6.5 19 9M14 9l2 2" }]],
  lock: [["rect", { x: 5, y: 10.5, width: 14, height: 10, rx: 2.5 }], ["path", { d: "M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5" }]],
  sun: [["circle", { cx: 12, cy: 12, r: 4 }], ["path", { d: "M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4" }]],
  moon: [["path", { d: "M20.5 13.5A8.5 8.5 0 1 1 10.5 3.5a6.5 6.5 0 0 0 10 10z" }]],
  play: [["path", { d: "M7 5v14l11-7z" }]],
  file: [["path", { d: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" }], ["path", { d: "M14 3v5h5M9 13h6M9 17h4" }]],
  vector: [["circle", { cx: 12, cy: 12, r: 2 }], ["path", { d: "M12 4v3M12 17v3M4 12h3M17 12h3M6.3 6.3l2.1 2.1M15.6 15.6l2.1 2.1M6.3 17.7l2.1-2.1M15.6 8.4l2.1-2.1" }]],
  clock: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 7v5l3.5 2" }]],
  shield: [["path", { d: "M12 3 4.5 6v5.5c0 4.6 3.2 8.4 7.5 9.5 4.3-1.1 7.5-4.9 7.5-9.5V6z" }], ["path", { d: "m9 12 2 2 4-4" }]],
  zap: [["path", { d: "M13 2.5 4.5 13.5H12l-1 8 8.5-11H12z" }]],
  plus: [["path", { d: "M12 5v14M5 12h14" }]],
  spark: [["path", { d: "M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6" }]],
  link: [["path", { d: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" }]],
  wifi: [["path", { d: "M2.5 9a14 14 0 0 1 19 0M5.5 12.5a9.5 9.5 0 0 1 13 0M8.5 16a5 5 0 0 1 7 0M12 19.5v.1" }]],
  trophy: [["path", { d: "M8 4h8v5a4 4 0 0 1-8 0zM8 6H4.5a3 3 0 0 0 3.5 4M16 6h3.5a3 3 0 0 1-3.5 4M12 13v4M8.5 20.5h7M10 17h4v3.5h-4z" }]],
  cloud: [["path", { d: "M7 18.5h10.5a4 4 0 0 0 .6-7.96A6 6 0 0 0 6.6 9.1 4.75 4.75 0 0 0 7 18.5z" }]],
  archive: [["rect", { x: 3, y: 4, width: 18, height: 4.5, rx: 1.2 }], ["path", { d: "M5 8.5V19a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 19V8.5M10 12.5h4" }]],
  download: [["path", { d: "M12 4v11.5M7 11l5 4.5 5-4.5M4.5 15v3.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V15" }]],
  wand: [["path", { d: "m4 20 11-11M14 4v2M19 9h2M17.5 5.5 19 4M9 3l.5 1.5L11 5l-1.5.5L9 7l-.5-1.5L7 5l1.5-.5z" }]],
};
function icon(name, cls = "") {
  return s("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 1.8,
                    "stroke-linecap": "round", "stroke-linejoin": "round", class: cls || null, "aria-hidden": "true" },
    (ICONS[name] || []).map(([t, a]) => s(t, a)));
}

// ------------------------------------------------------------------ small components

const STATUS = { ready: "ok", processing: "info", queued: "info", failed: "bad", deleting: "warn", starting: "info",
                 down: "bad", degraded: "warn" };
const pill = (text, kind = "", dot = true) => h("span", { class: `pill ${kind}` }, dot ? h("span", { class: "d" }) : null, text);
const status = (st) => pill(st, STATUS[st] || "");
const fmt = (n, d = 3) => (n === null || n === undefined ? "-" : Number(n).toFixed(d));
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function rel(iso) {
  if (!iso) return "-";
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 45) return "just now";
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function card(...children) { return h("div", { class: "card" }, children); }
function cardHead(title, sub, ...actions) {
  return h("div", { class: "card-head" }, h("div", {}, h("h2", {}, title), sub ? h("div", { class: "sub" }, sub) : null),
    actions.length ? h("div", { class: "row" }, actions) : null);
}
function btn(label, onclick, { kind = "ghost", ico, title } = {}) {
  return h("button", { class: `btn ${kind}${label ? "" : " icon"}`, type: "button", onclick, title, "aria-label": title },
    ico ? icon(ico) : null, label);
}
function empty(ico, title, text, ...actions) {
  return h("div", { class: "empty" }, icon(ico), h("b", {}, title), text ? h("div", {}, text) : null,
    actions.length ? h("div", { class: "row" }, actions) : null);
}
function skeleton(kind = "block", n = 1) {
  return Array.from({ length: n }, () => h("div", { class: `skeleton ${kind}` }));
}
function meter(value, max = 1, kind = "") {
  const bar = h("i");
  bar.style.width = `${Math.max(0, Math.min(1, value / (max || 1))) * 100}%`;
  return h("div", { class: `meter ${kind}` }, h("span", {}, fmt(value)), h("div", { class: "bar" }, bar));
}
function table(columns, rows, { onRow, selected, emptyView } = {}) {
  if (!rows.length) return emptyView || empty("info", "Nothing here yet");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
    h("tbody", {}, rows.map((r) => h("tr", {
      class: [onRow ? "click" : "", selected && selected(r) ? "selected" : "", r.__best ? "best" : ""].join(" ").trim() || null,
      onclick: onRow ? () => onRow(r) : null,
    }, columns.map((c) => h("td", { class: c.num ? "num" : c.mono ? "mono" : "" }, c.get(r))))))));
}
function sparkline(values) {
  const svg = s("svg", { class: "spark", viewBox: "0 0 100 36", preserveAspectRatio: "none" });
  if (values.length < 2) return svg;
  const max = Math.max(...values) * 1.15 || 1;
  const pts = values.map((v, i) => [(i / (values.length - 1)) * 100, 34 - (v / max) * 30]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const grad = s("defs", {}, s("linearGradient", { id: "sparkfill", x1: 0, y1: 0, x2: 0, y2: 1 },
    s("stop", { offset: "0", "stop-color": "#8b7bff", "stop-opacity": ".35" }),
    s("stop", { offset: "1", "stop-color": "#8b7bff", "stop-opacity": "0" })));
  svg.append(grad, s("path", { d: `M0,36 L${line.replaceAll(" ", " L")} L100,36 Z` }), s("polyline", { points: line }));
  return svg;
}
function ring(fraction, kind, big, small) {
  const r = 50, c = 2 * Math.PI * r;
  const val = s("circle", { cx: 60, cy: 60, r, class: "val", "stroke-dasharray": c.toFixed(1),
                            "stroke-dashoffset": (c * (1 - Math.max(0, Math.min(1, fraction)))).toFixed(1) });
  return h("div", { class: "ring-wrap" },
    s("svg", { class: `ring ${kind}`, viewBox: "0 0 120 120" }, s("circle", { cx: 60, cy: 60, r, class: "track" }), val),
    h("div", { class: "center" }, h("div", {}, h("b", {}, big), h("span", { class: "tiny muted" }, small))));
}
const STOP = new Set(["the", "and", "for", "are", "was", "were", "what", "who", "how", "why", "when", "where", "which",
  "does", "did", "can", "with", "from", "this", "that", "have", "has", "our", "you", "your", "about", "into", "they"]);
function highlight(text, query) {
  const words = [...new Set((query || "").toLowerCase().match(/[\p{L}\p{N}]{3,}/gu) || [])].filter((w) => !STOP.has(w));
  if (!words.length || !text) return [text || ""];
  const re = new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "giu");
  return text.split(re).map((part, i) => (i % 2 ? h("mark", {}, part) : part));
}

function toast(message, kind = "ok") {
  const t = h("div", { class: `toast ${kind}` }, icon(kind === "bad" ? "alert" : kind === "info" ? "info" : "check"),
    h("div", {}, message));
  $("#toasts").append(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, 3800);
}

// ------------------------------------------------------------------ API

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(method, path, body) {
  const headers = { "swsdb-tenant": state.tenant };
  const key = sessionStorage.getItem("swsdb.key");
  if (key) headers["api-key"] = key;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["content-type"] = "application/json"; payload = JSON.stringify(body); }
  let r;
  try {
    r = await fetch(path, { method, headers, body: payload });
  } catch {
    throw new ApiError(0, "offline", "can't reach the swsdb server");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 401) openKeyDialog();
    throw new ApiError(r.status, data.error || "error", data.message || r.statusText);
  }
  return data;
}

function showError(e) {
  const b = $("#error");
  if (!e) { b.classList.add("hidden"); return; }
  fill(b, icon("alert"), h("span", {}, e instanceof ApiError ? `${e.message} (${e.status} ${e.code})` : String(e)));
  b.classList.remove("hidden");
}

async function guard(fn) {
  try { showError(null); return await fn(); } catch (e) {
    if (e instanceof ApiError && e.status === 0) { goOffline(); return undefined; }
    showError(e); return undefined;
  }
}

async function documentCollections() {
  const { collections } = await api("GET", "/v1/collections");
  const infos = await Promise.all(collections.map((c) =>
    api("GET", `/v1/collections/${encodeURIComponent(c.name)}`).catch(() => null)));
  state.collections = collections.map((c, i) => ({ ...c, info: infos[i] })).filter((c) => c.info && c.info.documents);
  return state.collections;
}
const collPath = (name, rest = "") => `/v1/collections/${encodeURIComponent(name)}${rest}`;

function collectionSelect(current, onChange) {
  return h("select", { onchange: (e) => onChange(e.target.value), "aria-label": "Collection" },
    state.collections.map((c) => h("option", { value: c.name, selected: c.name === current }, c.name)));
}
function noCollections() {
  return empty("database", "No document collections yet",
    "Create one with the SDK, e.g. db.create_collection(\"handbook\", documents=True), then add documents here.");
}

// ------------------------------------------------------------------ charts (one series each: the accent hue, values in text ink)

const num = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString());

// horizontal bars for "how many of each": a label, a bar scaled to the largest value, the count
function barList(items, { onPick, active } = {}) {
  const max = Math.max(1, ...items.map((i) => i.count));
  return h("div", { class: "barlist" }, items.map((i) => {
    const fillBar = h("i");
    fillBar.style.width = `${Math.max(2, (i.count / max) * 100)}%`;
    const isOn = active && active(i);
    return h(onPick ? "button" : "div", { class: `bar-row${onPick ? " pick" : ""}${isOn ? " on" : ""}`, type: onPick ? "button" : null,
      title: `${i.label}: ${num(i.count)}`, onclick: onPick ? () => onPick(i) : null },
      h("span", { class: "bar-label ellipsis" }, i.label), h("span", { class: "bar-track" }, fillBar),
      h("span", { class: "bar-value" }, num(i.count)));
  }));
}

// vertical bars over time (minutes, days): hover shows the value; sparse axis labels; a baseline
function columnChart(points, { label = (p) => p.x, height = 120, unit = "", width = 600 } = {}) {
  const W = width, H = height, pad = { l: 34, r: 6, t: 8, b: 20 };
  const max = Math.max(1, ...points.map((p) => p.y));
  const pow = 10 ** Math.floor(Math.log10(max));
  const nice = [1, 2, 2.5, 5, 10].map((m) => m * pow).find((v) => v >= max);
  const bw = (W - pad.l - pad.r) / Math.max(points.length, 1);
  const svg = s("svg", { class: "colchart", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img" });
  for (const f of Number.isInteger(nice / 2) ? [0, 0.5, 1] : [0, 1]) {
    const y = pad.t + (H - pad.t - pad.b) * (1 - f);
    svg.append(s("line", { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: f ? "grid" : "base" }),
      s("text", { x: pad.l - 6, y: y + 3, "text-anchor": "end", class: "axis" }, num(Math.round(nice * f))));
  }
  const every = Math.ceil(points.length / (W < 400 ? 4 : 6));
  points.forEach((p, i) => {
    const hgt = ((H - pad.t - pad.b) * p.y) / nice;
    const x = pad.l + i * bw;
    const g = s("g", { class: "col" }, s("title", {}, `${label(p)}: ${num(p.y)}${unit}`),
      s("rect", { x: x, y: pad.t, width: bw, height: H - pad.t - pad.b, class: "hit" }),
      p.y ? s("rect", { x: x + Math.min(1, bw * 0.15), y: H - pad.b - hgt, width: Math.max(1, bw - Math.min(2, bw * 0.3)), height: hgt,
                        rx: Math.min(2, bw / 3), class: "val" }) : null);
    svg.append(g);
    if (i % every === 0) svg.append(s("text", { x: x + bw / 2, y: H - 5, "text-anchor": "middle", class: "axis" }, label(p)));
  });
  return svg;
}

// ------------------------------------------------------------------ pages

const PAGES = {
  overview: { title: "Overview", icon: "overview", sub: "Health of the engines, how far every store is behind, and your collections." },
  collections: { title: "Collections", icon: "database", sub: "Documents, their status in every store, and why anything isn't searchable." },
  search: { title: "Search", icon: "search", sub: "Ask questions as any user and see how each passage was found." },
  graph: { title: "Graph explorer", icon: "graph", sub: "Entities extracted from your documents, how they relate and where they're mentioned." },
  reports: { title: "Reports", icon: "eval", sub: "Saved queries pinned as live charts: your own dashboard, run fresh each time." },
  data: { title: "Data", icon: "file", sub: "Look at the raw data: tables row by row, SQL, the graph with Cypher, and every vector point. Through the API, with your key's permissions." },
  eval: { title: "Evaluation", icon: "eval", sub: "Measure retrieval on your own questions and compare configurations with numbers." },
  advisors: { title: "Advisors", icon: "shield", sub: "Security and performance checks of this server and tenant, each with what to do about it." },
  logs: { title: "Logs", icon: "clock", sub: "Recent API requests on this server and where the time goes, route by route." },
  users: { title: "Users", icon: "key", sub: "Your app's end users: who may sign in, their groups, and how long a sign-in lasts." },
  policies: { title: "Policies", icon: "shield", sub: "Row-level security: which tables and functions signed-in users reach, and which rows." },
  storage: { title: "Storage", icon: "archive", sub: "Files in buckets, kept in Postgres with everything else; each user owns a folder." },
  keys: { title: "API keys", icon: "key", sub: "Keys for one tenant each: make them, see when they were last used, revoke them." },
  backups: { title: "Backups", icon: "archive", sub: "Snapshots of every engine at one consistent point, scheduled backups and their copies in S3." },
  doctor: { title: "Doctor", icon: "doctor", sub: "Check that the vector store and the graph agree with Postgres, the source of truth, and repair drift." },
};
const view = {};

view.overview = async (root, _arg, actions) => {
  fill(actions, btn("Refresh", () => route(), { ico: "refresh" }));
  fill(root, h("div", { class: "grid c4" }, skeleton("block", 4)), skeleton("block", 2));
  const [st, cols, all, act] = await Promise.all([api("GET", "/v1/status"), documentCollections(),
    allCollections(), api("GET", "/v1/activity").catch(() => null)]);
  for (const e of Object.values(st.engines)) {
    const hist = (state.latency[e.engine] ||= []);
    if (e.latency_ms !== null && e.latency_ms !== undefined) { hist.push(e.latency_ms); if (hist.length > 40) hist.shift(); }
  }
  const totals = cols.reduce((a, c) => {
    for (const [k, v] of Object.entries(c.info.documents.by_status || {})) a[k] = (a[k] || 0) + v;
    return a;
  }, {});
  const vectors = all.reduce((a, c) => a + (c.info && c.info.vector ? c.info.vector.points || 0 : 0), 0);
  const nodes = all.reduce((a, c) => a + (c.info && c.info.graph ? c.info.graph.nodes || 0 : 0), 0);
  const docsTotal = cols.reduce((a, c) => a + c.info.documents.count, 0);
  const sync = st.sync || {};
  const stat = (ico, color, label, value, foot) => card(h("div", { class: "stat-row" },
    h("div", { class: "stat" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
      h("div", { class: "foot" }, foot)), h("div", { class: `icon-tile ${color}` }, icon(ico))));

  const engineCard = ([cap, e]) => card(
    h("div", { class: "row between" },
      h("div", { class: "row" }, h("div", { class: `icon-tile ${{ sql: "blue", vector: "violet", graph: "amber" }[cap] || ""}` },
        icon({ sql: "database", vector: "vector", graph: "graph" }[cap] || "database")),
        h("div", {}, h("b", {}, e.engine), h("div", { class: "tiny muted" }, `${cap} · ${e.version || "unknown version"}`))),
      status(e.state)),
    h("div", { class: "row between small" }, h("span", { class: "muted" }, "probe latency"),
      h("b", {}, e.latency_ms !== null && e.latency_ms !== undefined ? `${e.latency_ms} ms` : "-")),
    sparkline(state.latency[e.engine] || []),
    e.error ? h("div", { class: "tiny muted ellipsis", title: e.error }, e.error) : null,
    e.restarts_seen ? h("div", { class: "tiny muted" }, `${plural(e.restarts_seen, "restart")} seen since start`) : null);

  const pending = sync.pending ?? 0;
  const consistency = card(cardHead("Consistency", "Changes accepted by Postgres, still being applied to the vector store and the graph"),
    h("div", { class: "row" },
      ring(pending ? 0.25 : 1, pending ? (sync.retrying ? "bad" : "warn") : "", pending ? String(pending) : "✓",
           pending ? "pending" : "in sync"),
      h("dl", { class: "kv grow" },
        h("dt", {}, "Waiting"), h("dd", {}, plural(pending, "change")),
        h("dt", {}, "Retrying"), h("dd", {}, sync.retrying ? pill(String(sync.retrying), "warn") : "none"),
        h("dt", {}, "Oldest"), h("dd", {}, sync.oldest_s !== null && sync.oldest_s !== undefined ? `${sync.oldest_s}s` : "-"),
        h("dt", {}, "Reconciled"), h("dd", {}, sync.last_reconcile
          ? [rel(sync.last_reconcile.at), " · ", plural(sync.last_reconcile.issues, "issue")] : "not yet (every 10 min)"))));

  const minuteLabel = (p) => new Date(p.x * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const activity = act ? card(cardHead("Searches and indexing", "This server, the last hour (it starts again when the server restarts)"),
    h("div", { class: "metric-row" },
      h("div", { class: "metric" }, h("b", {}, num(act.searches)), h("span", {}, "searches")),
      h("div", { class: "metric" }, h("b", {}, act.latency_ms.p50 !== null ? `${act.latency_ms.p50} ms` : "no searches"),
        h("span", {}, act.latency_ms.p95 !== null ? `median search · p95 ${act.latency_ms.p95} ms` : "search time shows here")),
      h("div", { class: "metric" }, h("b", {}, num(act.indexed)), h("span", {}, act.failed ? `indexed · ${act.failed} failed` : "documents indexed"))),
    h("div", { class: "grid c2 charts-row" },
      h("div", {}, h("h3", { class: "chart-title" }, "Searches per minute"),
        columnChart(act.minutes.map((m) => ({ x: m.minute, y: m.search })), { label: minuteLabel, unit: " searches", width: 340 })),
      h("div", {}, h("h3", { class: "chart-title" }, "Documents indexed per minute"),
        columnChart(act.minutes.map((m) => ({ x: m.minute, y: m.indexed })), { label: minuteLabel, unit: " documents", width: 340 })))) : null;

  const collCard = (c) => {
    const d = c.info.documents;
    const by = d.by_status || {};
    const bar = h("div", { class: "stackbar" });
    for (const k of ["ready", "processing", "queued", "failed", "deleting"]) {
      if (!by[k]) continue;
      const seg = h("i", { class: `s-${k}`, title: `${by[k]} ${k}` });
      seg.style.width = `${(by[k] / (d.count || 1)) * 100}%`;
      bar.append(seg);
    }
    return h("div", { class: "card hover coll-card", onclick: () => { location.hash = `collections/${c.name}`; } },
      h("div", { class: "row between" }, h("div", { class: "name" }, c.name),
        d.graph ? pill("documents + graph", "violet", false) : pill("documents", "", false)),
      h("div", { class: "nums" },
        h("div", {}, h("b", {}, num(d.count)), h("span", {}, "documents")),
        h("div", {}, h("b", {}, num(c.info.vector ? c.info.vector.points : 0)), h("span", {}, "vectors")),
        h("div", {}, h("b", {}, num(c.info.graph ? c.info.graph.nodes : 0)), h("span", {}, "graph nodes"))),
      bar,
      h("div", { class: "row between" }, h("div", { class: "legend" },
        Object.entries(by).map(([k, v]) => h("span", {}, h("i", { class: `s-${k}` }), `${num(v)} ${k}`))),
        h("span", { class: "tiny muted" }, String(d.embedding || "").replace("local:", ""))));
  };
  // collections you manage yourself (raw vectors, graphs): open them in the data browser
  const rawCard = (c) => {
    const v = c.info && c.info.vector, g = c.info && c.info.graph;
    return h("div", { class: "card hover coll-card", onclick: () => { location.hash = v ? "data/vectors" : "data/cypher"; } },
      h("div", { class: "row between" }, h("div", { class: "name" }, c.name),
        pill(v && g ? "vectors + graph" : v ? "vectors" : "graph", "", false)),
      h("div", { class: "nums" },
        h("div", {}, h("b", {}, num(v ? v.points : 0)), h("span", {}, "points")),
        h("div", {}, h("b", {}, v ? v.size : "-"), h("span", {}, v ? `dimensions · ${v.distance}` : "dimensions")),
        h("div", {}, h("b", {}, num(g ? g.nodes : 0)), h("span", {}, "graph nodes"))));
  };
  const raws = all.filter((c) => c.info && !c.info.documents);

  fill(root,
    h("div", { class: "grid c4" },
      stat("file", "violet", "Documents", num(docsTotal), `${num(totals.ready || 0)} ready · ${num(totals.failed || 0)} failed`),
      stat("vector", "blue", "Vectors", num(vectors), plural(all.filter((c) => c.vector).length, "collection")),
      stat("graph", "amber", "Graph nodes", num(nodes), plural(all.filter((c) => c.graph).length, "graph")),
      stat("zap", pending ? "amber" : "green", "Sync backlog", num(pending), pending ? "being applied" : "every store in sync")),
    h("div", { class: "section-title" }, "Engines"),
    h("div", { class: "grid c3" }, Object.entries(st.engines).map(engineCard)),
    h("div", { class: "split" },
      h("div", { class: "stack" }, h("div", { class: "section-title" }, "Activity"), activity || card(h("div", { class: "card-pad muted small" }, "Activity needs a key from the environment or the tenant's admin key."))),
      h("div", { class: "stack" }, h("div", { class: "section-title" }, "Sync"), consistency)),
    h("div", { class: "section-title" }, "Collections"),
    all.length ? h("div", { class: "grid c3" }, cols.map(collCard), raws.map(rawCard)) : noCollections());
  state.timers.push(setTimeout(() => { if (currentPage() === "overview") route(true); }, 5000));
};

async function allCollections() {
  const { collections } = await api("GET", "/v1/collections");
  const infos = await Promise.all(collections.map((c) => api("GET", collPath(c.name)).catch(() => null)));
  return collections.map((c, i) => ({ ...c, info: infos[i] }));
}

const metaText = (v) => (typeof v === "string" ? v : JSON.stringify(v));

view.collections = async (root, arg, actions) => {
  fill(root, skeleton("block", 2));
  const cols = await documentCollections();
  if (!cols.length) { fill(root, noCollections()); return; }
  const name = cols.some((c) => c.name === arg) ? arg : cols[0].name;
  const PAGE = 100;
  let statusFilter = "", text = "", sort = "created", offset = 0, selected = null, meta = {}, facets = null;
  let page = { documents: [], total: 0 };
  const listCard = h("div", { class: "card flush" });
  const summary = h("div", { class: "stack" });
  let typing;
  const filterInput = h("input", { placeholder: "Search ids and titles", class: "grow", "aria-label": "Search documents",
    oninput: (e) => { clearTimeout(typing); typing = setTimeout(() => { text = e.target.value.trim(); offset = 0; load(); }, 250); } });
  const statusSel = h("select", { onchange: (e) => { statusFilter = e.target.value; offset = 0; load(); }, "aria-label": "Status" },
    ["", "ready", "processing", "queued", "failed", "deleting"].map((v) => h("option", { value: v }, v || "All statuses")));
  const sortSel = h("select", { onchange: (e) => { sort = e.target.value; offset = 0; load(); }, "aria-label": "Sort" },
    [["created", "Oldest first"], ["updated", "Recently changed"], ["id", "By id"], ["chunks", "Most passages"]]
      .map(([v, l]) => h("option", { value: v }, l)));
  // the metadata fields worth a column: the most common ones
  const metaCols = () => (facets ? Object.keys(facets.fields).slice(0, 2) : []);

  function toggleMeta(field, value) {
    if (field in meta && metaText(meta[field]) === metaText(value)) delete meta[field]; else meta[field] = value;
    offset = 0; load(); loadFacets();
  }
  function draw() {
    const rows = page.documents;
    const from = page.total ? offset + 1 : 0, to = offset + rows.length;
    const active = Object.entries(meta);
    fill(listCard,
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, name),
        h("div", { class: "sub" }, page.total ? `${num(from)}–${num(to)} of ${num(page.total)} documents` : "no documents match")),
        h("div", { class: "row" }, filterInput, statusSel, sortSel)),
      active.length ? h("div", { class: "filters" }, active.map(([k, v]) =>
        h("button", { class: "chip", type: "button", title: "Remove this filter", onclick: () => toggleMeta(k, v) }, `${k} = ${metaText(v)}  ×`))) : null,
      table([
        { label: "Document", get: (d) => h("div", {}, h("div", { class: "mono" }, d.id), d.title ? h("div", { class: "tiny muted" }, d.title) : null) },
        ...metaCols().map((k) => ({ label: k, get: (d) => (d.metadata && k in d.metadata
          ? h("span", { class: "small" }, metaText(d.metadata[k])) : h("span", { class: "muted" }, "-")) })),
        { label: "Status", get: (d) => h("div", { class: "row" }, status(d.status), d.attempts ? pill(`retry ${d.attempts}`, "warn", false) : null) },
        { label: "Version", num: true, get: (d) => (d.live_version && d.live_version !== d.version ? `${d.live_version} → ${d.version}` : d.version) },
        { label: "Chunks", num: true, get: (d) => d.chunks },
        { label: "Entities", num: true, get: (d) => d.entities },
        { label: "Updated", num: true, get: (d) => h("span", { class: "muted", title: new Date(d.updated_at).toLocaleString() }, rel(d.updated_at)) },
      ], rows, { onRow: (d) => { selected = d.id; draw(); openDiagnosis(name, d.id, load); }, selected: (d) => d.id === selected,
                 emptyView: h("div", { class: "card-pad" }, empty("file", "No documents match", "Change the search or the filters, or add documents.")) }),
      page.total > PAGE ? h("div", { class: "row between card-pad" },
        btn("Previous", () => { offset = Math.max(0, offset - PAGE); load(); }),
        h("span", { class: "small muted" }, `page ${Math.floor(offset / PAGE) + 1} of ${Math.ceil(page.total / PAGE)}`),
        btn("Next", () => { if (offset + PAGE < page.total) { offset += PAGE; load(); } })) : null);
  }
  function drawSummary() {
    if (!facets) return;
    const t = facets.totals;
    const fields = Object.entries(facets.fields);
    const filtered = Object.keys(meta).length;
    fill(summary, card(cardHead(filtered ? "In the filtered documents" : "What's inside",
      filtered ? "Counts within the current filters; click a value to add or remove it" : "Click a value to filter the list"),
      h("div", { class: "card-pad stack" },
        h("div", { class: "metric-row" },
          h("div", { class: "metric" }, h("b", {}, num(t.documents)), h("span", {}, "documents")),
          h("div", { class: "metric" }, h("b", {}, num(t.chunks)), h("span", {}, "passages")),
          h("div", { class: "metric" }, h("b", {}, num(t.entities)), h("span", {}, "entity mentions"))),
        facets.added_by_day.length > 1 ? h("div", { class: "facet" }, h("h3", {}, "Added per day"),
          columnChart(facets.added_by_day.map((d) => ({ x: d.day, y: d.documents })), { label: (p) => p.x.slice(5), unit: " documents" })) : null,
        fields.length ? fields.slice(0, 5).map(([k, f]) => h("div", { class: "facet" },
          h("h3", {}, `${k} · ${num(f.documents)} documents${f.more ? " · top values" : ""}`),
          barList(f.values.map((v) => ({ label: metaText(v.value), count: v.count, value: v.value })),
            { onPick: (i) => toggleMeta(k, i.value), active: (i) => k in meta && metaText(meta[k]) === metaText(i.value) })))
          : h("div", { class: "small muted" }, "No metadata yet. Add it when you upload, e.g. {\"team\": \"support\"}, to filter here and in search."))),
      uploadCard(name, () => { load(); loadFacets(); }));
  }
  async function load() {
    const params = new URLSearchParams({ limit: PAGE, offset, sort });
    if (statusFilter) params.set("status", statusFilter);
    if (text) params.set("q", text);
    if (Object.keys(meta).length) params.set("meta", JSON.stringify(meta));
    const r = await guard(() => api("GET", collPath(name, `/documents?${params}`)));
    if (r) { page = r; draw(); }
  }
  async function loadFacets() {
    const params = new URLSearchParams();
    if (Object.keys(meta).length) params.set("meta", JSON.stringify(meta));
    facets = await guard(() => api("GET", collPath(name, `/facets?${params}`)));
    drawSummary(); draw();
  }

  fill(actions, collectionSelect(name, (v) => { location.hash = `collections/${v}`; }), btn("Refresh", () => { load(); loadFacets(); }, { ico: "refresh" }));
  fill(root, h("div", { class: "split" }, listCard, summary));
  await Promise.all([load(), loadFacets()]);
};

function uploadCard(name, reload) {
  let mode = "file";
  const body = h("div", { class: "stack tight" });
  const tabs = h("div", { class: "tabs" });
  const fileInput = h("input", { type: "file", class: "hidden", multiple: true,
                                 accept: ".pdf,.md,.markdown,.txt,.html,.htm,.csv,.json,.rst" });
  async function send(file) {
    const fd = new FormData(); fd.append("file", file);
    if (await guard(() => api("POST", collPath(name, "/documents"), fd))) { toast(`Queued ${file.name}`); reload(); }
  }
  fileInput.addEventListener("change", () => { for (const f of fileInput.files) send(f); fileInput.value = ""; });
  function draw() {
    fill(tabs, ["file", "text"].map((m) => h("button", { class: m === mode ? "on" : null, type: "button",
      onclick: () => { mode = m; draw(); } }, m === "file" ? "Upload file" : "Paste text")));
    if (mode === "file") {
      const dz = h("div", { class: "dropzone", onclick: () => fileInput.click() }, icon("upload"),
        h("b", {}, "Drop files here or click to choose"), h("div", { class: "tiny" }, "PDF, Markdown, HTML or text"));
      dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
      dz.addEventListener("dragleave", () => dz.classList.remove("over"));
      dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); for (const f of e.dataTransfer.files) send(f); });
      fill(body, dz, fileInput);
    } else {
      const id = h("input", { placeholder: "Document id, e.g. refund-policy", "aria-label": "Document id" });
      const title = h("input", { placeholder: "Title (optional)", "aria-label": "Title" });
      const txt = h("textarea", { placeholder: "# Refunds\n\nRefunds are issued within five business days.", "aria-label": "Text" });
      fill(body, id, title, txt, h("div", { class: "row end" }, btn("Add document", async () => {
        if (!id.value.trim() || !txt.value.trim()) { showError("Give the document an id and some text."); return; }
        const r = await guard(() => api("POST", collPath(name, "/documents/text"),
          { id: id.value.trim(), text: txt.value, title: title.value.trim() || null }));
        if (r) { toast(r.unchanged ? `${r.id} is unchanged` : `Queued ${r.id}`); id.value = ""; title.value = ""; txt.value = ""; reload(); }
      }, { kind: "primary", ico: "plus" })));
    }
  }
  draw();
  return card(cardHead("Add documents", "Indexed in the background: chunked, embedded and graphed"), h("div", { class: "stack tight" }, tabs, body));
}

async function openDiagnosis(name, id, reload) {
  closeDrawer();
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const drawer = h("aside", { class: "drawer", role: "dialog", "aria-label": `Diagnosis of ${id}` }, skeleton("block", 3));
  document.body.append(scrim, drawer);
  const r = await guard(() => api("GET", collPath(name, `/diagnose/${encodeURIComponent(id)}`)));
  if (!r) { closeDrawer(); return; }
  const d = r.document;
  const kind = r.verdict === "searchable" ? "ok" : r.verdict.startsWith("not") ? "bad" : "warn";
  const step = (label, okay, detail, off) => h("div", { class: `pipe-step ${off ? "off" : okay ? "ok" : "bad"}` },
    h("div", { class: "ico" }, icon(off ? "info" : okay ? "check" : "x")), h("b", {}, label), h("span", {}, detail));
  const vecOk = !r.vectors.error && d.live_version && r.vectors.live === d.chunks;
  const graphOff = !r.graph;
  const graphOk = r.graph && !r.graph.error && r.graph.chunks === d.chunks;
  fill(drawer,
    h("div", { class: "row between" }, h("div", {}, h("div", { class: "tiny muted" }, `${name} /`), h("h2", { class: "mono" }, d.id)),
      btn("", closeDrawer, { ico: "x", title: "Close" })),
    h("div", { class: `verdict-hero ${kind}` }, icon(kind === "ok" ? "shield" : "alert"),
      h("div", {}, h("b", {}, r.verdict), h("div", { class: "msg" }, r.reasons[0]))),
    h("div", { class: "pipeline" },
      step("Catalog", d.status === "ready", `${d.status} · v${d.version}`),
      step("Vectors", vecOk, r.vectors.error ? "unreachable" : `${r.vectors.live}/${d.chunks} live`),
      step("Graph", graphOk, graphOff ? "not used" : r.graph.error ? "unreachable" : `${r.graph.chunks}/${d.chunks} chunks`, graphOff)),
    r.reasons.length > 1 ? h("div", { class: "stack tight" }, h("div", { class: "section-title" }, "Details"),
      h("ul", { class: "reasons" }, r.reasons.slice(1).map((x) => h("li", {}, icon("info"), h("span", {}, x))))) : null,
    h("div", { class: "section-title" }, "Document"),
    h("dl", { class: "kv" },
      h("dt", {}, "Status"), h("dd", {}, status(d.status)),
      h("dt", {}, "Version"), h("dd", {}, `${d.version} (queries return ${d.live_version ?? "none yet"})`),
      h("dt", {}, "Chunks"), h("dd", {}, d.chunks),
      h("dt", {}, "Entities"), h("dd", {}, d.entities),
      h("dt", {}, "Readers"), h("dd", { class: "row" }, d.readers.map((x) => pill(x === "*" ? "everyone" : x, "", false))),
      h("dt", {}, "Embedding"), h("dd", {}, r.collection.embedding),
      h("dt", {}, "Updated"), h("dd", {}, new Date(d.updated_at).toLocaleString()),
      d.error ? [h("dt", {}, "Error"), h("dd", {}, h("div", { class: "codeblock" }, d.error))] : null),
    r.pending.length ? [h("div", { class: "section-title" }, "Pending changes"),
      h("div", { class: "card flush" }, table([
        { label: "Change", get: (p) => h("b", {}, p.op) }, { label: "v", num: true, get: (p) => p.version },
        { label: "Attempts", num: true, get: (p) => p.attempts },
        { label: "State", get: (p) => (p.last_error ? h("span", { class: "tiny", title: p.last_error }, p.last_error.slice(0, 90))
                                                    : p.running ? pill("running", "info") : pill("waiting")) },
      ], r.pending))] : null,
    h("div", { class: "row end" },
      btn("Original file", () => downloadOriginal(name, id), { ico: "download", title: "Download the file as it was uploaded" }),
      btn("Re-check", () => openDiagnosis(name, id, reload), { ico: "refresh" }),
      btn("Delete", async () => {
        if (!confirm(`Delete ${d.id} from every store? Queries stop returning it at once.`)) return;
        if (await guard(() => api("DELETE", collPath(name, `/documents/${encodeURIComponent(d.id)}`)))) {
          toast(`Deleting ${d.id}`, "info"); closeDrawer(); reload();
        }
      }, { kind: "danger", ico: "trash" })));
}
function closeDrawer() { document.querySelectorAll(".scrim, .drawer").forEach((e) => e.remove()); }

function hitCard(x, i, query, top, { n, cited } = {}) {
  const bar = h("i"); bar.style.width = `${(x.score / top) * 100}%`;
  return h("div", { class: `hit ${x.via === "graph" ? "graph" : ""} ${cited ? "cited" : ""}`, id: n ? `p-${n}` : null },
    h("div", { class: "rank" }, n || (x.via === "graph" ? icon("link") : i + 1)),
    h("div", {},
      h("div", { class: "meta" }, h("span", { class: "doc" }, x.title || x.document),
        x.title ? h("span", { class: "tiny muted mono" }, x.document) : null,
        x.page !== null && x.page !== undefined ? pill(`page ${x.page}`, "", false) : null,
        x.heading ? pill(x.heading, "", false) : null,
        x.via === "graph" ? pill(x.graph_path ? x.graph_path.join(" → ") : "graph", "violet", false)
                          : pill(x.via === "sparse" ? "keywords" : x.via === "dense" ? "meaning" : "hybrid", "info", false),
        x.fusion_score !== undefined ? pill("re-ranked", "violet", false) : null,
        cited ? pill("cited", "ok", false) : null,
        Object.entries(x.metadata || {}).slice(0, 4).map(([k, v]) => h("span", { class: "tiny muted" }, `${k}: ${metaText(v)}`)),
        h("span", { class: "score" }, fmt(x.score, 3), h("span", { class: "bar" }, bar))),
      h("div", { class: "text" }, highlight(passageText(x.text, x.heading), query))));
}

// A passage as prose: its section heading is shown as a label, so a first line repeating it is dropped
function passageText(text, heading) {
  const lines = (text || "").split("\n");
  const first = (lines[0] || "").match(/^#+\s*(.*)$/);
  const section = (heading || "").split(" > ").pop();
  if (first && (!section || first[1].trim() === section.trim())) lines.shift();
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

function answerCard(r) {
  const jump = (n) => {
    const el = document.getElementById(`p-${n}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
  };
  const parts = r.answer.split(/(\[\d+\])/g).map((part) => {
    const m = part.match(/^\[(\d+)\]$/);
    return m ? h("button", { class: "cite", type: "button", title: `Show passage ${m[1]}`, onclick: () => jump(Number(m[1])) }, m[1])
             : part;
  });
  const generated = r.mode === "generated";
  return h("div", { class: "answer" },
    h("div", { class: "row between" },
      h("div", { class: "row" }, h("div", { class: "icon-tile violet" }, icon("spark")), h("b", {}, "Answer")),
      h("div", { class: "row" }, pill(generated ? `written by ${r.model}` : "extractive", generated ? "violet" : "", false),
        h("span", { class: "tiny muted" }, `${r.took_ms} ms`))),
    h("div", { class: "answer-text" }, parts),
    r.note ? h("div", { class: "tiny muted" }, r.note) : null,
    !generated && r.cited.length ? h("div", { class: "tiny muted" },
      "The best matching sentences from your documents, cited. Set SWSDB_LLM_URL and SWSDB_LLM_MODEL to have a model write the answer.") : null);
}

view.search = async (root, _arg, actions) => {
  fill(root, skeleton("block", 1));
  const cols = await documentCollections();
  if (!cols.length) { fill(root, noCollections()); return; }
  let coll = cols[0].name;
  const results = h("div", { class: "stack" });
  const q = h("input", { placeholder: "Ask anything about your documents…", id: "q", "aria-label": "Question" });
  const mode = h("select", { "aria-label": "Mode" }, [["hybrid", "Hybrid"], ["dense", "Meaning"], ["sparse", "Keywords"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  const k = h("select", { "aria-label": "Results" }, [3, 5, 10, 20].map((n) => h("option", { value: n, selected: n === 5 }, `top ${n}`)));
  const hops = h("select", { "aria-label": "Graph" }, [["", "Graph: auto"], ["0", "No graph"], ["1", "Graph 1 hop"], ["2", "Graph 2 hops"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  const principals = h("input", { placeholder: "Ask as… user:42, group:hr", class: "grow", "aria-label": "Principals" });
  const rerank = h("select", { "aria-label": "Re-rank", title: "Re-order hits with a cross-encoder: often more precise, slower" },
    [["", "Re-rank: default"], ["true", "Re-rank: on"], ["false", "Re-rank: off"]].map(([v, l]) => h("option", { value: v }, l)));
  // filters on document metadata: only passages from documents whose fields match (all of them)
  let where = {}, facets = null;
  const filterRow = h("div", { class: "row" });
  const filterChips = h("div", { class: "chips" });
  async function loadFacets() {
    facets = await api("GET", collPath(coll, "/facets")).catch(() => null);
    drawFilters();
  }
  function drawFilters() {
    const fields = facets ? Object.entries(facets.fields) : [];
    if (!fields.length) { fill(filterRow); fill(filterChips); return; }
    const field = h("select", { "aria-label": "Filter field" }, h("option", { value: "" }, "Filter by…"),
      fields.map(([k]) => h("option", { value: k }, k)));
    const value = h("select", { "aria-label": "Filter value", disabled: true }, h("option", { value: "" }, "value"));
    field.addEventListener("change", () => {
      const f = facets.fields[field.value];
      fill(value, h("option", { value: "" }, "value"),
        (f ? f.values : []).map((v, i) => h("option", { value: String(i) }, `${metaText(v.value)} (${num(v.count)})`)));
      value.disabled = !f;
    });
    value.addEventListener("change", () => {
      const f = facets.fields[field.value];
      if (!f || value.value === "") return;
      where[field.value] = f.values[Number(value.value)].value;
      drawFilters();
    });
    fill(filterRow, field, value);
    fill(filterChips, Object.entries(where).map(([k, v]) => h("button", { class: "chip", type: "button", title: "Remove this filter",
      onclick: () => { delete where[k]; drawFilters(); } }, `${k} = ${metaText(v)}  ×`)));
  }

  function params() {
    const body = { k: Number(k.value), mode: mode.value };
    if (rerank.value !== "") body.rerank = rerank.value === "true";
    if (hops.value !== "") body.graph_hops = Number(hops.value);
    const p = principals.value.split(",").map((x) => x.trim()).filter(Boolean);
    if (p.length) body.principals = p;
    if (Object.keys(where).length) body.where = { ...where };
    return body;
  }
  const asWho = (body) => (body.principals ? pill(`as ${body.principals.join(", ")}`, "info", false) : null);

  async function search() {
    const query = q.value.trim();
    if (!query) return;
    const body = { query, ...params() };
    fill(results, skeleton("block", 3));
    const r = await guard(() => api("POST", collPath(coll, "/query"), body));
    if (!r) { fill(results); return; }
    const top = Math.max(...r.hits.map((x) => x.score), 0.0001);
    fill(results,
      h("div", { class: "row between" }, h("div", { class: "small muted" },
        `${plural(r.hits.length, "passage")} in ${r.took_ms} ms`, r.graph && r.graph.used ? ` · ${r.graph.added} from the graph` : ""),
        asWho(body)),
      r.hits.length ? r.hits.map((x, i) => hitCard(x, i, query, top))
        : empty("search", "No passages found", "Try other words, another mode, or check the readers of your documents."));
  }

  async function ask() {
    const question = q.value.trim();
    if (!question) return;
    const body = { question, ...params() };
    fill(results, skeleton("block", 1), skeleton("block", 2));
    const r = await guard(() => api("POST", collPath(coll, "/answer"), body));
    if (!r) { fill(results); return; }
    const top = Math.max(...r.passages.map((x) => x.score), 0.0001);
    fill(results,
      answerCard(r),
      r.passages.length ? [
        h("div", { class: "row between" }, h("div", { class: "section-title" }, `Sources · ${plural(r.cited.length, "cited passage")} of ${r.passages.length}`),
          asWho(body)),
        r.passages.map((x, i) => hitCard(x, i, question, top, { n: x.n, cited: x.cited })),
      ] : null);
  }

  const example = (t) => h("button", { class: "chip", type: "button", onclick: () => { q.value = t; ask(); } }, t);
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") (e.shiftKey ? search : ask)(); });
  fill(actions, collectionSelect(coll, (v) => { coll = v; where = {}; fill(results); loadFacets(); }));
  fill(root,
    h("div", { class: "search-hero" }, icon("search", "lead"), q, btn("Search", search, { title: "Passages only (Shift+Enter)" }),
      btn("Ask", ask, { kind: "primary", ico: "spark", title: "A cited answer (Enter)" })),
    h("div", { class: "row" }, mode, k, hops, rerank, principals),
    h("div", { class: "row" }, filterRow, filterChips),
    h("div", { class: "chips" }, h("span", { class: "tiny muted" }, "Try"),
      ["What is the refund policy?", "Where do orders ship from?", "Who leads the project?"].map(example)),
    results);
  loadFacets();
  results.append(empty("spark", "Ask your documents", "Ask gives a short answer that cites its passages; Search shows the passages alone. Hybrid search combines meaning and keywords; the graph adds connected passages."));
  q.focus();
};

const TYPE_COLORS = { Person: "#60a5fa", Organization: "#a78bfa", Product: "#34d399", Place: "#fbbf24", Project: "#f472b6",
                      Policy: "#fb923c", Technology: "#22d3ee", Event: "#f87171", Other: "#94a3b8" };
const typeDot = (t) => { const d = h("span", { class: "type-dot" }); d.style.background = TYPE_COLORS[t] || TYPE_COLORS.Other; return d; };

view.graph = async (root, _arg, actions) => {
  fill(root, skeleton("block", 2));
  const [st, all] = await Promise.all([api("GET", "/v1/status"), documentCollections()]);
  if (st.engines.graph && st.engines.graph.engine === "neo4j") {
    fill(root, empty("graph", "The explorer needs pass-through Cypher",
      "With Neo4j as the graph engine, collections share one Neo4j database, so swsdb runs only its own graph queries. Search and Ask still follow the graph; open the entities in Neo4j's own browser."));
    return;
  }
  const cols = all.filter((c) => c.info.documents.graph);
  if (!cols.length) { fill(root, empty("graph", "No collections with a graph", "Create a documents collection while the graph engine is enabled.")); return; }
  let coll = cols[0].name;
  const listCard = h("div", { class: "card flush" });
  const detail = h("div", { class: "stack" });
  const cypher = (query, params) => api("POST", collPath(coll, "/cypher"), { query, params, read_only: true });
  const find = h("input", { placeholder: "Find an entity", class: "grow", "aria-label": "Find entity" });

  async function top(filter) {
    const where = filter ? "WHERE toLower(e.name) CONTAINS toLower($q) " : "";
    const r = await guard(() => cypher(`MATCH (e:Entity) ${where}OPTIONAL MATCH (e)<-[:MENTIONS]-(c:Chunk) ` +
      "RETURN e.key, e.name, e.type, count(c) AS mentions ORDER BY mentions DESC LIMIT 60", filter ? { q: filter } : null));
    if (!r) return;
    const count = await guard(() => cypher("MATCH (e:Entity) RETURN count(e)"));
    const total = count ? count.rows[0][0] : r.rows.length;
    const most = Math.max(1, ...r.rows.map((row) => row[3]));
    fill(listCard, h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Entities"),
      h("div", { class: "sub" }, `${num(r.rows.length)} of ${num(total)} shown, most mentioned first`)),
      h("div", { class: "row" }, find, btn("", () => top(find.value.trim()), { ico: "search", title: "Find" }))),
      table([
        { label: "Entity", get: (row) => h("span", { class: "row" }, typeDot(row[2]), row[1]) },
        { label: "Type", get: (row) => h("span", { class: "muted small" }, row[2]) },
        { label: "Mentions", get: (row) => {
          const b = h("i"); b.style.width = `${Math.max(2, (row[3] / most) * 100)}%`;
          return h("div", { class: "bar-row nolabel", title: `${row[1]}: ${num(row[3])} mentions` }, h("span", { class: "bar-track" }, b),
            h("span", { class: "bar-value" }, num(row[3])));
        } },
      ], r.rows, { onRow: (row) => focus(row[0], row[1], row[2]),
                   emptyView: h("div", { class: "card-pad" }, empty("wand", "No entities yet",
                     "Entities are the names documents mention (people, projects, companies). Add documents, or set SWSDB_LLM_MODEL for typed entities and relations.")) }));
    if (!detail.childNodes.length) fill(detail, card(empty("graph", "Pick an entity", "See what it's related to and which documents mention it.")));
  }

  async function focus(key, name, type) {
    fill(detail, skeleton("block", 2));
    const [rel, co, docs] = await Promise.all([
      guard(() => cypher("MATCH (e:Entity {key: $k})-[r:RELATED]-(o:Entity) RETURN o.key, o.name, o.type, r.type LIMIT 16", { k: key })),
      guard(() => cypher("MATCH (e:Entity {key: $k})<-[:MENTIONS]-(:Chunk)-[:MENTIONS]->(o:Entity) WHERE o.key <> $k " +
        "RETURN o.key, o.name, o.type, count(*) AS n ORDER BY n DESC LIMIT 16", { k: key })),
      guard(() => cypher("MATCH (e:Entity {key: $k})<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document) " +
        "RETURN d.external_id, count(c) AS n ORDER BY n DESC LIMIT 20", { k: key })),
    ]);
    if (!rel || !co || !docs) { fill(detail); return; }
    const nodes = [...rel.rows.map((r) => ({ key: r[0], name: r[1], type: r[2], label: r[3], kind: "rel" })),
                   ...co.rows.filter((r) => !rel.rows.some((x) => x[0] === r[0]))
                     .map((r) => ({ key: r[0], name: r[1], type: r[2], label: `co-mentioned ×${r[3]}`, kind: "co" }))].slice(0, 18);
    fill(detail,
      card(h("div", { class: "row between" }, h("div", { class: "row" }, typeDot(type), h("h2", {}, name)), pill(type, "", false)),
        nodes.length ? radial(name, nodes, (n) => focus(n.key, n.name, n.type))
                     : h("div", { class: "muted small" }, "No related or co-mentioned entities yet."),
        h("div", { class: "legend" }, h("span", {}, h("i", { class: "s-deleting" }), "related (extracted relation)"),
          h("span", {}, h("i", { class: "s-processing" }), "mentioned in the same passages"))),
      h("div", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, "Mentioned in")),
        table([{ label: "Document", mono: true, get: (r) => r[0] }, { label: "Passages", num: true, get: (r) => r[1] }], docs.rows)));
  }

  find.addEventListener("keydown", (e) => { if (e.key === "Enter") top(find.value.trim()); });
  fill(actions, collectionSelect(coll, (v) => { coll = v; fill(detail); top(); }));
  fill(root, h("div", { class: "split" }, listCard, detail));
  await top();
};

function radial(center, nodes, onClick) {
  const W = 640, H = 440, cx = W / 2, cy = H / 2, R = 165;
  const svg = s("svg", { class: "graphview", viewBox: `0 0 ${W} ${H}` },
    s("defs", {}, s("radialGradient", { id: "centergrad" }, s("stop", { offset: "0", "stop-color": "#a78bfa" }),
      s("stop", { offset: "1", "stop-color": "#6366f1" }))));
  const edges = s("g"), labels = s("g"), dots = s("g");
  nodes.forEach((n, i) => {
    const a = (2 * Math.PI * i) / nodes.length - Math.PI / 2;
    n.x = cx + R * 1.25 * Math.cos(a); n.y = cy + R * 0.95 * Math.sin(a);
    edges.append(s("line", { x1: cx, y1: cy, x2: n.x, y2: n.y, class: `edge ${n.kind === "rel" ? "rel" : ""}` }));
    if (n.kind === "rel") labels.append(s("text", { x: (cx + n.x) / 2, y: (cy + n.y) / 2 - 4, "text-anchor": "middle", class: "edge-label" }, n.label));
    const g = s("g", { class: "node", transform: `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})` },
      s("title", {}, `${n.name} (${n.type}) · ${n.label}`),
      s("circle", { r: 9, fill: TYPE_COLORS[n.type] || TYPE_COLORS.Other }),
      s("text", { x: n.x < cx - 1 ? -14 : 14, y: 4, "text-anchor": n.x < cx - 1 ? "end" : "start" },
        n.name.length > 26 ? `${n.name.slice(0, 25)}…` : n.name));
    g.addEventListener("click", () => onClick(n));
    dots.append(g);
  });
  const c = s("g", { class: "node center", transform: `translate(${cx},${cy})` }, s("circle", { r: 16 }),
    s("text", { y: 34, "text-anchor": "middle" }, center.length > 30 ? `${center.slice(0, 29)}…` : center));
  svg.append(edges, labels, dots, c);
  return svg;
}

view.eval = async (root, _arg, actions) => {
  fill(root, skeleton("block", 2));
  const cols = await documentCollections();
  if (!cols.length) { fill(root, noCollections()); return; }
  let coll = cols[0].name;
  const setsCard = h("div", { class: "card flush" });
  const report = h("div", { class: "stack" });
  const history = h("div", { class: "stack" });
  const base = () => collPath(coll, "/eval");
  const bestRecall = (run) => Math.max(...run.results.map((x) => x.metrics.recall));

  async function load() {
    const [sets, runs] = await Promise.all([guard(() => api("GET", `${base()}/sets`)), guard(() => api("GET", `${base()}/runs`))]);
    if (sets) fill(setsCard, h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Golden sets"),
      h("div", { class: "sub" }, "Questions with the documents that should answer them"))),
      table([
        { label: "Set", get: (x) => h("b", {}, x.name) }, { label: "Questions", num: true, get: (x) => x.cases },
        { label: "Updated", num: true, get: (x) => h("span", { class: "muted" }, rel(x.updated_at)) },
        { label: "", num: true, get: (x) => btn("Run", (e) => { e.stopPropagation(); run(x.name); }, { kind: "primary", ico: "play" }) },
      ], sets.sets, { emptyView: h("div", { class: "card-pad" }, empty("eval", "No golden sets yet", "Write one on the right: a few real questions is enough to start.")) }));
    if (runs) {
      fill(history, runs.runs.length ? [
        runs.runs.length > 1 ? card(cardHead("Best recall over time", plural(runs.runs.length, "run")),
          sparkline(runs.runs.slice().reverse().map(bestRecall))) : null,
        h("div", { class: "card flush" }, h("div", { class: "card-head" }, h("h2", {}, "History")), table([
          { label: "Run", get: (x) => h("b", {}, `#${x.id}`) }, { label: "Set", get: (x) => x.set },
          { label: "Winner", get: (x) => pill(x.best, "ok", false) },
          { label: "Recall", num: true, get: (x) => meter(bestRecall(x), 1, "ok") },
          { label: "When", num: true, get: (x) => h("span", { class: "muted" }, rel(x.created_at)) },
        ], runs.runs, { onRow: show }))] : null);
    }
  }

  function show(run) {
    const keys = ["hit", "recall", "mrr", "ndcg", "answer_found"].filter((k) => run.results.some((r) => k in r.metrics));
    const best = run.results.find((r) => r.config.name === run.best) || run.results[0];
    const label = { hit: "Hit rate", recall: "Recall", mrr: "MRR", ndcg: "nDCG", answer_found: "Answer found" };
    const band = (v) => (v >= 0.9 ? "ok" : v >= 0.6 ? "warn" : "bad");
    fill(report,
      h("div", { class: "winner" }, h("div", { class: "icon-tile green" }, icon("trophy")),
        h("div", { class: "grow" }, h("div", { class: "tiny muted" },
          `Run #${run.id ?? "?"} · ${run.set} · ${plural(run.cases, "question")} · top ${run.k}`),
          h("div", { class: "big" }, `${run.best} retrieves best`),
          h("div", { class: "small text-2" }, `recall ${fmt(best.metrics.recall)} · MRR ${fmt(best.metrics.mrr)} · ${best.metrics.latency_p50_ms} ms median`))),
      h("div", { class: "card flush" }, table([
        { label: "Configuration", get: (r) => h("b", {}, r.config.name, r.config.name === run.best ? " ★" : "") },
        ...keys.map((k) => ({ label: label[k], num: true, get: (r) => meter(r.metrics[k], 1, band(r.metrics[k])) })),
        { label: "Evidence", num: true, get: (r) => fmt(r.metrics.evidence, 1) },
        { label: "p50", num: true, get: (r) => `${r.metrics.latency_p50_ms} ms` },
        { label: "p95", num: true, get: (r) => `${r.metrics.latency_p95_ms} ms` },
      ], run.results.map((r) => ({ ...r, __best: r.config.name === run.best })))),
      h("div", { class: "tiny muted" }, "Hit, recall and MRR count documents in the returned evidence; evidence is the average number of passages returned (the graph adds connected passages)."),
      best.misses && best.misses.length ? h("div", { class: "card flush" }, h("div", { class: "card-head" },
        h("div", {}, h("h2", {}, "Missed by the winner"), h("div", { class: "sub" }, plural(best.misses.length, "question")))),
        table([{ label: "Question", get: (m) => m.query }, { label: "Expected", mono: true, get: (m) => m.expected.join(", ") },
               { label: "Got", mono: true, get: (m) => m.got.join(", ") || "nothing" }], best.misses)) : null);
  }

  async function run(name) {
    fill(report, skeleton("block", 2));
    const r = await guard(() => api("POST", `${base()}/runs`, { set: name }));
    if (r) { show(r); toast(`${name}: ${r.best} wins`); load(); } else fill(report);
  }

  const setName = h("input", { placeholder: "Set name, e.g. support-faq", "aria-label": "Set name" });
  const cases = h("textarea", { placeholder: "[ { \"query\": \"…\", \"relevant\": [\"doc-id\"] } ]", class: "mono", "aria-label": "Cases" });
  const template = () => {
    cases.value = JSON.stringify([
      { query: "How long do refunds take?", relevant: ["refund-policy"], answer: "five business days" },
      { query: "Where do orders ship from?", relevant: ["shipping"] },
    ], null, 2);
  };
  const editor = card(cardHead("Golden set", "Each question lists the ids of the documents that answer it; \"answer\" is optional",
    btn("Template", template, { ico: "wand" })),
    h("div", { class: "stack tight" }, setName, cases, h("div", { class: "row end" }, btn("Save set", async () => {
      let parsed;
      try { parsed = JSON.parse(cases.value); } catch (e) { showError(`That isn't valid JSON: ${e.message}`); return; }
      const n = setName.value.trim();
      if (!n) { showError("Give the set a name."); return; }
      if (await guard(() => api("PUT", `${base()}/sets/${encodeURIComponent(n)}`, { cases: parsed }))) { toast(`Saved ${n}`); load(); }
    }, { kind: "primary" }))));

  fill(actions, collectionSelect(coll, (v) => { coll = v; fill(report); load(); }));
  fill(root, h("div", { class: "split" }, setsCard, editor), report, history);
  await load();
};

const bytes = (n) => {
  if (n === null || n === undefined) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

async function downloadSnapshot(id) {
  // a plain link can't send the API key: fetch it, then hand the browser a local copy to save
  const headers = {};
  const key = sessionStorage.getItem("swsdb.key");
  if (key) headers["api-key"] = key;
  toast(`Downloading ${id}…`, "info");
  const r = await fetch(`/v1/admin/snapshots/${encodeURIComponent(id)}/download`, { headers });
  if (!r.ok) { showError(`download failed (${r.status})`); return; }
  const url = URL.createObjectURL(await r.blob());
  const a = h("a", { href: url, download: `${id}.tar` });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

view.backups = async (root, _arg, actions) => {
  fill(root, skeleton("block", 3));
  let data;
  try {
    data = await Promise.all([api("GET", "/v1/admin/backups"), api("GET", "/v1/admin/snapshots")]);
  } catch (e) {
    if (e instanceof ApiError && e.status === 403) {
      fill(root, empty("lock", "Backups need the admin key from the server's environment",
        "Snapshots cover every tenant, so a tenant's key can't take or restore them."));
      return;
    }
    throw e;
  }
  const [b, snaps] = data;
  const s = b.schedule;
  const busy = (label) => toast(label, "info");

  async function restore(id, note) {
    if (!confirm(`Replace ALL data in every tenant with ${id}?${note ? "\n\n" + note : ""}\n\nThe current data is snapshotted first.`)) return;
    busy(`Restoring ${id}…`);
    const r = await guard(() => api("POST", `/v1/admin/snapshots/${encodeURIComponent(id)}/restore`, { backup_first: true }));
    if (r) { toast(`Restored ${id} in ${r.took_s}s (previous data: ${r.backup})`); route(); }
  }

  fill(actions,
    btn("Take snapshot", async () => {
      busy("Taking a snapshot…");
      const r = await guard(() => api("POST", "/v1/admin/snapshots", {}));
      if (r) { toast(`Snapshot ${r.id} (${bytes(r.bytes)}) in ${r.took_s}s`); route(); }
    }, { ico: "archive" }),
    btn("Back up now", async () => {
      busy("Backing up…");
      const r = await guard(() => api("POST", "/v1/admin/backups/run"));
      if (r) { toast(r.uploaded ? `Backed up and uploaded to ${r.uploaded}` : `Backed up as ${r.snapshot}`); route(); }
    }, { kind: "primary", ico: "cloud" }));

  const last = s.last;
  const schedule = card(
    h("div", { class: "row between" },
      h("div", { class: "row" }, h("div", { class: `icon-tile ${s.interval_h ? "green" : "amber"}` }, icon("clock")),
        h("div", {}, h("b", {}, s.interval_h ? `Every ${s.interval_h} h, keeping the last ${s.keep}` : "No schedule"),
          h("div", { class: "small muted" }, s.interval_h ? (s.next_at ? `next ${new Date(s.next_at).toLocaleString()}` : "")
            : "Set SWSDB_BACKUP_INTERVAL_H to take backups automatically."))),
      last ? (last.ok ? pill(`last ${rel(last.at)} · ${bytes(last.bytes)}`, "ok") : pill("last backup failed", "bad")) : pill("never run", "", false)),
    last && !last.ok ? h("div", { class: "codeblock" }, last.error) : null,
    h("dl", { class: "kv" },
      h("dt", {}, "Storage"), h("dd", {}, s.s3 ? `${s.s3.endpoint} · bucket ${s.s3.bucket}${s.s3.prefix ? ` · ${s.s3.prefix}` : ""}`
        : "this server's volume only: set SWSDB_BACKUP_S3_BUCKET to keep copies elsewhere"),
      last && last.uploaded ? [h("dt", {}, "Last copy"), h("dd", { class: "mono" }, last.uploaded)] : null,
      last ? [h("dt", {}, "Took"), h("dd", {}, `${last.took_s}s`)] : null));

  const local = h("div", { class: "card flush" }, h("div", { class: "card-head" },
    h("div", {}, h("h2", {}, "Snapshots on this server"), h("div", { class: "sub" }, `${plural(snaps.snapshots.length, "snapshot")} in /swsdb/data/snapshots`))),
    table([
      { label: "Snapshot", get: (x) => h("div", {}, h("div", { class: "mono" }, x.id), x.error ? h("div", { class: "tiny" }, pill("unreadable", "bad", false)) : null) },
      { label: "Taken", get: (x) => h("span", { class: "muted" }, x.created_at ? rel(x.created_at) : "-") },
      { label: "swsdb", get: (x) => x.swsdb_version || "-" },
      { label: "Documents", num: true, get: (x) => x.documents ?? "-" },
      { label: "Size", num: true, get: (x) => bytes(x.bytes) },
      { label: "", num: true, get: (x) => h("div", { class: "row end" },
        btn("", () => downloadSnapshot(x.id), { ico: "download", title: "Download" }),
        btn("Restore", () => restore(x.id), { ico: "refresh" }),
        btn("", async () => {
          if (!confirm(`Delete snapshot ${x.id}?`)) return;
          if (await guard(() => api("DELETE", `/v1/admin/snapshots/${encodeURIComponent(x.id)}`))) { toast(`Deleted ${x.id}`, "info"); route(); }
        }, { kind: "danger", ico: "trash", title: "Delete" })) },
    ], snaps.snapshots, { emptyView: h("div", { class: "card-pad" }, empty("archive", "No snapshots yet", "Take one: it copies every engine at one consistent point.")) }));

  const remote = s.s3 ? h("div", { class: "card flush" }, h("div", { class: "card-head" },
    h("div", {}, h("h2", {}, "Copies in S3"), h("div", { class: "sub" }, b.remote_error ? "storage unreachable" : `${b.remote.length} ${b.remote.length === 1 ? "copy" : "copies"}`))),
    b.remote_error ? h("div", { class: "card-pad" }, h("div", { class: "codeblock" }, b.remote_error)) :
    table([
      { label: "Object", mono: true, get: (o) => o.key },
      { label: "Size", num: true, get: (o) => bytes(o.bytes) },
      { label: "Uploaded", num: true, get: (o) => h("span", { class: "muted" }, rel(o.modified)) },
      { label: "", num: true, get: (o) => btn("Restore", async () => {
        busy(`Fetching ${o.key}…`);
        const m = await guard(() => api("POST", "/v1/admin/backups/fetch", { key: o.key }));
        if (m) restore(m.id, `Fetched from ${o.key}.`);
      }, { ico: "cloud" }) },
    ], b.remote, { emptyView: h("div", { class: "card-pad" }, empty("cloud", "No copies yet", "Back up now, or wait for the schedule.")) })) : null;

  fill(root, schedule, h("div", { class: "section-title" }, "Snapshots"), local, remote ? [h("div", { class: "section-title" }, "Remote copies"), remote] : null);
};

const FINDINGS = {
  missing_vectors: ["vector", "Missing or hidden vectors", "Documents whose live version isn't fully in the vector store. Repair re-embeds them from the catalog."],
  stale_vectors: ["vector", "Old versions left behind", "Vectors of versions that are no longer live. Repair deletes them."],
  orphan_vectors: ["vector", "Vectors of unknown documents", "Points whose document doesn't exist in the catalog. Repair deletes them."],
  graph_mismatch: ["graph", "Graph out of step", "Documents whose graph doesn't match the catalog. Repair rebuilds it from the catalog (no LLM calls)."],
  orphan_graph: ["graph", "Graph nodes of unknown documents", "Repair removes them."],
  stuck: ["clock", "Stuck changes", "Accepted changes with nothing applying them. Repair queues them again."],
  missing_vector_collection: ["database", "Missing vector collection", "Repair recreates it and re-indexes every document."],
};

view.doctor = async (root, _arg, actions) => {
  const out = h("div", { class: "stack" });
  const all = h("input", { type: "checkbox" });
  async function check(repair) {
    fill(out, skeleton("block", 2));
    const r = await guard(() => api("POST", "/v1/admin/reconcile", { dry_run: !repair, all_tenants: all.checked }));
    if (!r) { fill(out); return; }
    const issues = r.issues;
    const found = Object.entries(r.totals || {}).filter(([, v]) => v);
    const docs = r.collections.reduce((a, c) => a + c.documents, 0);
    fill(out,
      card(h("div", { class: "row" },
        ring(issues ? 0.3 : 1, issues ? "warn" : "", issues ? String(issues) : "✓", issues ? "issues" : "healthy"),
        h("div", { class: "grow stack tight" },
          h("h2", {}, issues ? (repair ? `Repairing ${plural(issues, "issue")}` : `${plural(issues, "issue")} found`) : "Every store agrees with the catalog"),
          h("div", { class: "text-2" }, issues ? (repair ? "Repairs were queued and run in the background; check again in a moment."
            : "Nothing has been changed. Repair queues the fixes below through the same safe path as normal writes.")
            : `Checked ${plural(docs, "document")} in ${plural(r.collections.length, "collection")} in ${r.took_s}s.`),
          issues && !repair ? h("div", {}, btn("Repair", () => {
            if (confirm("Queue repairs for everything found?")) check(true);
          }, { kind: "primary", ico: "wand" })) : null))),
      found.length ? h("div", { class: "card flush" }, found.map(([k, v]) => {
        const [ico, title, text] = FINDINGS[k] || ["info", k, ""];
        return h("div", { class: "finding" }, h("div", { class: "icon-tile amber" }, icon(ico)),
          h("div", {}, h("b", {}, title), h("span", { class: "small muted" }, text)), pill(String(v), "warn", false));
      })) : null,
      h("div", { class: "card flush" }, table([
        { label: "Collection", get: (c) => h("b", {}, `${c.tenant} / ${c.collection}`) },
        { label: "Documents", num: true, get: (c) => c.documents },
        { label: "State", num: true, get: (c) => {
          const n = Object.values(c.found || {}).reduce((a, v) => a + (v || 0), 0);
          return n ? pill(plural(n, "issue"), "warn") : pill("consistent", "ok");
        } },
      ], r.collections, { emptyView: h("div", { class: "card-pad" }, noCollections()) })));
  }
  fill(actions, h("label", { class: "check" }, all, "All tenants"), btn("Check now", () => check(false), { kind: "primary", ico: "doctor" }));
  fill(root, card(h("div", { class: "row" }, h("div", { class: "icon-tile green" }, icon("shield")),
    h("div", { class: "grow" }, h("b", {}, "Postgres is the source of truth"),
      h("div", { class: "small muted" }, "The doctor compares it with what the vector store and the graph actually hold. Checking never changes anything; it also runs automatically every 10 minutes.")))), out);
  await check(false);
};

// ------------------------------------------------------------------ offline handling

function goOffline() {
  if (state.offline) return;
  state.offline = true;
  clearTimers();
  closeDrawer();
  setConn("bad", "unreachable");
  fill($("#actions"));
  showError(null);
  fill($("#page"), h("div", { class: "offline" },
    h("div", { class: "icon-tile red" }, icon("wifi")),
    h("h2", {}, "Can't reach the swsdb server"),
    h("div", { class: "text-2" }, `Nothing is answering at ${location.host}. Is the container running?`),
    h("div", { class: "codeblock" }, "docker ps --filter ancestor=swsdb/swsdb"),
    h("div", { class: "small muted" }, "Reconnecting automatically…"),
    btn("Try now", reconnect, { kind: "primary", ico: "refresh" })));
  state.reconnect = setInterval(reconnect, 3000);
}
async function reconnect() {
  try {
    const r = await fetch("/readyz", { cache: "no-store" });
    if (r.status === 200 || r.status === 503) {
      clearInterval(state.reconnect);
      state.offline = false;
      toast("Reconnected to swsdb");
      health();
      route();
    }
  } catch { /* still down */ }
}

// ------------------------------------------------------------------ data browser: SQL, Cypher and vectors, through the API

// a result cell as short text; nodes, edges and paths by their labels and properties
function cellText(v) {
  if (v === null || v === undefined) return "null";
  if (typeof v !== "object") return String(v);
  if (v.labels && v.properties) return `(${v.labels.join(":")} ${JSON.stringify(v.properties)})`;
  if (v.type && v.properties && "src" in v) return `[:${v.type} ${JSON.stringify(v.properties)}]`;
  if (v.nodes && v.edges) return `path of ${v.nodes.length} nodes`;
  return JSON.stringify(v);
}
const clip = (t, n = 140) => (t.length > n ? `${t.slice(0, n - 1)}…` : t);

function resultTable(res) {
  if (!res.columns.length) return h("div", { class: "card-pad muted small" }, "Done. The statement returned no rows.");
  return table(res.columns.map((c, i) => ({ label: c, mono: true, get: (row) => {
    const t = cellText(row[i]);
    return h("span", { title: t.length > 140 ? t : null }, clip(t));
  } })), res.rows, { emptyView: h("div", { class: "card-pad muted small" }, "No rows.") });
}

// nodes and edges anywhere in a Cypher result, laid out with a small force simulation
function graphOf(res) {
  const nodes = new Map(), edges = new Map();
  const visit = (v) => {
    if (!v || typeof v !== "object") return;
    if (Array.isArray(v)) { v.forEach(visit); return; }
    if (v.labels && v.properties && "id" in v) { nodes.set(String(v.id), v); return; }
    if (v.type && "src" in v && "dst" in v) { edges.set(String(v.id), v); return; }
    if (v.nodes && v.edges) { v.nodes.forEach(visit); v.edges.forEach(visit); return; }
  };
  res.rows.forEach((r) => r.forEach(visit));
  return { nodes: [...nodes.values()], edges: [...edges.values()].filter((e) => nodes.has(String(e.src)) && nodes.has(String(e.dst))) };
}

function nodeName(n) {
  const p = n.properties || {};
  if (n.labels.includes("Chunk") && p.ordinal !== undefined) return `chunk ${p.ordinal}`;
  return String(p.name ?? p.title ?? p.external_id ?? p.key ?? p.id ?? n.labels[0] ?? n.id);
}

function forceGraph({ nodes, edges }, onPick) {
  const W = 900, H = 560;
  const pos = new Map(nodes.map((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(nodes.length, 1);
    return [String(n.id), { x: W / 2 + 160 * Math.cos(a), y: H / 2 + 140 * Math.sin(a), vx: 0, vy: 0 }];
  }));
  const P = [...pos.values()];
  // Fruchterman-Reingold: repulsion k²/d, attraction d²/k, a little gravity, steps capped by a cooling temperature
  const k = 0.6 * Math.sqrt((W * H) / Math.max(P.length, 1));
  let temp = W / 8;
  for (let it = 0; it < 300; it++) {
    for (const p of P) { p.vx = 0; p.vy = 0; }
    for (let i = 0; i < P.length; i++) for (let j = i + 1; j < P.length; j++) {
      const a = P[i], b = P[j], dx = a.x - b.x, dy = a.y - b.y, d = Math.sqrt(dx * dx + dy * dy) || 0.1, f = (k * k) / d;
      a.vx += (dx / d) * f; a.vy += (dy / d) * f; b.vx -= (dx / d) * f; b.vy -= (dy / d) * f;
    }
    for (const e of edges) {
      const a = pos.get(String(e.src)), b = pos.get(String(e.dst)), dx = a.x - b.x, dy = a.y - b.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.1, f = (d * d) / k;
      a.vx -= (dx / d) * f; a.vy -= (dy / d) * f; b.vx += (dx / d) * f; b.vy += (dy / d) * f;
    }
    for (const p of P) {
      p.vx += (W / 2 - p.x) * 0.3; p.vy += (H / 2 - p.y) * 0.3;  // keeps separate clusters near each other
      const d = Math.sqrt(p.vx * p.vx + p.vy * p.vy) || 1, step = Math.min(d, temp);
      p.x += (p.vx / d) * step; p.y += (p.vy / d) * step;
    }
    temp = Math.max(1, temp * 0.97);
  }
  // then fit the drawing to the canvas (no clamping while it settles, so nothing piles up at the edges)
  const xs = P.map((p) => p.x), ys = P.map((p) => p.y);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const sc = Math.min((W - 180) / ((x1 - x0) || 1), (H - 60) / ((y1 - y0) || 1));
  for (const p of P) { p.x = 60 + (p.x - x0) * sc; p.y = 30 + (p.y - y0) * sc; }
  const labels = [...new Set(nodes.map((n) => n.labels[0] || ""))];
  const palette = ["#8b7bff", "#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#22d3ee", "#fb923c", "#a3e635"];
  const color = (n) => palette[labels.indexOf(n.labels[0] || "") % palette.length];
  const svg = s("svg", { class: "graphview tall", viewBox: `0 0 ${W} ${H}` });
  const g1 = s("g"), g2 = s("g"), g3 = s("g");
  for (const e of edges) {
    const a = pos.get(String(e.src)), b = pos.get(String(e.dst));
    g1.append(s("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "edge" }));
    if (edges.length <= 30) g2.append(s("text", { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 3, "text-anchor": "middle", class: "edge-label" }, e.type));
  }
  for (const n of nodes) {
    const p = pos.get(String(n.id)), name = nodeName(n);
    const node = s("g", { class: "node", transform: `translate(${p.x.toFixed(1)},${p.y.toFixed(1)})` },
      s("title", {}, `${n.labels.join(":")} ${JSON.stringify(n.properties)}`), s("circle", { r: 8, fill: color(n) }),
      nodes.length <= 40 || !n.labels.includes("Chunk") ? s("text", { x: 12, y: 4 }, name.length > 22 ? `${name.slice(0, 21)}…` : name) : null);
    node.addEventListener("click", () => onPick(n));
    g3.append(node);
  }
  svg.append(g1, g2, g3);
  const legend = h("div", { class: "legend" }, labels.map((l, i) => {
    const dot = h("i"); dot.style.background = palette[i % palette.length]; return h("span", {}, dot, l || "(no label)");
  }));
  return h("div", {}, svg, legend);
}

function dataTabs(current, onPick) {
  return h("div", { class: "tabs", role: "tablist" }, [["tables", "Tables"], ["schema", "Schema"], ["database", "Database"], ["sql", "SQL"], ["cypher", "Cypher"], ["vectors", "Vectors"]].map(([k, label]) =>
    h("button", { class: k === current ? "on" : null, role: "tab", "aria-selected": k === current ? "true" : "false",
                  onclick: () => onPick(k) }, label)));
}

view.data = async (root, arg, actions) => {
  const [first, ...rest] = (arg || "").split("/");
  const tab = ["tables", "schema", "database", "sql", "cypher", "vectors"].includes(first) ? first : "tables";
  fill(actions, dataTabs(tab, (k) => { location.hash = `data/${k}`; }));
  fill(root, skeleton("block", 2));
  if (tab === "tables") return dataTables(root, rest.length ? rest.join("/") : null);
  if (tab === "schema") return dataSchema(root);
  if (tab === "database") return dataDatabase(root, rest[0]);
  if (tab === "sql") return dataSql(root);
  if (tab === "cypher") return dataCypher(root);
  return dataVectors(root);
};

// a result worth a chart: a label (or date) column and a number column, a handful of rows
function resultChart(res) {
  if (res.rows.length < 2 || res.rows.length > 100 || res.columns.length < 2) return null;
  const isNum = (v) => v !== null && v !== "" && !Number.isNaN(Number(v)) && typeof v !== "boolean";
  const numeric = res.columns.map((_, i) => i).filter((i) => i > 0 && res.rows.every((r) => isNum(r[i])));
  if (!numeric.length) return null;
  const xi = 0;
  const isDate = res.rows.every((r) => typeof r[xi] === "string" && /^\d{4}-\d{2}-\d{2}/.test(r[xi]));
  const box = h("div", { class: "card" });
  // the column ORDER BY sorted on is usually the one to show: default to the last number column, let people switch
  const draw = (yi) => {
    const points = res.rows.map((r) => ({ x: String(r[xi]), y: Number(r[yi]) }));
    const pick = numeric.length > 1 ? h("select", { "aria-label": "Column to chart", onchange: (e) => draw(Number(e.target.value)) },
      numeric.map((i) => h("option", { value: i, selected: i === yi }, res.columns[i]))) : null;
    if (isDate) points.sort((a, b) => a.x.localeCompare(b.x));
    fill(box, cardHead(`${res.columns[yi]} by ${res.columns[xi]}`, isDate ? "From the result above; hover a bar for its value" : "From the result above", pick),
      h("div", { class: "card-pad" }, isDate ? columnChart(points, { label: (p) => p.x.slice(0, 10) })
        : barList(points.slice(0, 30).map((p) => ({ label: p.x, count: p.y })))));
  };
  draw(numeric[numeric.length - 1]);
  return box;
}

function csv(res) {
  const cell = (v) => {
    const t = v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v);
    return /[",\n]/.test(t) ? `"${t.replaceAll('"', '""')}"` : t;
  };
  return [res.columns.map(cell).join(","), ...res.rows.map((r) => r.map(cell).join(","))].join("\n");
}

function download(name, text) {
  const a = h("a", { href: URL.createObjectURL(new Blob([text], { type: "text/csv" })), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function dataCypher(root) {
  const st = await guard(() => api("GET", "/v1/status"));
  if (st && st.engines.graph && st.engines.graph.engine === "neo4j") {
    fill(root, card(empty("graph", "Cypher is off with Neo4j",
      "Collections share one Neo4j database, so swsdb runs only its own graph queries. Browse the graph in Neo4j's own browser.")));
    return;
  }
  const { collections } = await api("GET", "/v1/collections");
  const graphs = collections.filter((c) => c.graph);
  if (!graphs.length) { fill(root, card(empty("graph", "No graphs yet", "Create a collection while the graph engine is enabled."))); return; }
  let coll = graphs[0].name;
  const q = h("textarea", { class: "mono", "aria-label": "Cypher", spellcheck: "false" });
  q.value = "MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 60";
  const ro = h("input", { type: "checkbox", checked: true, "aria-label": "Read only" });
  const picked = h("div");
  const out = h("div", { class: "stack" });
  const sel = h("select", { "aria-label": "Graph", onchange: (e) => { coll = e.target.value; run(); } },
    graphs.map((c) => h("option", { value: c.name }, c.name)));
  async function run() {
    const r = await guard(() => api("POST", collPath(coll, "/cypher"), { query: q.value, read_only: ro.checked }));
    if (!r) return;
    const g = graphOf(r);
    fill(picked);
    fill(out,
      g.nodes.length ? card(cardHead("Graph", `${plural(g.nodes.length, "node")}, ${plural(g.edges.length, "relationship")}. Click a node for its properties.`),
        forceGraph(g, (n) => fill(picked, card(cardHead(nodeName(n), n.labels.join(":")),
          h("pre", { class: "codeblock wrap" }, JSON.stringify(n.properties, null, 2)))))) : null,
      picked,
      card(cardHead("Rows", plural(r.rows.length, "row")), resultTable(r)));
  }
  q.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); });
  const examples = [["Sample", "MATCH (a)-[r]->(b) RETURN a, r, b LIMIT 60"],
    ["Labels", "MATCH (n) RETURN labels(n) AS label, count(n) AS nodes"],
    ["A document", "MATCH (d:Document)-[h:HAS_CHUNK]->(c:Chunk)-[m:MENTIONS]->(e:Entity) RETURN d, h, c, m, e LIMIT 40"],
    ["Entities", "MATCH (c:Chunk)-[m:MENTIONS]->(e:Entity) RETURN e.name AS entity, count(c) AS mentions ORDER BY mentions DESC LIMIT 25"]];
  fill(root, h("div", { class: "stack" },
    card(cardHead("Cypher", "Runs on this collection's own graph.", sel,
        h("label", { class: "row small muted" }, ro, "read only"), btn("Run", run, { kind: "primary", ico: "play", title: "Run (Ctrl+Enter)" })),
      h("div", { class: "card-pad stack" }, q,
        h("div", { class: "chips" }, examples.map(([label, text]) => h("button", { class: "chip", type: "button",
          onclick: () => { q.value = text; run(); } }, label))))),
    out));
  run();
}

async function dataVectors(root) {
  const { collections } = await api("GET", "/v1/collections");
  const vecs = collections.filter((c) => c.vector);
  if (!vecs.length) { fill(root, card(empty("vector", "No vector collections yet", "Create a collection with documents or raw vectors."))); return; }
  let coll = vecs[0].name, next = null, points = [], info = null, where = {}, fields = {}, loading = 0;
  const listCard = h("div", { class: "card flush" });
  const detail = h("div", { class: "stack" });
  const filterBar = h("div", { class: "row" });
  const sel = h("select", { "aria-label": "Collection", onchange: (e) => { coll = e.target.value; where = {}; load(true); } },
    vecs.map((c) => h("option", { value: c.name }, c.name)));

  // payload fields to filter on, and their common values, from a sample of the collection (document metadata is
  // under "meta.")
  async function sampleFields() {
    const r = await api("POST", collPath(coll, "/points/scroll"), { limit: 500 }).catch(() => null);
    const out = {}, decimal = new Set();
    const add = (k, v) => {
      if (k.startsWith("_") || !["string", "number", "boolean"].includes(typeof v)) return;
      if (typeof v === "number" && !Number.isInteger(v)) { decimal.add(k); return; }  // exact matches only: no measures
      if (typeof v === "string" && v.length > 80) return;  // text, not a category
      const f = (out[k] ||= new Map());
      const key = JSON.stringify(v);
      f.set(key, (f.get(key) || 0) + 1);
    };
    for (const p of (r ? r.points : [])) {
      for (const [k, v] of Object.entries(p.payload || {})) {
        if (k === "meta" && v && typeof v === "object" && !Array.isArray(v)) for (const [mk, mv] of Object.entries(v)) add(`meta.${mk}`, mv);
        else if (Array.isArray(v)) v.forEach((x) => add(k, x));
        else add(k, v);
      }
    }
    fields = Object.fromEntries(Object.entries(out)
      .filter(([k, m]) => !decimal.has(k) && (m.size > 1 || [...m.values()][0] > 1))
      .map(([k, m]) => [k, [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, 40).map(([v, n]) => ({ value: JSON.parse(v), count: n }))]));
    drawFilters();
  }
  function drawFilters() {
    const names = Object.keys(fields).sort();
    const field = h("select", { "aria-label": "Payload field" }, h("option", { value: "" }, names.length ? "Filter by field…" : "No fields to filter on"),
      names.map((k) => h("option", { value: k }, k)));
    const listId = `vals-${Math.random().toString(36).slice(2)}`;
    const values = h("datalist", { id: listId });
    const value = h("input", { placeholder: "value", list: listId, "aria-label": "Payload value", disabled: true });
    field.addEventListener("change", () => {
      fill(values, (fields[field.value] || []).map((v) => h("option", { value: String(v.value) }, `${num(v.count)} in the sample`)));
      value.disabled = !field.value; value.value = ""; value.focus();
    });
    const apply = () => {
      if (!field.value || value.value === "") return;
      // keep the value's type: what the sample holds for this field, else a number or true/false when it reads as one
      const known = (fields[field.value] || []).find((v) => String(v.value) === value.value);
      let v = known ? known.value : value.value;
      if (!known && /^-?\d+(\.\d+)?$/.test(v)) v = Number(v);
      if (!known && (v === "true" || v === "false")) v = v === "true";
      where[field.value] = v;
      load(true);
    };
    value.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
    fill(filterBar, field, value, btn("Add filter", apply, { ico: "plus" }),
      Object.entries(where).map(([k, v]) => h("button", { class: "chip", type: "button", title: "Remove this filter",
        onclick: () => { delete where[k]; load(true); } }, `${k} = ${metaText(v)}  ×`)));
  }
  const isDocs = () => info && info.documents;
  const preview = (p) => {
    const pl = p.payload || {};
    if (isDocs()) return `${pl.doc ?? ""} #${pl.chunk ?? ""}  ${clip(String(pl.text ?? ""), 90)}`;
    return clip(JSON.stringify(pl), 110);
  };
  async function load(reset) {
    const filtered = Object.keys(where).length > 0;
    const mine = ++loading;  // a newer load (another filter, collection or page) replaces this one's result
    if (reset) {
      next = null; points = [];
      const fresh = !info || info.name !== coll;
      info = await guard(() => api("GET", collPath(coll)));
      fill(detail);
      if (fresh) sampleFields(); else drawFilters();
    }
    const r = await guard(() => api("POST", collPath(coll, "/points/scroll"),
      { limit: 50, offset: next, where: filtered ? where : null }));
    if (!r || mine !== loading) return;
    points = points.concat(r.points); next = r.next;
    const v = info && info.vector;
    const shape = v ? `${v.size} dimensions · ${v.distance}${v.sparse ? " · with BM25 keywords" : ""}` : "";
    fill(listCard, cardHead("Points", v ? (filtered
      ? `${num(points.length)}${next !== null ? "+" : ""} matching the filters, of ${num(v.points)} · ${shape}`
      : `${num(v.points)} points · ${shape}`) : ""),
      table([{ label: "Id", mono: true, get: (p) => clip(String(p.id), 38) },
             { label: isDocs() ? "Document · chunk · text" : "Payload", get: (p) => h("span", { class: "small" }, preview(p)) },
             ...(isDocs() ? [{ label: "Live", get: (p) => (p.payload.live ? pill("live", "ok") : pill("hidden", "warn")) }] : [])],
            points, { onRow: show }),
      next !== null ? h("div", { class: "card-pad" }, btn("Load more", () => load(false), { ico: "download" })) : null);
    if (!detail.childNodes.length) fill(detail, card(empty("vector", "Pick a point", "See its payload and vector, and find its nearest neighbours.")));
  }
  async function show(p) {
    const r = await guard(() => api("POST", collPath(coll, "/points/get"), { ids: [p.id] }));
    const full = r && r.points[0] ? r.points[0] : p;
    const vec = Array.isArray(full.vector) ? full.vector : full.vector && full.vector.dense;
    const near = h("div", { class: "stack" });
    async function similar() {
      let hits;
      if (isDocs()) {
        const res = await guard(() => api("POST", collPath(coll, "/query"), { query: String(full.payload.text || ""), k: 8, mode: "dense", graph_hops: 0 }));
        hits = res && res.hits.map((x) => ({ id: x.chunk_id, score: x.score, label: `${x.document} #${x.chunk}  ${clip(String(x.text || ""), 80)}` }));
      } else {
        const res = vec && await guard(() => api("POST", collPath(coll, "/search"),
          { vector: vec, limit: 8, where: Object.keys(where).length ? where : null }));
        hits = res && res.hits.map((x) => ({ id: x.id, score: x.score, label: clip(JSON.stringify(x.payload || {}), 80) }));
      }
      if (!hits) return;
      fill(near, card(cardHead("Nearest neighbours", isDocs() ? "by the chunk's text, dense vectors only"
        : Object.keys(where).length ? "by this point's vector, within the filters" : "by this point's vector"),
        table([{ label: "Id", mono: true, get: (x) => clip(String(x.id), 30) }, { label: "Score", num: true, get: (x) => fmt(x.score) },
               { label: "", get: (x) => h("span", { class: "small wrap-any" }, x.label) }], hits)));
    }
    fill(detail,
      card(cardHead(clip(String(p.id), 40), isDocs() ? "a chunk of a document" : "a point", btn("Find similar", similar, { kind: "primary", ico: "search" })),
        h("div", { class: "card-pad stack" },
          h("div", { class: "field-label" }, "Payload"), h("pre", { class: "codeblock wrap" }, JSON.stringify(full.payload, null, 2)),
          vec ? h("div", { class: "field-label" }, `Vector (${vec.length} dimensions)`) : null,
          vec ? h("pre", { class: "codeblock wrap" }, `[${vec.slice(0, 12).map((x) => Number(x).toFixed(4)).join(", ")}${vec.length > 12 ? ", …" : ""}]`) : null)),
      near);
  }
  fill(root, h("div", { class: "stack" },
    card(cardHead("Vectors", "Every point of a collection, as the vector store holds it. Read only.", sel),
      h("div", { class: "card-pad" }, filterBar)),
    h("div", { class: "split" }, listCard, detail)));
  await load(true);
}

// ------------------------------------------------------------------ table editor: browse the tenant's SQL tables like a spreadsheet

// an identifier inside a statement sent with parameters: "quoted", and % doubled for the placeholder syntax
const ident = (name) => `"${String(name).replaceAll('"', '""').replaceAll("%", "%%")}"`;
const sql = (query, params, maxRows = 1000) => api("POST", "/v1/sql", { query, params: params || null, read_only: true, max_rows: maxRows });
const bytesText = (n) => {
  if (n === null || n === undefined) return "-";
  const u = ["B", "kB", "MB", "GB", "TB"];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
};

const TABLES_SQL = `SELECT c.relname AS name,
  CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' WHEN 'p' THEN 'partitioned table' ELSE 'table' END AS kind,
  (SELECT count(*) FROM information_schema.columns col WHERE col.table_schema = n.nspname AND col.table_name = c.relname) AS columns,
  CASE WHEN c.relkind IN ('r', 'p', 'm') AND c.reltuples >= 0 THEN c.reltuples::bigint
       WHEN c.relkind IN ('r', 'p', 'm') THEN (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
         n.nspname, c.relname), false, true, '')))[1]::text::bigint END AS rows,
  CASE WHEN c.relkind IN ('r', 'p', 'm') THEN pg_total_relation_size(c.oid) END AS bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname`;

const COLUMNS_SQL = `SELECT c.column_name, c.data_type, c.udt_name, c.is_nullable, c.column_default,
  EXISTS (SELECT 1 FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage k
          ON k.constraint_name = tc.constraint_name AND k.table_schema = tc.table_schema AND k.table_name = tc.table_name
          WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = c.table_schema AND tc.table_name = c.table_name
            AND k.column_name = c.column_name) AS pk
FROM information_schema.columns c WHERE c.table_schema = current_schema() AND c.table_name = %s ORDER BY c.ordinal_position`;

// what each filter operator means in SQL; the value always travels as a parameter
const OPERATORS = [
  ["eq", "equals", (c) => `${c} = %s`], ["neq", "not equal", (c) => `${c} <> %s`],
  ["gt", "greater than", (c) => `${c} > %s`], ["gte", "at least", (c) => `${c} >= %s`],
  ["lt", "less than", (c) => `${c} < %s`], ["lte", "at most", (c) => `${c} <= %s`],
  ["like", "contains", (c) => `${c}::text ILIKE %s`], ["null", "is empty", (c) => `${c} IS NULL`],
  ["notnull", "is not empty", (c) => `${c} IS NOT NULL`]];

function cellView(v, type) {
  if (v === null || v === undefined) return h("span", { class: "null" }, "NULL");
  if (typeof v === "boolean") return h("span", { class: v ? "bool-t" : "bool-f" }, String(v));
  const text = typeof v === "object" ? JSON.stringify(v) : String(v);
  const num = typeof v === "number" || /^(int|float|numeric|money)/.test(type || "");
  return h("span", { class: num ? "num" : null, title: text.length > 60 ? text : null }, text.length > 120 ? `${text.slice(0, 119)}…` : text);
}

function rowDrawer(table, cols, row) {
  closeDrawer();
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const drawer = h("aside", { class: "drawer", role: "dialog", "aria-label": `A row of ${table}` },
    h("div", { class: "row between" }, h("div", {}, h("div", { class: "tiny muted" }, "Row of"), h("h2", { class: "mono" }, table)),
      btn("", closeDrawer, { ico: "x", title: "Close" })),
    h("div", { class: "stack" }, cols.map((c, i) => h("div", { class: "field" },
      h("div", { class: "row between" }, h("span", { class: "field-label" }, c.column_name, c.pk ? h("span", { class: "pk" }, " key") : null),
        h("span", { class: "tiny muted mono" }, c.udt_name)),
      row[i] !== null && typeof row[i] === "object"
        ? h("pre", { class: "codeblock wrap" }, JSON.stringify(row[i], null, 2))
        : h("div", { class: "cell-full" }, cellView(row[i], c.udt_name))))));
  document.body.append(scrim, drawer);
}

async function dataTables(root, selected) {
  const list = await guard(() => sql(TABLES_SQL));
  if (!list) return;
  const tables = list.rows.map(([name, kind, columns, rows, bytes]) => ({ name, kind, columns, rows, bytes }));
  const side = h("div", { class: "card flush tables-side" });
  const main = h("div", { class: "stack" });
  const find = h("input", { placeholder: "Search tables", "aria-label": "Search tables" });
  const drawSide = () => {
    const q = find.value.trim().toLowerCase();
    const shown = tables.filter((t) => !q || t.name.toLowerCase().includes(q));
    fill(side, h("div", { class: "card-pad stack tight" }, h("div", { class: "row between" }, h("b", {}, "Tables"),
        h("span", { class: "tiny muted" }, plural(tables.length, "table"))), find),
      h("div", { class: "table-links" },
        h("a", { href: "#data/tables", class: !selected ? "on" : null }, icon("overview"), "All tables"),
        shown.map((t) => h("a", { href: `#data/tables/${encodeURIComponent(t.name)}`, class: t.name === selected ? "on" : null,
          title: `${t.kind} · ${t.rows === null ? "rows unknown" : `about ${num(t.rows)} rows`}` },
          icon(t.kind === "table" ? "database" : "file"), h("span", { class: "ellipsis" }, t.name),
          h("span", { class: "tiny muted" }, t.rows === null ? "" : num(t.rows))))));
  };
  find.addEventListener("input", drawSide);
  drawSide();
  fill(root, h("div", { class: "tables-layout" }, side, main));
  if (!selected || !tables.some((t) => t.name === selected)) {
    const admin = (await role()) === "admin";
    fill(main, card(cardHead("Database tables", "Every table and view in this tenant's schema. Counts and sizes are Postgres's estimates.",
        admin ? btn("New table", () => tableDesigner(tables, (n) => { location.hash = `data/tables/${encodeURIComponent(n)}`; }), { kind: "primary", ico: "plus" }) : null),
      table([{ label: "Name", mono: true, get: (t) => t.name },
             { label: "Kind", get: (t) => h("span", { class: "small muted" }, t.kind) },
             { label: "Columns", num: true, get: (t) => num(t.columns) },
             { label: "Rows (estimated)", num: true, get: (t) => num(t.rows) },
             { label: "Size (estimated)", num: true, get: (t) => bytesText(t.bytes) }], tables,
        { onRow: (t) => { location.hash = `data/tables/${encodeURIComponent(t.name)}`; },
          emptyView: h("div", { class: "card-pad" }, empty("database", "No tables yet", "Create one in the SQL tab, e.g. CREATE TABLE notes (id int PRIMARY KEY, body text).")) }),
      h("div", { class: "card-pad tiny muted" }, plural(tables.length, "table"))));
    return;
  }
  await tableGrid(main, selected);
}

async function tableGrid(main, name) {
  const colRes = await guard(() => sql(COLUMNS_SQL, [name]));
  if (!colRes) return;
  const cols = colRes.rows.map(([column_name, data_type, udt_name, is_nullable, column_default, pk]) =>
    ({ column_name, data_type, udt_name, nullable: is_nullable === "YES", column_default, pk }));
  const keys = cols.filter((c) => c.pk).map((c) => c.column_name);
  const admin = (await role()) === "admin";
  const editable = admin && keys.length > 0;  // a row is found again by its key: without one, edits could hit several rows
  let page = 0, size = 100, sort = null, filters = [], view = "data", total = null, rows = [], loading = 0, extras = null;
  const box = h("div", { class: "stack" });
  const filterBox = h("div", { class: "stack tight" });

  function whereSql() {
    const parts = [], params = [];
    for (const f of filters) {
      const op = OPERATORS.find((o) => o[0] === f.op);
      parts.push(op[2](ident(f.column)));
      if (!["null", "notnull"].includes(f.op)) params.push(f.op === "like" ? `%${f.value}%` : f.value);
    }
    return { where: parts.length ? ` WHERE ${parts.join(" AND ")}` : "", params };
  }
  async function load() {
    const mine = ++loading;
    const { where, params } = whereSql();
    const order = sort ? ` ORDER BY ${ident(sort.column)} ${sort.desc ? "DESC" : "ASC"} NULLS LAST`
      : keys.length ? ` ORDER BY ${keys.map(ident).join(", ")}` : "";
    const sel = cols.map((c) => ident(c.column_name)).join(", ") || "*";
    const [data, count] = await Promise.all([
      guard(() => sql(`SELECT ${sel} FROM ${ident(name)}${where}${order} LIMIT ${size} OFFSET ${page * size}`, params, size)),
      guard(() => sql(`SELECT count(*) FROM ${ident(name)}${where}`, params))]);
    if (mine !== loading || !data) return;
    rows = data.rows; total = count ? Number(count.rows[0][0]) : null;
    draw();
  }
  function header(c) {
    const on = sort && sort.column === c.column_name;
    return h("th", { class: "click", title: `Sort by ${c.column_name}`, onclick: () => {
      sort = !on ? { column: c.column_name, desc: false } : sort.desc ? null : { column: c.column_name, desc: true };
      page = 0; load();
    } }, h("div", { class: "colhead" }, h("span", {}, c.pk ? h("span", { class: "pk", title: "primary key" }, "⚿ ") : null, c.column_name,
        on ? (sort.desc ? " ↓" : " ↑") : ""), h("span", { class: "coltype" }, c.udt_name)));
  }
  function drawFilters() {
    const column = h("select", { "aria-label": "Column" }, cols.map((c) => h("option", { value: c.column_name }, c.column_name)));
    const op = h("select", { "aria-label": "Operator" }, OPERATORS.map(([k, label]) => h("option", { value: k }, label)));
    const value = h("input", { placeholder: "value", "aria-label": "Value" });
    op.addEventListener("change", () => { value.disabled = ["null", "notnull"].includes(op.value); });
    const add = () => {
      if (!["null", "notnull"].includes(op.value) && value.value === "") return;
      filters.push({ column: column.value, op: op.value, value: value.value });
      page = 0; drawFilters(); load();
    };
    value.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
    fill(filterBox, h("div", { class: "row" }, column, op, value, btn("Add filter", add, { ico: "plus" })),
      filters.length ? h("div", { class: "chips" }, filters.map((f, i) => h("button", { class: "chip", type: "button", title: "Remove this filter",
        onclick: () => { filters.splice(i, 1); page = 0; drawFilters(); load(); } },
        `${f.column} ${OPERATORS.find((o) => o[0] === f.op)[1]}${["null", "notnull"].includes(f.op) ? "" : ` ${f.value}`}  ×`))) : null);
  }
  function draw() {
    const pages = total === null ? null : Math.max(1, Math.ceil(total / size));
    const from = rows.length ? page * size + 1 : 0, to = page * size + rows.length;
    const sizeSel = h("select", { "aria-label": "Rows per page", onchange: (e) => { size = Number(e.target.value); page = 0; load(); } },
      [50, 100, 500].map((n) => h("option", { value: n, selected: n === size }, `${n} rows`)));
    const tabs = h("div", { class: "tabs" }, [["data", "Data"], ["definition", "Definition"], ["api", "API"]].map(([k, l]) =>
      h("button", { type: "button", class: view === k ? "on" : null, onclick: () => {
        view = k; draw();
        if (k === "definition" && !extras) definitionExtras(name).then((x) => { extras = x; draw(); });
      } }, l)));
    const grid = view === "data"
      ? (rows.length ? h("div", { class: "grid-wrap" }, h("table", { class: "sheet" },
          h("thead", {}, h("tr", {}, h("th", { class: "rownum" }, "#"), cols.map(header))),
          h("tbody", {}, rows.map((r, i) => h("tr", { class: "click", title: editable ? "Edit this row" : "See this row",
            onclick: () => (editable ? rowEditor(name, cols, r, keys, load) : rowDrawer(name, cols, r)) },
            h("td", { class: "rownum" }, page * size + i + 1), r.map((v, j) => h("td", {}, cellView(v, cols[j] && cols[j].udt_name))))))))
        : h("div", { class: "card-pad" }, empty("database", filters.length ? "No rows match the filters" : "This table is empty", "")))
      : table([{ label: "Column", mono: true, get: (c) => [c.pk ? h("span", { class: "pk" }, "⚿ ") : null, c.column_name] },
               { label: "Type", mono: true, get: (c) => c.data_type === "USER-DEFINED" ? c.udt_name : c.data_type },
               { label: "Nullable", get: (c) => (c.nullable ? "yes" : h("b", {}, "no")) },
               { label: "Default", mono: true, get: (c) => c.column_default || h("span", { class: "muted" }, "-") }], cols);
    fill(box, card(
      h("div", { class: "card-head" }, h("div", {}, h("h2", { class: "mono" }, name),
          h("div", { class: "sub" }, total === null ? "" : `${num(from)}–${num(to)} of ${num(total)} rows${filters.length ? " matching the filters" : ""} · ${plural(cols.length, "column")}`)),
        h("div", { class: "row" }, tabs, editable && view === "data" ? btn("Insert row", () => rowEditor(name, cols, null, keys, load), { kind: "primary", ico: "plus" }) : null,
          btn("", load, { ico: "refresh", title: "Refresh" }),
          btn("CSV", () => download(`${name}.csv`, csv({ columns: cols.map((c) => c.column_name), rows })), { ico: "download", title: "Download this page as CSV" }))),
      view === "data" ? h("div", { class: "card-pad" }, filterBox) : null,
      view === "data" && admin && !keys.length ? h("div", { class: "card-pad tiny muted" }, "Read-only: this table has no primary key, so a row can't be singled out for editing (the Advisors page explains how to add one).") : null,
      view === "api" ? null : grid,
      view === "data" && total !== null ? h("div", { class: "row between card-pad" },
        h("div", { class: "row" }, btn("Previous", () => { if (page > 0) { page--; load(); } }),
          h("span", { class: "small muted" }, `page ${page + 1} of ${num(pages)}`),
          btn("Next", () => { if (page + 1 < pages) { page++; load(); } })), sizeSel) : null),
      view === "definition" ? extras : null,
      view === "definition" && admin ? columnTools(name, () => tableGrid(main, name)) : null,
      view === "api" ? tableApi(name, cols) : null);
  }
  fill(main, box);
  drawFilters();
  await load();
}

// ------------------------------------------------------------------ connect: how an app, an SDK or an agent reaches this server

function copyButton(text) {
  return btn("Copy", async () => {
    try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Copy isn't allowed here: select the text instead", "info"); }
  }, { ico: "file", title: "Copy to the clipboard" });
}

function openConnect() {
  closeDrawer();
  const url = location.origin;
  const t = state.tenant;
  const tenantArg = t === "default" ? "" : `, tenant="${t}"`;
  const tenantTs = t === "default" ? "" : `, tenant: "${t}"`;
  const tenantHdr = t === "default" ? "" : ` -H "swsdb-tenant: ${t}"`;
  const snippets = [
    ["Python", `pip install swsdb

import os
from swsdb import SwsDB

db = SwsDB("${url}", api_key=os.environ["SWSDB_API_KEY"]${tenantArg})
kb = db.collection("handbook")
print(kb.answer("What is the refund policy?")["answer"])`],
    ["TypeScript", `npm install swsdb

import { SwsDB } from "swsdb";

const db = new SwsDB({ url: "${url}", apiKey: process.env.SWSDB_API_KEY${tenantTs} });
const hits = await db.collection("handbook").query("refund policy", { k: 5 });`],
    ["curl", `curl ${url}/v1/collections/handbook/query \\
  -H "api-key: $SWSDB_API_KEY"${tenantHdr} \\
  -H "content-type: application/json" \\
  -d '{"query": "refund policy", "k": 5}'`],
    ["MCP (agents)", `{
  "mcpServers": {
    "swsdb": {
      "type": "http",
      "url": "${url}/mcp",
      "headers": { "api-key": "<your key>"${t === "default" ? "" : `, "swsdb-tenant": "${t}"`} }
    }
  }
}

Read-only tools at /mcp; tools that add documents and memories at /mcp/rw.`],
    ["LangChain", `pip install "swsdb[langchain]"

import os
from swsdb.langchain import SwsDBRetriever

retriever = SwsDBRetriever(collection="handbook", url="${url}", api_key=os.environ["SWSDB_API_KEY"]${tenantArg}, k=5)
docs = retriever.invoke("What is the refund policy?")`]];
  let current = 0;
  const body = h("div", { class: "stack" });
  const tabs = h("div", { class: "tabs" });
  const draw = () => {
    fill(tabs, snippets.map(([label], i) => h("button", { type: "button", class: i === current ? "on" : null,
      onclick: () => { current = i; draw(); } }, label)));
    const [, code] = snippets[current];
    fill(body, h("div", { class: "row end" }, copyButton(code)), h("pre", { class: "codeblock wrap" }, code));
  };
  draw();
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const drawer = h("aside", { class: "drawer", role: "dialog", "aria-label": "Connect" },
    h("div", { class: "row between" }, h("h2", {}, "Connect to swsdb"), btn("", closeDrawer, { ico: "x", title: "Close" })),
    h("dl", { class: "kv" }, h("dt", {}, "Server"), h("dd", { class: "mono" }, url, " ", copyButton(url)),
      h("dt", {}, "Tenant"), h("dd", { class: "mono" }, t), h("dt", {}, "Key"),
      h("dd", {}, "Send it as the ", h("span", { class: "kbd" }, "api-key"), " header. Make one on the ",
        h("a", { href: "#keys", onclick: closeDrawer }, "API keys"), " page; this page never shows a stored key.")),
    tabs, body,
    h("div", { class: "small muted" }, "The full API is at ", h("a", { href: "/docs", target: "_blank", rel: "noopener" }, "/docs"),
      " (OpenAPI)."));
  document.body.append(scrim, drawer);
}

// ------------------------------------------------------------------ API keys

view.keys = async (root, _arg, actions) => {
  fill(root, skeleton("block", 2));
  const st = await api("GET", "/v1/status");
  const global = st.tenant === null || st.tenant === undefined;
  let all = false, armed = null, created = null;
  const out = h("div", { class: "stack" });
  const name = h("input", { placeholder: "Name, e.g. support-bot", "aria-label": "Key name" });
  const role = h("select", { "aria-label": "Role" }, [["read", "read: search and read"], ["admin", "admin: read and write"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  const tenant = h("input", { value: state.tenant, "aria-label": "Tenant", disabled: !global, title: global ? "" : "A tenant's key makes keys for its own tenant" });
  async function create() {
    const r = await guard(() => api("POST", "/v1/keys", { name: name.value.trim() || null, role: role.value, tenant: tenant.value.trim() || null }));
    if (!r) return;
    created = r; name.value = ""; load();
  }
  async function load() {
    const r = await guard(() => api("GET", `/v1/keys?all_tenants=${all ? "true" : "false"}`));
    if (!r) return;
    const keys = r.keys;
    const active = keys.filter((k) => !k.revoked_at);
    fill(out,
      created ? card(h("div", { class: "card-pad stack" }, h("div", { class: "row between" }, h("b", {}, `Key ${created.id} (${created.role}, ${created.tenant})`),
          btn("", () => { created = null; load(); }, { ico: "x", title: "Dismiss" })),
        h("div", { class: "small" }, "Copy it now: swsdb keeps only a hash, so it can't show this key again."),
        h("div", { class: "row" }, h("code", { class: "codeblock wrap grow" }, created.key), copyButton(created.key)))) : null,
      card(cardHead("Keys", `${num(active.length)} active · ${num(keys.length - active.length)} revoked`,
          global ? h("label", { class: "row small muted" }, h("input", { type: "checkbox", checked: all,
            onchange: (e) => { all = e.target.checked; load(); } }), "every tenant") : null),
        table([
          { label: "Id", mono: true, get: (k) => k.id },
          { label: "Name", get: (k) => k.name || h("span", { class: "muted" }, "-") },
          { label: "Tenant", mono: true, get: (k) => k.tenant },
          { label: "Role", get: (k) => pill(k.role, k.role === "admin" ? "warn" : "info", false) },
          { label: "Created", get: (k) => h("span", { class: "muted", title: new Date(k.created_at).toLocaleString() }, rel(k.created_at)) },
          { label: "Last used", get: (k) => (k.last_used_at ? h("span", { title: new Date(k.last_used_at).toLocaleString() }, rel(k.last_used_at)) : h("span", { class: "muted" }, "never")) },
          { label: "", num: true, get: (k) => (k.revoked_at ? pill("revoked", "bad", false)
            : armed === k.id
              ? h("div", { class: "row end" }, btn("Cancel", (e) => { e.stopPropagation(); armed = null; load(); }),
                  btn("Revoke now", async (e) => { e.stopPropagation(); if (await guard(() => api("DELETE", `/v1/keys/${encodeURIComponent(k.id)}`))) { toast(`Revoked ${k.id}`, "info"); armed = null; load(); } }, { kind: "danger", ico: "trash" }))
              : btn("Revoke", (e) => { e.stopPropagation(); armed = k.id; load(); }, { ico: "trash" })) },
        ], keys, { emptyView: h("div", { class: "card-pad" }, empty("key", "No managed keys yet", "Make one below. Keys from the environment (SWSDB_API_KEY) aren't listed.")) })));
  }
  fill(actions, btn("Refresh", load, { ico: "refresh" }));
  fill(root, out, card(cardHead("New key", "A key reaches one tenant. Revoking it works at once, on every server."),
    h("div", { class: "card-pad row" }, name, role, tenant, btn("Create key", create, { kind: "primary", ico: "key" }))));
  await load();
};

// ------------------------------------------------------------------ advisors

view.advisors = async (root, arg, actions) => {
  const cat = arg === "performance" ? "performance" : "security";
  fill(actions, h("div", { class: "tabs" }, [["security", "Security"], ["performance", "Performance"]].map(([k, l]) =>
    h("button", { type: "button", class: k === cat ? "on" : null, onclick: () => { location.hash = `advisors/${k}`; } }, l))),
    btn("Run again", () => route(), { ico: "refresh" }));
  fill(root, skeleton("block", 3));
  const r = await api("GET", "/v1/advisors");
  const mine = r.findings.filter((f) => f.category === cat);
  const count = (lvl) => mine.filter((f) => f.level === lvl).length;
  const kind = { error: "bad", warning: "warn", info: "info" };
  const tile = (lvl, label) => card(h("div", { class: "stat" }, h("div", { class: "label" }, label),
    h("div", { class: "value" }, num(count(lvl))), h("div", { class: "foot" }, `${cat} · ${{ error: "fix these", warning: "worth fixing", info: "consider" }[lvl]}`)));
  fill(root,
    h("div", { class: "grid c3" }, tile("error", "Errors"), tile("warning", "Warnings"), tile("info", "Suggestions")),
    mine.length ? mine.map((f) => card(h("div", { class: "card-pad stack tight" },
      h("div", { class: "row between" }, h("div", { class: "row" }, pill(f.level, kind[f.level]), h("b", {}, f.title)),
        h("span", { class: "tiny muted mono" }, f.check)),
      h("div", { class: "small" }, f.detail),
      h("div", { class: "small" }, h("b", {}, "What to do: "), f.fix),
      f.items.length ? h("details", {}, h("summary", { class: "small muted" }, plural(f.items.length, "item")),
        h("ul", { class: "small mono" }, f.items.map((x) => h("li", {}, x)))) : null)))
      : card(empty("shield", `No ${cat} issues found`, `${plural(r.checks.length, "check")} ran (${r.scope}).`)),
    h("div", { class: "tiny muted" }, `Checked ${r.scope} for tenant ${r.tenant}: ${r.checks.join(", ")}.`));
};

// ------------------------------------------------------------------ logs and query performance

view.logs = async (root, arg, actions) => {
  const tabs = h("div", { class: "tabs" }, [["requests", "API requests"], ["database", "Database queries"]].map(([k, l]) =>
    h("button", { type: "button", class: (arg === "database" ? "database" : "requests") === k ? "on" : null,
                  onclick: () => { location.hash = k === "database" ? "logs/database" : "logs"; } }, l)));
  if (arg === "database") { fill(actions, tabs, btn("Refresh", () => route(), { ico: "refresh" })); return dbQueries(root); }
  let status = "", routeQ = "", minMs = 0, live = false;
  const out = h("div", { class: "stack" });
  const statusSel = h("select", { "aria-label": "Status", onchange: (e) => { status = e.target.value; load(); } },
    [["", "Every status"], ["2xx", "2xx success"], ["4xx", "4xx refused"], ["5xx", "5xx errors"]].map(([v, l]) => h("option", { value: v }, l)));
  const routeIn = h("input", { placeholder: "Route contains, e.g. query", "aria-label": "Route" });
  let typing;
  routeIn.addEventListener("input", () => { clearTimeout(typing); typing = setTimeout(() => { routeQ = routeIn.value.trim(); load(); }, 250); });
  const slowSel = h("select", { "aria-label": "Slower than", onchange: (e) => { minMs = Number(e.target.value); load(); } },
    [[0, "Any time"], [100, "≥ 100 ms"], [500, "≥ 500 ms"], [1000, "≥ 1 s"]].map(([v, l]) => h("option", { value: v }, l)));
  const liveBox = h("input", { type: "checkbox", "aria-label": "Live", onchange: (e) => { live = e.target.checked; if (live) tick(); } });
  function tick() { if (live && currentPage() === "logs") { load(); state.timers.push(setTimeout(tick, 3000)); } }
  const statusKind = (c) => (c >= 500 ? "bad" : c >= 400 ? "warn" : "ok");
  async function load() {
    const p = new URLSearchParams({ limit: 300 });
    if (status) p.set("status", status);
    if (routeQ) p.set("route", routeQ);
    if (minMs) p.set("min_ms", minMs);
    const r = await guard(() => api("GET", `/v1/logs?${p}`));
    if (!r) return;
    const most = Math.max(1, ...r.routes.map((x) => x.total_ms));
    fill(out,
      card(cardHead("Query performance", "Time spent per route among the requests shown, most first: where to look when things are slow"),
        table([
          { label: "Route", mono: true, get: (x) => `${x.method} ${x.route}` },
          { label: "Calls", num: true, get: (x) => num(x.calls) },
          { label: "Errors", num: true, get: (x) => (x.errors ? pill(num(x.errors), "bad", false) : x.client_errors ? pill(num(x.client_errors), "warn", false) : "0") },
          { label: "Mean", num: true, get: (x) => `${x.mean_ms} ms` },
          { label: "p95", num: true, get: (x) => `${x.p95_ms} ms` },
          { label: "Max", num: true, get: (x) => `${x.max_ms} ms` },
          { label: "Time consumed", get: (x) => {
            const b = h("i"); b.style.width = `${Math.max(2, (x.total_ms / most) * 100)}%`;
            return h("div", { class: "bar-row nolabel", title: `${num(Math.round(x.total_ms))} ms in all` }, h("span", { class: "bar-track" }, b),
              h("span", { class: "bar-value" }, `${num(Math.round(x.total_ms))} ms`));
          } }], r.routes, { emptyView: h("div", { class: "card-pad muted small" }, "No requests match.") })),
      card(cardHead("Requests", `${num(r.entries.length)} newest of ${num(r.matched)} matching · this server keeps the last ${num(r.kept)}`),
        table([
          { label: "Time", get: (e) => h("span", { class: "mono small", title: new Date(e.at * 1000).toLocaleString() }, new Date(e.at * 1000).toLocaleTimeString()) },
          { label: "Status", get: (e) => pill(String(e.status), statusKind(e.status), false) },
          { label: "Method", mono: true, get: (e) => e.method },
          { label: "Route", mono: true, get: (e) => e.route },
          { label: "Time", num: true, get: (e) => `${e.ms} ms` },
          { label: "Tenant", mono: true, get: (e) => e.tenant || h("span", { class: "muted" }, "-") },
          { label: "Key", mono: true, get: (e) => e.key || h("span", { class: "muted" }, e.role ? `${e.role} (environment)` : "-") }],
          r.entries, { emptyView: h("div", { class: "card-pad muted small" }, "No requests match.") })));
  }
  fill(actions, tabs, btn("Refresh", load, { ico: "refresh" }));
  fill(root, card(h("div", { class: "card-pad row" }, statusSel, routeIn, slowSel, h("label", { class: "row small muted" }, liveBox, "live"),
    h("span", { class: "tiny muted" }, "Routes, statuses and times only: no request bodies, parameters or key values are kept."))), out);
  await load();
};

// ------------------------------------------------------------------ schema visualizer

const SCHEMA_SQL = `SELECT c.table_name, c.column_name, c.udt_name, c.is_nullable,
  EXISTS (SELECT 1 FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage k
          ON k.constraint_name = tc.constraint_name AND k.table_schema = tc.table_schema AND k.table_name = tc.table_name
          WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = c.table_schema AND tc.table_name = c.table_name
            AND k.column_name = c.column_name) AS pk
FROM information_schema.columns c JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE c.table_schema = current_schema() AND t.table_type = 'BASE TABLE' ORDER BY c.table_name, c.ordinal_position`;
const FKEYS_SQL = `SELECT cl.relname, ref.relname, a.attname, ra.attname, con.conname
FROM pg_constraint con JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_class ref ON ref.oid = con.confrelid
JOIN pg_namespace n ON n.oid = con.connamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) AS k(col, refcol)
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.col
JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = k.refcol
WHERE con.contype = 'f' AND n.nspname = current_schema()`;

async function dataSchema(root) {
  const [colsR, fkR] = await Promise.all([guard(() => sql(SCHEMA_SQL, null, 5000)), guard(() => sql(FKEYS_SQL, null, 2000))]);
  if (!colsR || !fkR) return;
  const tables = new Map();
  for (const [t, c, type, nullable, pk] of colsR.rows) {
    if (!tables.has(t)) tables.set(t, []);
    tables.get(t).push({ name: c, type, nullable: nullable === "YES", pk });
  }
  const fks = fkR.rows.map(([from, to, col, refcol, conname]) => ({ from, to, col, refcol, conname }));
  if (!tables.size) { fill(root, card(empty("database", "No tables yet", "Create one in the SQL tab; its columns and foreign keys show here."))); return; }
  // columns of boxes by dependency depth: a table goes right of the tables it points to
  const names = [...tables.keys()];
  const depth = new Map(names.map((n) => [n, 0]));
  for (let i = 0; i < names.length; i++) for (const f of fks) if (f.from !== f.to && depth.get(f.from) <= depth.get(f.to)) depth.set(f.from, depth.get(f.to) + 1);
  const BW = 230, RH = 20, HH = 30, GX = 90, GY = 28;
  const cols = new Map();
  for (const n of names.sort()) { const d = Math.min(depth.get(n), 6); if (!cols.has(d)) cols.set(d, []); cols.get(d).push(n); }
  const pos = new Map();
  let W = 0, H = 0;
  for (const [d, list] of [...cols.entries()].sort((a, b) => a[0] - b[0])) {
    let y = 20;
    for (const n of list) {
      const hgt = HH + tables.get(n).length * RH + 8;
      pos.set(n, { x: 20 + d * (BW + GX), y, h: hgt });
      y += hgt + GY;
    }
    W = Math.max(W, 20 + d * (BW + GX) + BW + 20); H = Math.max(H, y);
  }
  const svg = s("svg", { class: "schemaview", viewBox: `0 0 ${W} ${H}`, width: W, height: H });
  const rowY = (t, col) => { const p = pos.get(t); const i = tables.get(t).findIndex((c) => c.name === col); return p.y + HH + i * RH + RH / 2 + 4; };
  for (const f of fks) {
    const a = pos.get(f.from), b = pos.get(f.to);
    if (!a || !b) continue;
    const y1 = rowY(f.from, f.col), y2 = rowY(f.to, f.refcol);
    const x1 = a.x, x2 = b.x + BW;
    const back = x1 > x2;
    const d = back ? `M${x1},${y1} C${x1 - 50},${y1} ${x2 + 50},${y2} ${x2},${y2}` : `M${a.x + BW},${y1} C${a.x + BW + 40},${y1} ${b.x - 40},${y2} ${b.x},${y2}`;
    svg.append(s("path", { d, class: "fk" }, s("title", {}, `${f.from}.${f.col} → ${f.to}.${f.refcol} (${f.conname})`)));
  }
  for (const [n, list] of tables) {
    const p = pos.get(n);
    const g = s("g", { class: "tbox", transform: `translate(${p.x},${p.y})` });
    g.append(s("rect", { width: BW, height: p.h, rx: 8, class: "frame" }), s("rect", { width: BW, height: HH, rx: 8, class: "head" }),
      s("text", { x: 12, y: 20, class: "tname" }, n));
    list.forEach((c, i) => {
      const y = HH + i * RH + 14;
      g.append(s("text", { x: 12, y, class: c.pk ? "cname pkcol" : "cname" }, `${c.pk ? "⚿ " : ""}${c.name}`),
        s("text", { x: BW - 10, y, "text-anchor": "end", class: "ctype" }, `${c.type}${c.nullable ? "" : " !"}`));
    });
    g.addEventListener("click", () => { location.hash = `data/tables/${encodeURIComponent(n)}`; });
    svg.append(g);
  }
  fill(root, card(cardHead("Schema", `${plural(tables.size, "table")} · ${plural(fks.length, "foreign key")}. Lines link a foreign key to the column it references; ! marks NOT NULL. Click a table to open it.`),
    h("div", { class: "schema-wrap" }, svg)));
}

// table definition extras: indexes and constraints
const INDEXES_SQL = `SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = %s ORDER BY indexname`;
const CONSTRAINTS_SQL = `SELECT con.conname, CASE con.contype WHEN 'p' THEN 'primary key' WHEN 'f' THEN 'foreign key' WHEN 'u' THEN 'unique'
  WHEN 'c' THEN 'check' WHEN 'x' THEN 'exclusion' ELSE con.contype::text END, pg_get_constraintdef(con.oid)
FROM pg_constraint con JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace
WHERE n.nspname = current_schema() AND cl.relname = %s ORDER BY 2, 1`;

async function definitionExtras(name) {
  const [idx, con] = await Promise.all([guard(() => sql(INDEXES_SQL, [name])), guard(() => sql(CONSTRAINTS_SQL, [name]))]);
  return [
    card(cardHead("Indexes", idx ? plural(idx.rows.length, "index") : ""),
      table([{ label: "Name", mono: true, get: (r) => r[0] }, { label: "Definition", mono: true, get: (r) => h("span", { class: "wrap-any" }, r[1]) }],
        idx ? idx.rows : [], { emptyView: h("div", { class: "card-pad muted small" }, "No indexes.") })),
    card(cardHead("Constraints", con ? plural(con.rows.length, "constraint") : ""),
      table([{ label: "Name", mono: true, get: (r) => r[0] }, { label: "Kind", get: (r) => r[1] },
             { label: "Definition", mono: true, get: (r) => h("span", { class: "wrap-any" }, r[2]) }],
        con ? con.rows : [], { emptyView: h("div", { class: "card-pad muted small" }, "No constraints.") }))];
}

// ------------------------------------------------------------------ SQL editor with saved queries and history

const HISTORY_KEY = "swsdb.sqlhistory";
const history = {
  list() { try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || "[]"); } catch { return []; } },
  add(q) {
    try {
      const items = [q, ...this.list().filter((x) => x !== q)].slice(0, 25);
      localStorage.setItem(HISTORY_KEY, JSON.stringify(items));
    } catch { /* private window: history is a convenience */ }
  },
};

async function dataSql(root) {
  const q = h("textarea", { class: "mono", "aria-label": "SQL", spellcheck: "false" });
  q.value = "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() ORDER BY 1";
  const ro = h("input", { type: "checkbox", checked: true, "aria-label": "Read only" });
  const out = h("div", { class: "stack" });
  const side = h("div", { class: "stack" });
  const saveName = h("input", { placeholder: "Name to save as", "aria-label": "Query name" });
  let saved = [], tab = "saved";
  async function run() {
    const started = performance.now();
    const r = await guard(() => api("POST", "/v1/sql", { query: q.value, read_only: ro.checked, max_rows: 500 }));
    if (!r) return;
    history.add(q.value.trim());
    const took = Math.round(performance.now() - started);
    fill(out, resultChart(r), card(cardHead("Result", `${plural(r.row_count, "row")}${r.truncated ? " (first 500)" : ""} · ${took} ms`,
      r.columns.length ? btn("CSV", () => download("result.csv", csv(r)), { ico: "download", title: "Download these rows as CSV" }) : null),
      resultTable(r)));
    if (tab === "history") drawSide();
  }
  async function loadSaved() {
    const r = await guard(() => api("GET", "/v1/sql/saved"));
    saved = r ? r.queries : [];
    drawSide();
  }
  async function save() {
    const n = saveName.value.trim();
    if (!n) { showError("Give the query a name to save it."); return; }
    if (await guard(() => api("PUT", `/v1/sql/saved/${encodeURIComponent(n)}`, { query: q.value }))) { toast(`Saved ${n}`); saveName.value = ""; tab = "saved"; loadSaved(); }
  }
  function drawSide() {
    const tabs = h("div", { class: "tabs" }, [["saved", `Saved (${saved.length})`], ["history", "History"]].map(([k, l]) =>
      h("button", { type: "button", class: tab === k ? "on" : null, onclick: () => { tab = k; drawSide(); } }, l)));
    const items = tab === "saved"
      ? saved.map((x) => h("div", { class: "query-item" },
          h("button", { type: "button", class: "query-load", title: x.query, onclick: () => { q.value = x.query; run(); } },
            h("b", {}, x.name), h("span", { class: "tiny muted ellipsis" }, x.query)),
          h("div", { class: "row" }, btn(x.pinned ? "★" : "☆", async () => {
            if (await guard(() => api("PUT", `/v1/sql/saved/${encodeURIComponent(x.name)}`, { query: x.query, pinned: !x.pinned }))) {
              toast(x.pinned ? `Removed ${x.name} from reports` : `Pinned ${x.name} to reports`); loadSaved(); }
          }, { title: x.pinned ? "Unpin from Reports" : "Pin to Reports" }),
          btn("", async () => { if (await guard(() => api("DELETE", `/v1/sql/saved/${encodeURIComponent(x.name)}`))) { toast(`Deleted ${x.name}`, "info"); loadSaved(); } },
            { ico: "trash", title: `Delete ${x.name}` }))))
      : history.list().map((x) => h("div", { class: "query-item" },
          h("button", { type: "button", class: "query-load", title: x, onclick: () => { q.value = x; run(); } },
            h("span", { class: "tiny mono ellipsis" }, x))));
    fill(side, card(h("div", { class: "card-pad stack tight" }, tabs,
      items.length ? items : h("div", { class: "small muted" }, tab === "saved"
        ? "Nothing saved yet: name a query above and save it. Saved queries are shared by everyone with this tenant's keys."
        : "Queries you run in this browser show here."))));
  }
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run();
    if (e.key === "s" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveName.focus(); }
  });
  const examples = [
    ["Rows per table", "SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', table_name), " +
      "false, true, '')))[1]::text::bigint AS rows FROM information_schema.tables WHERE table_schema = current_schema() ORDER BY 2 DESC"],
    ["Columns", "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema() " +
      "ORDER BY table_name, ordinal_position"],
    ["Table sizes", "SELECT relname AS table, pg_size_pretty(pg_total_relation_size(c.oid)) AS size, pg_total_relation_size(c.oid) AS bytes " +
      "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema() AND c.relkind = 'r' ORDER BY bytes DESC"]];
  fill(root, h("div", { class: "split" },
    h("div", { class: "stack" },
      card(cardHead("SQL", "Runs as this tenant's own Postgres role: its tables only. Ctrl+Enter runs, Ctrl+S saves.",
          h("label", { class: "row small muted" }, ro, "read only"), btn("Run", run, { kind: "primary", ico: "play", title: "Run (Ctrl+Enter)" })),
        h("div", { class: "card-pad stack" }, q,
          h("div", { class: "row between" }, h("div", { class: "chips" }, examples.map(([label, text]) =>
            h("button", { class: "chip", type: "button", onclick: () => { q.value = text; run(); } }, label))),
            h("div", { class: "row" }, saveName, btn("Save", save, { ico: "plus", title: "Save this query for the tenant" }))))),
      out),
    side));
  const handed = sessionStorage.getItem("swsdb.sqlload");  // from Reports or the command palette
  if (handed) { q.value = handed; sessionStorage.removeItem("swsdb.sqlload"); }
  await loadSaved();
  run();
}

// ------------------------------------------------------------------ writing to tables (admin keys only; the server refuses writes from read keys)

const sqlWrite = (query, params) => api("POST", "/v1/sql", { query, params: params || null, read_only: false });
async function role() {
  const key = sessionStorage.getItem("swsdb.key") || "";
  if (!state.role || state.roleKey !== key) {
    state.role = (await api("GET", "/v1/status").catch(() => ({}))).role || "read";
    state.roleKey = key;
  }
  return state.role;
}

// one field of a row form: a typed input, and "NULL" for columns that may be empty
function fieldInput(c, value, isNew) {
  const isJson = /^json/.test(c.udt_name), isBool = c.udt_name === "bool";
  const text = value === null || value === undefined ? "" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
  const input = isBool
    ? h("select", { "aria-label": c.column_name }, [["", isNew ? "(default)" : "-"], ["true", "true"], ["false", "false"]].map(([v, l]) =>
        h("option", { value: v, selected: !isNew && String(value) === v }, l)))
    : isJson ? h("textarea", { class: "mono", rows: 4, "aria-label": c.column_name }, text)
      : h("input", { value: isNew ? "" : text, "aria-label": c.column_name,
                     placeholder: isNew ? (c.column_default ? `default: ${c.column_default}` : c.nullable ? "empty" : "required") : "" });
  if (isJson && isNew) input.value = "";
  const nul = c.nullable ? h("input", { type: "checkbox", checked: !isNew && (value === null || value === undefined), title: "NULL" }) : null;
  if (nul) for (const ev of ["input", "change"]) input.addEventListener(ev, () => { nul.checked = false; });  // a value replaces NULL
  return { el: h("div", { class: "row" }, h("div", { class: "grow" }, input), nul ? h("label", { class: "row tiny muted" }, nul, "NULL") : null),
    read: () => (nul && nul.checked ? { set: true, value: null }
      : input.value === "" ? { set: false } : { set: true, value: input.value }),
    original: text, isNull: value === null || value === undefined };
}

function rowEditor(table, cols, row, keys, done) {
  closeDrawer();
  const isNew = row === null;
  const fields = cols.map((c, i) => ({ c, f: fieldInput(c, isNew ? null : row[i], isNew) }));
  let arm = false;
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const drawer = h("aside", { class: "drawer", role: "dialog", "aria-label": isNew ? `New row in ${table}` : `Edit a row of ${table}` });
  const where = () => {
    const parts = [], params = [];
    for (const k of keys) { const i = cols.findIndex((c) => c.column_name === k); parts.push(`${ident(k)} = %s`); params.push(row[i] === null ? null : String(row[i])); }
    return { sql: parts.join(" AND "), params };
  };
  async function save() {
    const set = [], params = [];
    for (const { c, f } of fields) {
      const v = f.read();
      if (isNew) { if (v.set) { set.push(c.column_name); params.push(v.value); } continue; }
      const changed = v.set ? (v.value === null ? !f.isNull : f.isNull || v.value !== f.original) : false;
      if (changed) { set.push(c.column_name); params.push(v.value); }
    }
    let r;
    if (isNew) {
      r = await guard(() => sqlWrite(set.length
        ? `INSERT INTO ${ident(table)} (${set.map(ident).join(", ")}) VALUES (${set.map(() => "%s").join(", ")})`
        : `INSERT INTO ${ident(table)} DEFAULT VALUES`, params));
    } else {
      if (!set.length) { toast("Nothing changed", "info"); return; }
      const w = where();
      r = await guard(() => sqlWrite(`UPDATE ${ident(table)} SET ${set.map((c) => `${ident(c)} = %s`).join(", ")} WHERE ${w.sql}`, [...params, ...w.params]));
    }
    if (r) { toast(isNew ? "Row added" : `Row updated (${plural(r.row_count, "row")})`); closeDrawer(); done(); }
  }
  async function remove() {
    if (!arm) { arm = true; draw(); return; }
    const w = where();
    const r = await guard(() => sqlWrite(`DELETE FROM ${ident(table)} WHERE ${w.sql}`, w.params));
    if (r) { toast(`Deleted ${plural(r.row_count, "row")}`, "info"); closeDrawer(); done(); }
  }
  function draw() {
    fill(drawer,
      h("div", { class: "row between" }, h("div", {}, h("div", { class: "tiny muted" }, isNew ? "New row in" : "Edit a row of"), h("h2", { class: "mono" }, table)),
        btn("", closeDrawer, { ico: "x", title: "Close" })),
      h("div", { class: "stack" }, fields.map(({ c, f }) => h("div", { class: "field" },
        h("div", { class: "row between" }, h("span", { class: "field-label" }, c.column_name, c.pk ? h("span", { class: "pk" }, " key") : null),
          h("span", { class: "tiny muted mono" }, `${c.udt_name}${c.nullable ? "" : " · required"}`)), f.el))),
      h("div", { class: "row between" },
        isNew ? h("span", { class: "tiny muted" }, "Leave a field empty for its default.") : btn(arm ? "Delete this row now" : "Delete row", remove, { kind: arm ? "danger" : "ghost", ico: "trash" }),
        h("div", { class: "row" }, btn("Cancel", closeDrawer), btn(isNew ? "Add row" : "Save", save, { kind: "primary", ico: "check" }))));
  }
  draw();
  document.body.append(scrim, drawer);
}

// ------------------------------------------------------------------ table designer

const COLUMN_TYPES = ["bigint", "integer", "text", "varchar(255)", "numeric", "double precision", "boolean", "date", "timestamptz",
  "jsonb", "uuid", "text[]"];
const NAME_OK = /^[a-z_][a-z0-9_]{0,62}$/;

function tableDesigner(tables, done) {
  closeDrawer();
  const name = h("input", { placeholder: "table_name", "aria-label": "Table name", class: "mono" });
  const cols = [{ name: "id", type: "bigint", def: "", notNull: true, pk: true, unique: false, ref: "" },
                { name: "created_at", type: "timestamptz", def: "now()", notNull: true, pk: false, unique: false, ref: "" }];
  const preview = h("pre", { class: "codeblock wrap" });
  const list = h("div", { class: "stack tight" });
  const refs = tables.map((t) => t.name);
  function statement() {
    const n = name.value.trim();
    const lines = cols.filter((c) => c.name.trim()).map((c) => {
      const pkIdentity = c.pk && /^(bigint|integer)$/.test(c.type) && !c.def;
      return [`  ${ident(c.name.trim()).replaceAll("%%", "%")} ${c.type}`, pkIdentity ? "GENERATED BY DEFAULT AS IDENTITY" : "",
        c.def ? `DEFAULT ${c.def}` : "", c.notNull && !c.pk ? "NOT NULL" : "", c.unique && !c.pk ? "UNIQUE" : "",
        c.ref ? `REFERENCES ${ident(c.ref).replaceAll("%%", "%")}` : ""].filter(Boolean).join(" ");
    });
    const pks = cols.filter((c) => c.pk && c.name.trim()).map((c) => ident(c.name.trim()).replaceAll("%%", "%"));
    if (pks.length) lines.push(`  PRIMARY KEY (${pks.join(", ")})`);
    return `CREATE TABLE ${ident(n || "table_name").replaceAll("%%", "%")} (\n${lines.join(",\n")}\n)`;
  }
  function problems() {
    const out = [];
    if (!NAME_OK.test(name.value.trim())) out.push("The table name: lowercase letters, digits and _ (starting with a letter or _).");
    if (tables.some((t) => t.name === name.value.trim())) out.push("A table with this name exists.");
    const names = cols.map((c) => c.name.trim()).filter(Boolean);
    if (!names.length) out.push("Add at least one column.");
    if (names.some((x) => !NAME_OK.test(x))) out.push("Column names: lowercase letters, digits and _.");
    if (new Set(names).size !== names.length) out.push("Two columns have the same name.");
    return out;
  }
  function draw() {
    fill(list, cols.map((c, i) => {
      const nm = h("input", { value: c.name, class: "mono", "aria-label": "Column name", oninput: (e) => { c.name = e.target.value; show(); } });
      const ty = h("select", { "aria-label": "Type", onchange: (e) => { c.type = e.target.value; show(); } },
        COLUMN_TYPES.map((x) => h("option", { value: x, selected: x === c.type }, x)));
      const df = h("input", { value: c.def, placeholder: "default", class: "mono", "aria-label": "Default", oninput: (e) => { c.def = e.target.value; show(); } });
      const ref = h("select", { "aria-label": "References", onchange: (e) => { c.ref = e.target.value; show(); } },
        h("option", { value: "" }, "no reference"), refs.map((x) => h("option", { value: x, selected: x === c.ref }, `→ ${x}`)));
      const box = (label, key) => h("label", { class: "row tiny muted" }, h("input", { type: "checkbox", checked: c[key],
        onchange: (e) => { c[key] = e.target.checked; show(); } }), label);
      return h("div", { class: "designer-row" }, nm, ty, df, ref, box("key", "pk"), box("required", "notNull"), box("unique", "unique"),
        btn("", () => { cols.splice(i, 1); draw(); show(); }, { ico: "trash", title: "Remove this column" }));
    }));
  }
  function show() {
    preview.textContent = statement();
    const p = problems();
    fill(issues, p.map((x) => h("div", { class: "tiny warn-text" }, x)));
  }
  const issues = h("div", { class: "stack tight" });
  async function create() {
    if (problems().length) { show(); return; }
    const r = await guard(() => sqlWrite(statement()));
    if (r) { toast(`Created ${name.value.trim()}`); closeDrawer(); done(name.value.trim()); }
  }
  name.addEventListener("input", show);
  draw(); show();
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const drawer = h("aside", { class: "drawer wide", role: "dialog", "aria-label": "New table" },
    h("div", { class: "row between" }, h("h2", {}, "New table"), btn("", closeDrawer, { ico: "x", title: "Close" })),
    h("div", { class: "field-label" }, "Name"), name,
    h("div", { class: "row between" }, h("div", { class: "field-label" }, "Columns"),
      btn("Add column", () => { cols.push({ name: "", type: "text", def: "", notNull: false, pk: false, unique: false, ref: "" }); draw(); show(); }, { ico: "plus" })),
    list,
    h("div", { class: "field-label" }, "This is what runs"), preview, issues,
    h("div", { class: "row end" }, btn("Cancel", closeDrawer), btn("Create table", create, { kind: "primary", ico: "check" })));
  document.body.append(scrim, drawer);
}

function columnTools(table, done) {
  const n = h("input", { placeholder: "new_column", class: "mono", "aria-label": "New column name" });
  const ty = h("select", { "aria-label": "Type" }, COLUMN_TYPES.map((x) => h("option", { value: x }, x)));
  const df = h("input", { placeholder: "default (optional)", class: "mono", "aria-label": "Default" });
  const confirmName = h("input", { placeholder: `type ${table} to delete it`, class: "mono", "aria-label": "Confirm table name" });
  return card(cardHead("Change the table", "Runs as this tenant's role. Deleting a table can't be undone (take a snapshot first)."),
    h("div", { class: "card-pad stack" },
      h("div", { class: "row" }, n, ty, df, btn("Add column", async () => {
        if (!NAME_OK.test(n.value.trim())) { showError("Column names: lowercase letters, digits and _."); return; }
        const stmt = `ALTER TABLE ${ident(table).replaceAll("%%", "%")} ADD COLUMN ${ident(n.value.trim()).replaceAll("%%", "%")} ${ty.value}${df.value.trim() ? ` DEFAULT ${df.value.trim()}` : ""}`;
        if (await guard(() => sqlWrite(stmt))) { toast(`Added ${n.value.trim()}`); done(); }
      }, { ico: "plus" })),
      h("div", { class: "row" }, confirmName, btn("Delete table", async () => {
        if (confirmName.value.trim() !== table) { showError(`Type ${table} to confirm.`); return; }
        if (await guard(() => sqlWrite(`DROP TABLE ${ident(table).replaceAll("%%", "%")}`))) { toast(`Deleted ${table}`, "info"); location.hash = "data/tables"; }
      }, { kind: "danger", ico: "trash" }))));
}

// how to reach one table from code
function tableApi(name, cols) {
  const url = location.origin, t = state.tenant;
  const col = (cols.find((c) => !c.pk) || cols[0] || { column_name: "id" }).column_name;
  const q = `SELECT * FROM "${name.replaceAll('"', '""')}" WHERE "${col}" = %s LIMIT 100`;
  const snippets = [
    ["Python", `from swsdb import SwsDB\n\ndb = SwsDB("${url}", api_key=KEY${t === "default" ? "" : `, tenant="${t}"`})\nrows = db.sql(${JSON.stringify(q)}, [value], read_only=True)["rows"]`],
    ["TypeScript", `const db = new SwsDB({ url: "${url}", apiKey: KEY${t === "default" ? "" : `, tenant: "${t}"`} });\nconst { rows } = await db.sql(${JSON.stringify(q)}, [value], { readOnly: true });`],
    ["curl", `curl ${url}/v1/sql -H "api-key: $SWSDB_API_KEY"${t === "default" ? "" : ` -H "swsdb-tenant: ${t}"`} \\\n  -H "content-type: application/json" \\\n  -d '${JSON.stringify({ query: q, params: ["value"], read_only: true })}'`],
    ["Data API (REST)", `# rows, filtered and sorted (a key sees every row; a signed-in user, what the table's policies allow)\ncurl "${url}/v1/rest/${name}?${col}=eq.value&order=${col}.desc&limit=100" \\\n  -H "Authorization: Bearer $KEY_OR_ACCESS_TOKEN"${t === "default" ? "" : ` -H "swsdb-tenant: ${t}"`}\n\n# insert, update (with a filter), delete (with a filter)\ncurl -X POST "${url}/v1/rest/${name}" -H "Authorization: Bearer $TOKEN" -d '{"${col}": "value"}'\ncurl -X PATCH "${url}/v1/rest/${name}?${col}=eq.value" -H "Authorization: Bearer $TOKEN" -d '{"${col}": "new"}'\ncurl -X DELETE "${url}/v1/rest/${name}?${col}=eq.value" -H "Authorization: Bearer $TOKEN"`]];
  return card(cardHead("Use this table from code", "Values go in params (never into the SQL text); read_only refuses writes."),
    h("div", { class: "card-pad stack" }, snippets.map(([label, code]) => h("div", { class: "stack tight" },
      h("div", { class: "row between" }, h("b", {}, label), copyButton(code)), h("pre", { class: "codeblock wrap" }, code))),
      h("div", { class: "tiny muted" }, "Columns: ", cols.map((c) => `${c.column_name} (${c.udt_name})`).join(", "))));
}

// ------------------------------------------------------------------ database objects

const OBJECTS = {
  functions: ["Functions", `SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments, pg_get_function_result(p.oid) AS returns,
    l.lanname AS language FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
    WHERE n.nspname = current_schema() ORDER BY 1`],
  triggers: ["Triggers", `SELECT t.tgname AS name, c.relname AS "table", pg_get_triggerdef(t.oid) AS definition, t.tgenabled <> 'D' AS enabled
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE NOT t.tgisinternal AND n.nspname = current_schema() ORDER BY 2, 1`],
  enums: ["Enumerated types", `SELECT t.typname AS name, string_agg(e.enumlabel, ', ' ORDER BY e.enumsortorder) AS "values"
    FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = current_schema() GROUP BY 1 ORDER BY 1`],
  views: ["Views", `SELECT table_name AS name, view_definition AS definition FROM information_schema.views
    WHERE table_schema = current_schema() ORDER BY 1`],
  indexes: ["Indexes", `SELECT tablename AS "table", indexname AS name, indexdef AS definition FROM pg_indexes
    WHERE schemaname = current_schema() ORDER BY 1, 2`],
  extensions: ["Extensions", `SELECT name, installed_version AS installed, default_version AS available, comment FROM pg_available_extensions
    ORDER BY installed_version IS NULL, name`],
};

async function dataDatabase(root, which) {
  const kind = OBJECTS[which] ? which : "functions";
  const tabs = h("div", { class: "tabs" }, Object.entries(OBJECTS).map(([k, [label]]) =>
    h("button", { type: "button", class: k === kind ? "on" : null, onclick: () => { location.hash = `data/database/${k}`; } }, label)));
  const r = await guard(() => sql(OBJECTS[kind][1], null, 1000));
  if (!r) return;
  const blurb = {
    functions: "Functions in this tenant's schema. Create them with CREATE FUNCTION in the SQL tab.",
    triggers: "Triggers on this tenant's tables.",
    enums: "Enumerated types (CREATE TYPE ... AS ENUM).",
    views: "Views in this tenant's schema.",
    indexes: "Every index on this tenant's tables. The Advisors page points out foreign keys that need one.",
    extensions: "Extensions on this server: installed ones first. Installing one needs the database owner, not a tenant role.",
  }[kind];
  const callable = kind === "functions";
  const callCol = callable ? [{ label: "", num: true, get: (row) => row[2] === "trigger" ? h("span", { class: "tiny muted" }, "runs from its trigger") : btn("Call", () => rpcDrawer({ name: row[0], arguments: row[1],
    argnames: String(row[1] || "").split(",").map((x) => x.trim().split(/\s+/)[0]).filter(Boolean) }), { ico: "play" }) }] : [];
  fill(root, h("div", { class: "stack" }, tabs, card(cardHead(OBJECTS[kind][0], `${plural(r.rows.length, "item")} · ${blurb}`),
    table([...r.columns.map((c, i) => ({ label: c, mono: c !== "comment", get: (row) => {
      const v = row[i];
      if (v === null) return h("span", { class: "muted" }, "-");
      if (typeof v === "boolean") return v ? pill("yes", "ok", false) : pill("no", "", false);
      return h("span", { class: "wrap-any small" }, String(v));
    } })), ...callCol], r.rows, { emptyView: h("div", { class: "card-pad muted small" }, `No ${OBJECTS[kind][0].toLowerCase()} yet.`) }))));
}

// ------------------------------------------------------------------ reports: saved queries pinned as live charts

view.reports = async (root, _arg, actions) => {
  fill(actions, btn("Refresh", () => route(), { ico: "refresh" }));
  fill(root, skeleton("block", 2));
  const r = await api("GET", "/v1/sql/saved");
  const pinned = r.queries.filter((q) => q.pinned);
  if (!pinned.length) {
    fill(root, card(empty("eval", "No reports yet", "Save a query in Data → SQL and pin it (☆): it shows here as a live chart, run fresh each time you open this page.",
      btn("Open the SQL editor", () => { location.hash = "data/sql"; }, { kind: "primary" }))));
    return;
  }
  const widgets = pinned.map((q) => {
    const box = h("div", { class: "card" }, h("div", { class: "card-pad" }, skeleton("block", 1)));
    (async () => {
      const res = await sql(q.query, null, 500).catch((e) => ({ error: e.message }));
      const head = cardHead(q.name, res.error ? "the query failed" : `${plural(res.row_count, "row")} · updated ${rel(q.updated_at)}`,
        btn("", () => { sessionStorage.setItem("swsdb.sqlload", q.query); location.hash = "data/sql"; }, { ico: "play", title: "Open in the SQL editor" }),
        btn("", async () => { if (await guard(() => api("PUT", `/v1/sql/saved/${encodeURIComponent(q.name)}`, { query: q.query, pinned: false }))) route(); },
          { ico: "x", title: "Unpin from reports" }));
      if (res.error) { fill(box, head, h("div", { class: "card-pad small" }, res.error)); return; }
      const chart = resultChart(res);
      fill(box, head, chart ? h("div", { class: "report-chart" }, chart) : resultTable(res));
    })();
    return box;
  });
  fill(root, h("div", { class: "grid c2" }, widgets));
};

// ------------------------------------------------------------------ command palette (Ctrl+K)

async function openPalette() {
  if (document.querySelector(".palette")) return;
  const input = h("input", { placeholder: "Go to a page, collection, table, saved query or document…", "aria-label": "Search" });
  const results = h("div", { class: "palette-results" });
  let items = [], sel = 0, typing;
  const close = () => { document.querySelectorAll(".palette, .palette-scrim").forEach((e) => e.remove()); };
  const base = Object.entries(PAGES).map(([k, p]) => ({ group: "Pages", label: p.title, sub: p.sub, go: `#${k}` }));
  const extra = [];
  const go = (it) => { close(); location.hash = it.go; };
  (async () => {
    const [cols, tbls, saved] = await Promise.all([api("GET", "/v1/collections").catch(() => ({ collections: [] })),
      sql(TABLES_SQL).catch(() => ({ rows: [] })), api("GET", "/v1/sql/saved").catch(() => ({ queries: [] }))]);
    for (const c of cols.collections) extra.push({ group: "Collections", label: c.name, sub: [c.vector && "vectors", c.graph && "graph"].filter(Boolean).join(" · "), go: `#collections/${encodeURIComponent(c.name)}` });
    for (const [n, k, , rows] of tbls.rows) extra.push({ group: "Tables", label: n, sub: `${k} · ${rows === null ? "" : `${num(rows)} rows`}`, go: `#data/tables/${encodeURIComponent(n)}` });
    for (const q of saved.queries) extra.push({ group: "Saved queries", label: q.name, sub: q.query, go: "#data/sql", query: q.query });
    draw();
  })();
  async function documents(q) {
    if (q.length < 2) return [];
    const docCols = state.collections.length ? state.collections : await documentCollections().catch(() => []);
    const found = await Promise.all(docCols.slice(0, 5).map((c) => api("GET", collPath(c.name, `/documents?limit=5&q=${encodeURIComponent(q)}`))
      .then((r) => r.documents.map((d) => ({ group: "Documents", label: d.title || d.id, sub: `${c.name} · ${d.id}`, go: `#collections/${encodeURIComponent(c.name)}` })))
      .catch(() => [])));
    return found.flat();
  }
  function draw(docs = []) {
    const q = input.value.trim().toLowerCase();
    items = [...base, ...extra].filter((it) => !q || it.label.toLowerCase().includes(q) || (it.sub || "").toLowerCase().includes(q)).slice(0, 40).concat(docs);
    sel = Math.min(sel, Math.max(0, items.length - 1));
    let last = "";
    fill(results, items.length ? items.map((it, i) => {
      const head = it.group !== last ? h("div", { class: "palette-group" }, it.group) : null;
      last = it.group;
      return [head, h("button", { type: "button", class: `palette-item${i === sel ? " on" : ""}`, onclick: () => {
        if (it.query) sessionStorage.setItem("swsdb.sqlload", it.query); go(it); } },
        h("span", {}, it.label), it.sub ? h("span", { class: "tiny muted ellipsis" }, it.sub) : null)];
    }) : h("div", { class: "card-pad small muted" }, "Nothing matches."));
  }
  input.addEventListener("input", () => {
    sel = 0; draw();
    clearTimeout(typing);
    typing = setTimeout(async () => draw(await documents(input.value.trim())), 250);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
    if (e.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); draw(); e.preventDefault(); }
    if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); draw(); e.preventDefault(); }
    if (e.key === "Enter" && items[sel]) { const it = items[sel]; if (it.query) sessionStorage.setItem("swsdb.sqlload", it.query); go(it); }
  });
  document.body.append(h("div", { class: "palette-scrim", onclick: close }),
    h("div", { class: "palette", role: "dialog", "aria-label": "Search" }, input, results));
  draw();
  input.focus();
}

// a document's original file, fetched with the key (a plain link can't send it)
async function downloadOriginal(collection, id) {
  const headers = { "swsdb-tenant": state.tenant };
  const key = sessionStorage.getItem("swsdb.key");
  if (key) headers["api-key"] = key;
  const r = await fetch(collPath(collection, `/original/${encodeURIComponent(id)}`), { headers });
  if (!r.ok) { const body = await r.json().catch(() => ({})); showError(body.message || r.statusText); return; }
  const name = (r.headers.get("content-disposition") || "").match(/filename="([^"]+)"/);
  const a = h("a", { href: URL.createObjectURL(await r.blob()), download: name ? name[1] : id });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ------------------------------------------------------------------ Auth: the tenant's end users

view.users = async (root, _arg, actions) => {
  fill(root, skeleton("block", 2));
  let q = "", typing;
  const out = h("div", { class: "stack" });
  const settingsBox = h("div", { class: "stack" });
  const find = h("input", { placeholder: "Search by email", "aria-label": "Search users",
    oninput: (e) => { clearTimeout(typing); typing = setTimeout(() => { q = e.target.value.trim(); load(); }, 250); } });
  const email = h("input", { placeholder: "email@example.com", "aria-label": "Email", type: "email" });
  const password = h("input", { placeholder: "password (8+ characters)", "aria-label": "Password", type: "password", autocomplete: "new-password" });
  const groups = h("input", { placeholder: "groups, e.g. support,hr", "aria-label": "Groups" });
  async function create() {
    const r = await guard(() => api("POST", "/v1/auth/users", { email: email.value.trim(), password: password.value,
      groups: groups.value.split(",").map((x) => x.trim()).filter(Boolean) }));
    if (r) { toast(`Created ${r.email}`); email.value = ""; password.value = ""; groups.value = ""; load(); }
  }
  async function load() {
    const r = await guard(() => api("GET", `/v1/auth/users?limit=500${q ? `&q=${encodeURIComponent(q)}` : ""}`));
    if (!r) return;
    fill(out, card(cardHead("Users", `${num(r.total)} in tenant ${state.tenant}`, find),
      table([
        { label: "Email", get: (u) => h("div", {}, h("div", {}, u.email), h("div", { class: "tiny muted mono" }, u.id)) },
        { label: "Groups", get: (u) => (u.groups.length ? h("div", { class: "row" }, u.groups.map((g) => pill(g, "", false))) : h("span", { class: "muted" }, "-")) },
        { label: "Status", get: (u) => (u.disabled ? pill("disabled", "bad") : pill("active", "ok")) },
        { label: "Created", get: (u) => h("span", { class: "muted" }, rel(u.created_at)) },
        { label: "Last sign-in", get: (u) => (u.last_sign_in_at ? rel(u.last_sign_in_at) : h("span", { class: "muted" }, "never")) },
        { label: "", num: true, get: (u) => h("div", { class: "row end" },
          btn(u.disabled ? "Enable" : "Disable", async (e) => { e.stopPropagation();
            if (await guard(() => api("PATCH", `/v1/auth/users/${u.id}`, { disabled: !u.disabled }))) load(); }),
          btn("", async (e) => { e.stopPropagation();
            if (await guard(() => api("DELETE", `/v1/auth/users/${u.id}`))) { toast(`Deleted ${u.email}`, "info"); load(); } },
            { ico: "trash", title: `Delete ${u.email}` })) },
      ], r.users, { emptyView: h("div", { class: "card-pad" }, empty("key", "No users yet", "Create one below, or open sign-up so your app's users can sign up themselves.")) })));
  }
  async function loadSettings() {
    const st = await guard(() => api("GET", "/v1/auth/settings"));
    if (!st) return;
    const signup = h("input", { type: "checkbox", checked: st.signup, "aria-label": "Open sign-up" });
    const ttl = h("select", { "aria-label": "Token lifetime" }, [[900, "15 minutes"], [3600, "1 hour"], [86400, "1 day"], [604800, "7 days"]]
      .map(([v, l]) => h("option", { value: v, selected: v === st.token_ttl_s }, l)));
    const save = async () => { if (await guard(() => api("PUT", "/v1/auth/settings", { signup: signup.checked, token_ttl_s: Number(ttl.value) }))) toast("Saved"); };
    signup.addEventListener("change", save); ttl.addEventListener("change", save);
    let armed = false;
    const rotate = btn("Sign everyone out", async () => {
      if (!armed) { armed = true; rotate.textContent = "Click again to sign everyone out"; return; }
      if (await guard(() => api("POST", "/v1/auth/settings/rotate"))) { toast("Every access token stopped working", "info"); armed = false; rotate.textContent = "Sign everyone out"; }
    }, { kind: "danger" });
    const url = location.origin;
    const code = `curl ${url}/v1/auth/token -H "content-type: application/json" \\\n  -d '{"email": "ana@example.com", "password": "...", "tenant": "${state.tenant}"}'\n\n# then, as that user:\ncurl ${url}/v1/rest/notes -H "Authorization: Bearer $ACCESS_TOKEN"`;
    fill(settingsBox, card(cardHead("Settings", "How your app's users sign in"),
      h("div", { class: "card-pad stack" },
        h("label", { class: "row small" }, signup, "Anyone may sign up (POST /v1/auth/signup, no key needed)"),
        h("div", { class: "row small" }, "Access tokens last", ttl),
        h("div", { class: "row between" }, h("span", { class: "tiny muted" }, "Rotating the signing secret ends every session at once, on every server."), rotate))),
      card(cardHead("From your app", "Sign in, then send the token: it reaches only what policies, exposure and ownership allow"),
        h("div", { class: "card-pad stack" }, h("div", { class: "row end" }, copyButton(code)), h("pre", { class: "codeblock wrap" }, code))));
  }
  fill(actions, btn("Refresh", () => { load(); loadSettings(); }, { ico: "refresh" }));
  fill(root, h("div", { class: "split" },
    h("div", { class: "stack" }, out, card(cardHead("New user", "Passwords are stored as scrypt hashes, never in clear."),
      h("div", { class: "card-pad row" }, email, password, groups, btn("Create user", create, { kind: "primary", ico: "plus" })))),
    settingsBox));
  await Promise.all([load(), loadSettings()]);
};

// ------------------------------------------------------------------ row-level security policies

const POLICY_TEMPLATES = [
  ["Users see and change their own rows", "ALL", "owner = swsdb_auth.uid()", "owner = swsdb_auth.uid()"],
  ["Users read every row", "SELECT", "true", ""],
  ["Users add rows as themselves", "INSERT", "", "owner = swsdb_auth.uid()"],
  ["A group reads the rows", "SELECT", "'support' = ANY(swsdb_auth.groups())", ""]];

view.policies = async (root, _arg, actions) => {
  fill(actions, btn("Refresh", () => route(), { ico: "refresh" }));
  fill(root, skeleton("block", 2));
  const r = await api("GET", "/v1/policies");
  const tableCard = (t) => {
    const name = h("input", { placeholder: "policy name", "aria-label": "Policy name" });
    const command = h("select", { "aria-label": "Command" }, ["ALL", "SELECT", "INSERT", "UPDATE", "DELETE"].map((c) => h("option", { value: c }, c)));
    const using = h("input", { placeholder: "using: which rows, e.g. owner = swsdb_auth.uid()", class: "mono grow", "aria-label": "Using" });
    const check = h("input", { placeholder: "with check: which rows a user may write", class: "mono grow", "aria-label": "Check" });
    const tmpl = h("select", { "aria-label": "Template", onchange: (e) => {
      const x = POLICY_TEMPLATES[Number(e.target.value)];
      if (x) { name.value = x[0].toLowerCase().slice(0, 60); command.value = x[1]; using.value = x[2]; check.value = x[3]; }
    } }, h("option", { value: "" }, "Start from a template…"), POLICY_TEMPLATES.map((x, i) => h("option", { value: i }, x[0])));
    const add = async () => {
      if (await guard(() => api("POST", `/v1/policies/tables/${encodeURIComponent(t.table)}`, { name: name.value.trim(), command: command.value,
        using: using.value.trim() || null, check: check.value.trim() || null }))) { toast(`Added ${name.value.trim()}`); route(); }
    };
    return card(
      h("div", { class: "card-head" }, h("div", {}, h("h2", { class: "mono" }, t.table),
          h("div", { class: "sub row" }, t.rls ? pill("row-level security on", "ok", false) : pill("row-level security off", "", false),
            t.exposed ? pill(`users may ${t.grants.join(", ")}`, "warn", false) : pill("not exposed to users", "", false))),
        btn(t.exposed ? "Hide from users" : "Expose to users", async () => {
          if (await guard(() => api("PUT", `/v1/policies/tables/${encodeURIComponent(t.table)}`, { exposed: !t.exposed }))) route();
        }, { kind: t.exposed ? "ghost" : "primary" })),
      t.exposed && !t.policies.length ? h("div", { class: "card-pad tiny warn-text" }, "Exposed with no policy: users see no rows at all until you add one.") : null,
      t.policies.length ? table([
        { label: "Policy", get: (p) => h("b", {}, p.name) },
        { label: "For", get: (p) => p.command },
        { label: "Using (rows)", mono: true, get: (p) => h("span", { class: "wrap-any small" }, p.using || "-") },
        { label: "Check (writes)", mono: true, get: (p) => h("span", { class: "wrap-any small" }, p.check || "-") },
        { label: "", num: true, get: (p) => btn("", async () => {
          if (await guard(() => api("DELETE", `/v1/policies/tables/${encodeURIComponent(t.table)}/${encodeURIComponent(p.name)}`))) route();
        }, { ico: "trash", title: `Drop ${p.name}` }) }], t.policies) : null,
      t.exposed ? h("div", { class: "card-pad stack tight" }, h("div", { class: "row" }, tmpl, name, command),
        h("div", { class: "policy-exprs" }, using, check, btn("Add policy", add, { ico: "plus" })))
        : h("div", { class: "card-pad tiny muted" }, "Signed-in users can't reach this table. Expose it to turn row-level security on and write its policies."));
  };
  const fnRows = r.functions;
  fill(root,
    card(h("div", { class: "card-pad small" }, "Signed-in end users reach tables only through the Data API (", h("span", { class: "mono" }, "/v1/rest"),
      "), as the role ", h("span", { class: "mono" }, r.users_role), ". A table they may reach has row-level security on, and its policies decide which rows; in policies, ",
      h("span", { class: "mono" }, "swsdb_auth.uid()"), ", ", h("span", { class: "mono" }, ".email()"), " and ", h("span", { class: "mono" }, ".groups()"),
      " are the signed-in user. API keys aren't bound by policies.")),
    r.tables.length ? [...r.tables].sort((a, b) => Number(b.exposed) - Number(a.exposed)).map(tableCard)
      : card(empty("database", "No tables yet", "Create one in Data → Tables.")),
    card(cardHead("Functions", "Which of the tenant's functions signed-in users may call (POST /v1/rpc/<name>)"),
      table([{ label: "Function", mono: true, get: (f) => `${f.name}(${f.arguments})` },
             { label: "Returns", mono: true, get: (f) => f.returns },
             { label: "", num: true, get: (f) => btn(f.exposed ? "Users may call it" : "Allow users", async () => {
               if (await guard(() => api("PUT", `/v1/policies/functions/${encodeURIComponent(f.name)}`, { exposed: !f.exposed }))) route();
             }, { kind: f.exposed ? "ghost" : "primary" }) }], fnRows,
        { emptyView: h("div", { class: "card-pad muted small" }, "No functions yet: CREATE FUNCTION in the SQL tab.") })));
};

// ------------------------------------------------------------------ storage

view.storage = async (root, arg, actions) => {
  fill(root, skeleton("block", 2));
  const st = await api("GET", "/v1/storage/buckets");
  const bucket = st.buckets.some((b) => b.name === arg) ? arg : (st.buckets[0] || {}).name;
  const side = card(h("div", { class: "card-pad stack tight" }, h("b", {}, "Buckets"),
    h("div", { class: "table-links" }, st.buckets.map((b) => h("a", { href: `#storage/${encodeURIComponent(b.name)}`, class: b.name === bucket ? "on" : null },
      icon("archive"), h("span", { class: "ellipsis" }, b.name), h("span", { class: "tiny muted" }, b.public ? "public" : num(b.objects))))),
    (() => {
      const n = h("input", { placeholder: "new-bucket", "aria-label": "Bucket name" });
      const pub = h("input", { type: "checkbox", "aria-label": "Public" });
      return h("div", { class: "stack tight" }, n, h("label", { class: "row tiny muted" }, pub, "public (every key and user of the tenant may read)"),
        btn("New bucket", async () => { if (await guard(() => api("POST", "/v1/storage/buckets", { name: n.value.trim(), public: pub.checked }))) { location.hash = `storage/${encodeURIComponent(n.value.trim())}`; route(); } }, { ico: "plus" }));
    })()));
  const main = h("div", { class: "stack" });
  fill(actions, btn("Refresh", () => route(), { ico: "refresh" }));
  fill(root, h("div", { class: "tables-layout" }, side, main));
  if (!bucket) { fill(main, card(empty("archive", "No buckets yet", "Make one on the left, then upload files: they're kept in Postgres, so snapshots include them."))); return; }
  const b = st.buckets.find((x) => x.name === bucket);
  let prefix = "";
  const fileIn = h("input", { type: "file", multiple: true, class: "hidden" });
  const prefixIn = h("input", { placeholder: "folder/ (optional)", class: "mono", "aria-label": "Folder" });
  async function upload(files) {
    for (const f of files) {
      const headers = { "swsdb-tenant": state.tenant, "content-type": f.type || "application/octet-stream" };
      const key = sessionStorage.getItem("swsdb.key");
      if (key) headers["api-key"] = key;
      const path = `${prefixIn.value.trim().replace(/^\/+/, "")}${f.name}`;
      const r = await fetch(`/v1/storage/${encodeURIComponent(bucket)}/${path.split("/").map(encodeURIComponent).join("/")}`, { method: "PUT", headers, body: f });
      if (!r.ok) { const e = await r.json().catch(() => ({})); showError(e.message || r.statusText); return; }
    }
    toast(`Uploaded ${plural(files.length, "file")}`); load();
  }
  fileIn.addEventListener("change", () => { upload([...fileIn.files]); fileIn.value = ""; });
  async function load() {
    const r = await guard(() => api("GET", `/v1/storage/${encodeURIComponent(bucket)}?prefix=${encodeURIComponent(prefix)}`));
    if (!r) return;
    const total = r.objects.reduce((a, o) => a + o.size, 0);
    fill(main, card(
      h("div", { class: "card-head" }, h("div", {}, h("h2", { class: "mono" }, bucket),
          h("div", { class: "sub" }, `${plural(r.objects.length, "file")} · ${bytesText(total)} · ${b.public ? "public: every key and user of the tenant may read" : "private: keys, and each user in their own folder"}`)),
        h("div", { class: "row" }, prefixIn, btn("Upload", () => fileIn.click(), { kind: "primary", ico: "upload" }), fileIn,
          btn(b.public ? "Make private" : "Make public", async () => { if (await guard(() => api("PATCH", `/v1/storage/buckets/${encodeURIComponent(bucket)}`, { public: !b.public }))) route(); }))),
      table([
        { label: "Path", mono: true, get: (o) => o.path },
        { label: "Type", get: (o) => h("span", { class: "small muted" }, o.content_type) },
        { label: "Size", num: true, get: (o) => bytesText(o.size) },
        { label: "Owner", mono: true, get: (o) => o.owner ? h("span", { class: "tiny" }, o.owner.slice(0, 8)) : h("span", { class: "muted" }, "key") },
        { label: "Updated", get: (o) => h("span", { class: "muted" }, rel(o.updated_at)) },
        { label: "", num: true, get: (o) => h("div", { class: "row end" },
          btn("", () => downloadFile(bucket, o.path), { ico: "download", title: "Download" }),
          btn("", async () => { if (await guard(() => api("DELETE", `/v1/storage/${encodeURIComponent(bucket)}/${o.path.split("/").map(encodeURIComponent).join("/")}`))) { toast(`Deleted ${o.path}`, "info"); load(); } },
            { ico: "trash", title: "Delete" })) }],
        r.objects, { emptyView: h("div", { class: "card-pad" }, empty("upload", "No files yet", "Upload some: HTML and other active files always download, never open as pages.")) })));
  }
  await load();
};

async function downloadFile(bucket, path) {
  const headers = { "swsdb-tenant": state.tenant };
  const key = sessionStorage.getItem("swsdb.key");
  if (key) headers["api-key"] = key;
  const r = await fetch(`/v1/storage/${encodeURIComponent(bucket)}/${path.split("/").map(encodeURIComponent).join("/")}`, { headers });
  if (!r.ok) { showError(r.statusText); return; }
  const a = h("a", { href: URL.createObjectURL(await r.blob()), download: path.split("/").pop() });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ------------------------------------------------------------------ database query statistics (Logs > Database queries)

async function dbQueries(root) {
  let order = "total";
  const box = h("div", { class: "stack" });
  async function load() {
    const r = await guard(() => api("GET", `/v1/query-stats?limit=100&order=${order}`));
    if (!r) return;
    if (!r.available) { fill(box, card(empty("info", "Query statistics aren't available", `${r.reason}. The bundled Postgres loads pg_stat_statements after a restart; on your own Postgres, add it to shared_preload_libraries.`))); return; }
    const most = Math.max(1, ...r.statements.map((s) => s.total_ms));
    const sel = h("select", { "aria-label": "Order", onchange: (e) => { order = e.target.value; load(); } },
      [["total", "Most total time"], ["mean", "Slowest on average"], ["calls", "Most calls"], ["rows", "Most rows"]].map(([v, l]) => h("option", { value: v, selected: v === order }, l)));
    fill(box, card(cardHead("Database queries", "Postgres's own statistics (pg_stat_statements): values show as $1, so no data appears here", sel,
        btn("Reset", async () => { if (await guard(() => api("POST", "/v1/query-stats/reset"))) load(); }, { title: "Start counting again (needs a key from the environment)" })),
      table([
        { label: "Statement", mono: true, get: (s) => h("span", { class: "wrap-any small", title: s.query }, s.query.length > 220 ? `${s.query.slice(0, 219)}…` : s.query) },
        { label: "Role", mono: true, get: (s) => h("span", { class: "tiny" }, s.role) },
        { label: "Calls", num: true, get: (s) => num(s.calls) },
        { label: "Mean", num: true, get: (s) => `${s.mean_ms} ms` },
        { label: "Max", num: true, get: (s) => `${s.max_ms} ms` },
        { label: "Rows", num: true, get: (s) => num(s.rows) },
        { label: "Cache hit", num: true, get: (s) => (s.cache_hit === null ? "-" : `${(s.cache_hit * 100).toFixed(1)}%`) },
        { label: "Time consumed", get: (s) => {
          const b = h("i"); b.style.width = `${Math.max(2, (s.total_ms / most) * 100)}%`;
          return h("div", { class: "bar-row nolabel", title: `${num(Math.round(s.total_ms))} ms in all` }, h("span", { class: "bar-track" }, b),
            h("span", { class: "bar-value" }, `${num(Math.round(s.total_ms))} ms`));
        } }], r.statements, { emptyView: h("div", { class: "card-pad muted small" }, "No statements recorded yet.") })));
  }
  fill(root, box);
  await load();
}

// call one of the tenant's functions from the Database tab
function rpcDrawer(fn) {
  closeDrawer();
  const args = h("textarea", { class: "mono", rows: 6, "aria-label": "Arguments" }, JSON.stringify(Object.fromEntries((fn.argnames || []).map((n) => [n, null])), null, 2));
  const out = h("div", { class: "stack" });
  const scrim = h("div", { class: "scrim", onclick: closeDrawer });
  const drawer = h("aside", { class: "drawer", role: "dialog", "aria-label": `Call ${fn.name}` },
    h("div", { class: "row between" }, h("div", {}, h("div", { class: "tiny muted" }, "Call"), h("h2", { class: "mono" }, `${fn.name}(${fn.arguments})`)), btn("", closeDrawer, { ico: "x", title: "Close" })),
    h("div", { class: "field-label" }, "Arguments (JSON, by name)"), args,
    h("div", { class: "row end" }, btn("Call", async () => {
      let body;
      const fail = (msg) => fill(out, h("pre", { class: "codeblock wrap drawer-error" }, msg));
      try { body = JSON.parse(args.value || "{}"); } catch (e) { fail(`Arguments: ${e.message}`); return; }
      try {
        const r = await api("POST", `/v1/rpc/${encodeURIComponent(fn.name)}`, body);
        fill(out, h("pre", { class: "codeblock wrap" }, JSON.stringify(r.data, null, 2)));
      } catch (e) { fail(e instanceof ApiError ? `${e.message} (${e.status} ${e.code})` : String(e)); }
    }, { kind: "primary", ico: "play" })), out,
    h("div", { class: "tiny muted" }, `From code: POST ${location.origin}/v1/rpc/${fn.name} with the arguments as JSON. Signed-in users may call it once it's allowed on the Policies page.`));
  document.body.append(scrim, drawer);
}

// ------------------------------------------------------------------ shell

function setConn(kind, text) { const c = $("#conn"); c.className = `conn ${kind}`; fill(c, h("span", { class: "d" }), h("span", {}, text)); }
function clearTimers() { state.timers.forEach((t) => clearTimeout(t)); state.timers = []; }
function currentPage() { const p = (location.hash.slice(1) || "overview").split("/")[0]; return PAGES[p] ? p : "overview"; }

async function route(quiet = false) {
  if (state.offline) return;
  clearTimers();
  const name = currentPage();
  const rest = location.hash.slice(1).split("/").slice(1);
  const arg = rest.length ? decodeURIComponent(rest.join("/")) : undefined;
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  $("#title").textContent = PAGES[name].title;
  $("#subtitle").textContent = PAGES[name].sub;
  document.title = `${PAGES[name].title} · swsdb`;
  if (quiet) {  // background refresh: render off-screen, swap in when ready so nothing flickers
    const tmp = h("div"), tmpActions = h("div");
    await guard(() => view[name](tmp, arg, tmpActions));
    if (!state.offline && currentPage() === name) { fill($("#page"), [...tmp.childNodes]); fill($("#actions"), [...tmpActions.childNodes]); }
    return;
  }
  closeDrawer(); fill($("#actions")); showError(null);
  await guard(() => view[name]($("#page"), arg, $("#actions")));
}

async function health() {
  if (state.offline) return;
  try {
    const body = await (await fetch("/readyz", { cache: "no-store" })).json();
    setConn(body.status === "ready" ? "ok" : body.status === "starting" ? "warn" : "bad",
            body.status === "ready" ? "all engines ready" : body.status);
  } catch { goOffline(); }
}

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("swsdb.theme", t);
  fill($("#theme-btn"), icon(t === "dark" ? "sun" : "moon"));
}
function drawKeyButton() {
  const has = !!sessionStorage.getItem("swsdb.key");
  fill($("#key-btn"), icon(has ? "lock" : "key"), has ? "Key set" : "API key");
}
function openKeyDialog() { const d = $("#key-dialog"); if (!d.open) d.showModal(); }

// A tenant's key reaches only its tenant: ask the server whose key this is, then lock the tenant field to it.
async function applyKey() {
  const key = sessionStorage.getItem("swsdb.key");
  const input = $("#tenant");
  let keyTenant = null;
  if (key) {
    try {
      const r = await fetch("/v1/status", { headers: { "api-key": key } });
      if (r.ok) keyTenant = (await r.json()).tenant;
    } catch { /* offline: the health check takes over */ }
  }
  input.disabled = !!keyTenant;
  input.title = keyTenant ? `This key belongs to tenant ${keyTenant}` : "";
  if (keyTenant) state.tenant = keyTenant;
  else state.tenant = localStorage.getItem("swsdb.tenant") || "default";
  input.value = state.tenant;
  drawKeyButton();
}

function init() {
  fill($("#brand-mark"), icon("database"));
  const link = (p) => h("a", { href: `#${p}`, "data-page": p }, icon(PAGES[p].icon), PAGES[p].title.replace(" explorer", ""));
  fill($("#nav"), h("div", { class: "nav-label" }, "Data"), ["overview", "collections", "search", "graph", "data", "storage", "reports"].map(link),
    h("div", { class: "nav-label" }, "Quality"), ["eval", "doctor", "advisors", "logs"].map(link),
    h("div", { class: "nav-label" }, "Admin"), ["users", "policies", "keys", "backups"].map(link));
  setTheme(localStorage.getItem("swsdb.theme") || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
  $("#theme-btn").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("#tenant").value = state.tenant;
  $("#tenant").addEventListener("change", (e) => {
    state.tenant = e.target.value.trim() || "default";
    localStorage.setItem("swsdb.tenant", state.tenant);
    toast(`Tenant: ${state.tenant}`, "info");
    route();
  });
  drawKeyButton();
  $("#key-btn").addEventListener("click", openKeyDialog);
  $("#connect-btn").addEventListener("click", openConnect);
  $("#search-btn").addEventListener("click", openPalette);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  });
  $("#key-input").addEventListener("keydown", (e) => {  // Enter saves (the form's first button would forget the key)
    if (e.key === "Enter") { e.preventDefault(); $("#key-save").click(); $("#key-dialog").close(); }
  });
  $("#key-save").addEventListener("click", () => {
    const v = $("#key-input").value.trim();
    if (v) sessionStorage.setItem("swsdb.key", v);
    $("#key-input").value = "";
    applyKey().then(() => route());
  });
  $("#key-clear").addEventListener("click", () => { sessionStorage.removeItem("swsdb.key"); applyKey().then(() => route()); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); location.hash = "search"; }
  });
  window.addEventListener("hashchange", () => route());
  fetch("/").then((r) => r.json()).then((info) => {
    fill($("#version"), `swsdb ${info.version}`, h("br"), `engines: ${(info.engines || []).join(" · ")}`);
  }).catch(() => {});
  health();
  setInterval(health, 4000);
  applyKey().then(() => route());
}

init();
