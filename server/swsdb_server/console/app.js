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

// ------------------------------------------------------------------ pages

const PAGES = {
  overview: { title: "Overview", icon: "overview", sub: "Health of the engines, how far every store is behind, and your collections." },
  collections: { title: "Collections", icon: "database", sub: "Documents, their status in every store, and why anything isn't searchable." },
  search: { title: "Search", icon: "search", sub: "Ask questions as any user and see how each passage was found." },
  graph: { title: "Graph explorer", icon: "graph", sub: "Entities extracted from your documents, how they relate and where they're mentioned." },
  eval: { title: "Evaluation", icon: "eval", sub: "Measure retrieval on your own questions and compare configurations with numbers." },
  backups: { title: "Backups", icon: "archive", sub: "Snapshots of every engine at one consistent point, scheduled backups and their copies in S3." },
  doctor: { title: "Doctor", icon: "doctor", sub: "Check that Qdrant and the graph agree with Postgres, the source of truth, and repair drift." },
};
const view = {};

view.overview = async (root, _arg, actions) => {
  fill(actions, btn("Refresh", () => route(), { ico: "refresh" }));
  fill(root, h("div", { class: "grid c4" }, skeleton("block", 4)), skeleton("block", 2));
  const [st, cols] = await Promise.all([api("GET", "/v1/status"), documentCollections()]);
  for (const e of Object.values(st.engines)) {
    const hist = (state.latency[e.engine] ||= []);
    if (e.latency_ms !== null && e.latency_ms !== undefined) { hist.push(e.latency_ms); if (hist.length > 40) hist.shift(); }
  }
  const totals = cols.reduce((a, c) => {
    for (const [k, v] of Object.entries(c.info.documents.by_status || {})) a[k] = (a[k] || 0) + v;
    a.vectors += c.info.vector ? c.info.vector.points || 0 : 0;
    a.nodes += c.info.graph ? c.info.graph.nodes || 0 : 0;
    return a;
  }, { vectors: 0, nodes: 0 });
  const docsTotal = cols.reduce((a, c) => a + c.info.documents.count, 0);
  const sync = st.sync || {};
  const stat = (ico, color, label, value, foot) => card(h("div", { class: "row between" },
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
  const consistency = card(cardHead("Consistency", "Changes accepted by Postgres, still being applied to Qdrant and the graph"),
    h("div", { class: "row" },
      ring(pending ? 0.25 : 1, pending ? (sync.retrying ? "bad" : "warn") : "", pending ? String(pending) : "✓",
           pending ? "pending" : "in sync"),
      h("dl", { class: "kv grow" },
        h("dt", {}, "Waiting"), h("dd", {}, plural(pending, "change")),
        h("dt", {}, "Retrying"), h("dd", {}, sync.retrying ? pill(String(sync.retrying), "warn") : "none"),
        h("dt", {}, "Oldest"), h("dd", {}, sync.oldest_s !== null && sync.oldest_s !== undefined ? `${sync.oldest_s}s` : "-"),
        h("dt", {}, "Reconciled"), h("dd", {}, sync.last_reconcile
          ? [rel(sync.last_reconcile.at), " · ", plural(sync.last_reconcile.issues, "issue")] : "not yet (every 10 min)"))));

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
        d.graph ? pill("graph", "violet", false) : pill("vectors only", "", false)),
      h("div", { class: "nums" },
        h("div", {}, h("b", {}, d.count), h("span", {}, "documents")),
        h("div", {}, h("b", {}, c.info.vector ? c.info.vector.points : 0), h("span", {}, "vectors")),
        h("div", {}, h("b", {}, c.info.graph ? c.info.graph.nodes : 0), h("span", {}, "graph nodes"))),
      bar,
      h("div", { class: "row between" }, h("div", { class: "legend" },
        Object.entries(by).map(([k, v]) => h("span", {}, h("i", { class: `s-${k}` }), `${v} ${k}`))),
        h("span", { class: "tiny muted" }, String(d.embedding || "").replace("local:", ""))));
  };

  fill(root,
    h("div", { class: "grid c4" },
      stat("file", "violet", "Documents", docsTotal, `${totals.ready || 0} ready · ${totals.failed || 0} failed`),
      stat("vector", "blue", "Vectors", totals.vectors.toLocaleString(), plural(cols.length, "collection")),
      stat("graph", "amber", "Graph nodes", totals.nodes.toLocaleString(), "documents, chunks, entities"),
      stat("zap", pending ? "amber" : "green", "Sync backlog", pending, pending ? "being applied" : "every store in sync")),
    h("div", { class: "section-title" }, "Engines"),
    h("div", { class: "grid c3" }, Object.entries(st.engines).map(engineCard)),
    h("div", { class: "split" },
      h("div", { class: "stack" }, h("div", { class: "section-title" }, "Collections"),
        cols.length ? h("div", { class: "grid c2" }, cols.map(collCard)) : noCollections()),
      h("div", { class: "stack" }, h("div", { class: "section-title" }, "Sync"), consistency)));
  state.timers.push(setTimeout(() => { if (currentPage() === "overview") route(true); }, 5000));
};

view.collections = async (root, arg, actions) => {
  fill(root, skeleton("block", 2));
  const cols = await documentCollections();
  if (!cols.length) { fill(root, noCollections()); return; }
  const name = cols.some((c) => c.name === arg) ? arg : cols[0].name;
  let statusFilter = "";
  let text = "";
  let docs = [];
  let selected = null;
  const listCard = h("div", { class: "card flush" });
  const filterInput = h("input", { placeholder: "Filter by id or title", class: "grow", "aria-label": "Filter",
    oninput: (e) => { text = e.target.value.toLowerCase(); draw(); } });
  const statusSel = h("select", { onchange: (e) => { statusFilter = e.target.value; load(); }, "aria-label": "Status" },
    ["", "ready", "processing", "queued", "failed", "deleting"].map((v) => h("option", { value: v }, v || "All statuses")));

  function draw() {
    const rows = docs.filter((d) => !text || d.id.toLowerCase().includes(text) || (d.title || "").toLowerCase().includes(text));
    fill(listCard,
      h("div", { class: "card-head" }, h("div", {}, h("h2", {}, name),
        h("div", { class: "sub" }, `${plural(rows.length, "document")}${text ? " shown" : ""}`)),
        h("div", { class: "row" }, filterInput, statusSel)),
      table([
        { label: "Document", get: (d) => h("div", {}, h("div", { class: "mono" }, d.id), d.title ? h("div", { class: "tiny muted" }, d.title) : null) },
        { label: "Status", get: (d) => h("div", { class: "row" }, status(d.status), d.attempts ? pill(`retry ${d.attempts}`, "warn", false) : null) },
        { label: "Version", num: true, get: (d) => (d.live_version && d.live_version !== d.version ? `${d.live_version} → ${d.version}` : d.version) },
        { label: "Chunks", num: true, get: (d) => d.chunks },
        { label: "Entities", num: true, get: (d) => d.entities },
        { label: "Updated", num: true, get: (d) => h("span", { class: "muted", title: new Date(d.updated_at).toLocaleString() }, rel(d.updated_at)) },
      ], rows, { onRow: (d) => { selected = d.id; draw(); openDiagnosis(name, d.id, load); }, selected: (d) => d.id === selected,
                 emptyView: h("div", { class: "card-pad" }, empty("file", "No documents match", "Add one with the panel on the right.")) }));
  }
  async function load() {
    const q = statusFilter ? `&status=${encodeURIComponent(statusFilter)}` : "";
    const r = await guard(() => api("GET", collPath(name, `/documents?limit=500${q}`)));
    if (r) { docs = r.documents; draw(); }
  }

  fill(actions, collectionSelect(name, (v) => { location.hash = `collections/${v}`; }), btn("Refresh", load, { ico: "refresh" }));
  fill(root, h("div", { class: "split" }, listCard, uploadCard(name, load)));
  await load();
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
        cited ? pill("cited", "ok", false) : null,
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

  function params() {
    const body = { k: Number(k.value), mode: mode.value };
    if (rerank.value !== "") body.rerank = rerank.value === "true";
    if (hops.value !== "") body.graph_hops = Number(hops.value);
    const p = principals.value.split(",").map((x) => x.trim()).filter(Boolean);
    if (p.length) body.principals = p;
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
  fill(actions, collectionSelect(coll, (v) => { coll = v; fill(results); }));
  fill(root,
    h("div", { class: "search-hero" }, icon("search", "lead"), q, btn("Search", search, { title: "Passages only (Shift+Enter)" }),
      btn("Ask", ask, { kind: "primary", ico: "spark", title: "A cited answer (Enter)" })),
    h("div", { class: "row" }, mode, k, hops, rerank, principals),
    h("div", { class: "chips" }, h("span", { class: "tiny muted" }, "Try"),
      ["What is the refund policy?", "Where do orders ship from?", "Who leads the project?"].map(example)),
    results);
  results.append(empty("spark", "Ask your documents", "Ask gives a short answer that cites its passages; Search shows the passages alone. Hybrid search combines meaning and keywords; the graph adds connected passages."));
  q.focus();
};

const TYPE_COLORS = { Person: "#60a5fa", Organization: "#a78bfa", Product: "#34d399", Place: "#fbbf24", Project: "#f472b6",
                      Policy: "#fb923c", Technology: "#22d3ee", Event: "#f87171", Other: "#94a3b8" };
const typeDot = (t) => { const d = h("span", { class: "type-dot" }); d.style.background = TYPE_COLORS[t] || TYPE_COLORS.Other; return d; };

view.graph = async (root, _arg, actions) => {
  fill(root, skeleton("block", 2));
  const cols = (await documentCollections()).filter((c) => c.info.documents.graph);
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
    fill(listCard, h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Entities"),
      h("div", { class: "sub" }, `${r.rows.length} shown, most mentioned first`)),
      h("div", { class: "row" }, find, btn("", () => top(find.value.trim()), { ico: "search", title: "Find" }))),
      table([
        { label: "Entity", get: (row) => h("span", { class: "row" }, typeDot(row[2]), row[1]) },
        { label: "Type", get: (row) => h("span", { class: "muted small" }, row[2]) },
        { label: "Mentions", num: true, get: (row) => row[3] },
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
  missing_vectors: ["vector", "Missing or hidden vectors", "Documents whose live version isn't fully in Qdrant. Repair re-embeds them from the catalog."],
  stale_vectors: ["vector", "Old versions left behind", "Vectors of versions that are no longer live. Repair deletes them."],
  orphan_vectors: ["vector", "Vectors of unknown documents", "Points whose document doesn't exist in the catalog. Repair deletes them."],
  graph_mismatch: ["graph", "Graph out of step", "Documents whose graph doesn't match the catalog. Repair rebuilds it from the catalog (no LLM calls)."],
  orphan_graph: ["graph", "Graph nodes of unknown documents", "Repair removes them."],
  stuck: ["clock", "Stuck changes", "Accepted changes with nothing applying them. Repair queues them again."],
  missing_vector_collection: ["database", "Missing Qdrant collection", "Repair recreates it and re-indexes every document."],
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
      h("div", { class: "small muted" }, "The doctor compares it with what Qdrant and the graph actually hold. Checking never changes anything; it also runs automatically every 10 minutes.")))), out);
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
  fill($("#nav"), h("div", { class: "nav-label" }, "Data"), ["overview", "collections", "search", "graph"].map(link),
    h("div", { class: "nav-label" }, "Quality"), ["eval", "doctor"].map(link),
    h("div", { class: "nav-label" }, "Admin"), ["backups"].map(link));
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
