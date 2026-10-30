// swsdb console. Talks only to this server's /v1 API. Every value from the API is rendered as text, never as HTML.

const $ = (sel) => document.querySelector(sel);
const state = { tenant: localStorage.getItem("swsdb.tenant") || "default", collections: [] };

// ------------------------------------------------------------------ DOM helpers (text only, no innerHTML)

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
// replace an element's children; arrays are flattened and empty values skipped (like h())
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
}
const badge = (text, kind = "") => h("span", { class: `badge ${kind}` }, text);
const statusKind = { ready: "ok", processing: "info", queued: "info", failed: "bad", deleting: "warn",
                     starting: "info", down: "bad", degraded: "warn" };
const fmt = (n, d = 3) => (n === null || n === undefined ? "-" : Number(n).toFixed(d));
const when = (iso) => (iso ? new Date(iso).toLocaleString() : "-");

function table(columns, rows, { onRow, selected, empty = "Nothing here yet." } = {}) {
  if (!rows.length) return h("div", { class: "empty" }, empty);
  return h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
    h("tbody", {}, rows.map((r) => h("tr", {
      class: [onRow ? "click" : "", selected && selected(r) ? "selected" : ""].join(" "),
      onclick: onRow ? () => onRow(r) : null,
    }, columns.map((c) => h("td", { class: c.num ? "num" : c.mono ? "mono" : "" }, c.get(r)))))));
}

// ------------------------------------------------------------------ API

class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function api(method, path, body, { raw = false } = {}) {
  const headers = { "swsdb-tenant": state.tenant };
  const key = sessionStorage.getItem("swsdb.key");
  if (key) headers["api-key"] = key;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["content-type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch(path, { method, headers, body: payload });
  if (raw) return r;
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 401) $("#key-dialog").showModal();
    throw new ApiError(r.status, data.error || "error", data.message || r.statusText);
  }
  return data;
}

function showError(e) {
  const b = $("#error");
  if (!e) { b.classList.add("hidden"); return; }
  b.textContent = e instanceof ApiError ? `${e.status} ${e.code}: ${e.message}` : String(e);
  b.classList.remove("hidden");
}

async function guard(fn) {
  try { showError(null); return await fn(); } catch (e) { showError(e); return undefined; }
}

async function documentCollections() {
  const { collections } = await api("GET", "/v1/collections");
  const out = [];
  for (const c of collections) {
    const info = await api("GET", `/v1/collections/${encodeURIComponent(c.name)}`).catch(() => null);
    if (info && info.documents) out.push({ ...c, info });
  }
  state.collections = out;
  return out;
}

function collectionPicker(onChange, current) {
  const sel = h("select", { onchange: (e) => onChange(e.target.value) },
    state.collections.map((c) => h("option", { value: c.name, selected: c.name === current }, c.name)));
  return sel;
}

// ------------------------------------------------------------------ pages

const pages = {};

pages.overview = async (root) => {
  const status = await api("GET", "/v1/status");
  const engines = h("div", { class: "grid" }, Object.entries(status.engines).map(([cap, e]) =>
    h("div", { class: "card" },
      h("div", { class: "row" }, h("strong", {}, cap), badge(e.state, statusKind[e.state])),
      h("div", { class: "muted small" }, `${e.engine} ${e.version || ""}`),
      h("div", { class: "small" }, e.latency_ms !== null ? `probe ${e.latency_ms} ms` : e.error || ""),
      e.restarts_seen ? h("div", { class: "small muted" }, `${e.restarts_seen} restarts seen`) : null)));
  root.append(h("h3", {}, "Engines"), engines);
  if (status.sync) {
    const s = status.sync;
    const last = s.last_reconcile;
    root.append(h("h3", {}, "Consistency"), h("div", { class: "grid" },
      h("div", { class: "card" }, h("div", { class: "muted small" }, "Changes waiting to reach every store"),
        h("div", { class: "big" }, s.pending ?? "-"),
        h("div", { class: "small muted" }, s.retrying ? `${s.retrying} retrying` : "none retrying")),
      h("div", { class: "card" }, h("div", { class: "muted small" }, "Oldest pending change"),
        h("div", { class: "big" }, s.oldest_s !== null && s.oldest_s !== undefined ? `${s.oldest_s}s` : "-")),
      h("div", { class: "card" }, h("div", { class: "muted small" }, "Last reconciliation"),
        h("div", { class: "big" }, last ? `${last.issues} issues` : "not run"),
        h("div", { class: "small muted" }, last ? when(last.at) : "runs every 10 minutes"))));
  }
  const cols = await documentCollections();
  root.append(h("h3", {}, "Document collections"), table([
    { label: "Collection", get: (c) => c.name },
    { label: "Documents", num: true, get: (c) => c.info.documents.count },
    { label: "Vectors", num: true, get: (c) => (c.info.vector ? c.info.vector.points : "-") },
    { label: "Graph nodes", num: true, get: (c) => (c.info.graph ? c.info.graph.nodes : "-") },
    { label: "Embedding", get: (c) => c.info.documents.embedding },
  ], cols, { onRow: (c) => { location.hash = `collections/${c.name}`; },
             empty: "No document collections yet. Create one with the API or SDK: {\"documents\": true}" }));
};

pages.collections = async (root, arg) => {
  const cols = await documentCollections();
  if (!cols.length) { root.append(h("div", { class: "empty" }, "No document collections in this tenant.")); return; }
  const name = arg || cols[0].name;
  let statusFilter = "";
  let selectedDoc = null;
  const list = h("div", { class: "stack" });
  const side = h("div", { class: "stack" });

  async function loadDocs() {
    const q = statusFilter ? `&status=${encodeURIComponent(statusFilter)}` : "";
    const res = await api("GET", `/v1/collections/${encodeURIComponent(name)}/documents?limit=200${q}`);
    fill(list, 
      h("div", { class: "muted small" }, `${res.total} documents`),
      table([
        { label: "Document", mono: true, get: (d) => d.id },
        { label: "Status", get: (d) => badge(d.status, statusKind[d.status]) },
        { label: "Version", num: true, get: (d) => (d.live_version && d.live_version !== d.version
          ? `${d.version} (serving ${d.live_version})` : d.version) },
        { label: "Chunks", num: true, get: (d) => d.chunks },
        { label: "Entities", num: true, get: (d) => d.entities },
        { label: "Updated", get: (d) => when(d.updated_at) },
      ], res.documents, { onRow: (d) => { selectedDoc = d.id; diagnose(d.id); loadDocs(); },
                          selected: (d) => d.id === selectedDoc, empty: "No documents." }));
  }

  async function diagnose(id) {
    fill(side, h("div", { class: "muted" }, "Checking every store..."));
    const r = await guard(() => api("GET", `/v1/collections/${encodeURIComponent(name)}/diagnose/${encodeURIComponent(id)}`));
    if (!r) { fill(side); return; }
    const kind = r.verdict === "searchable" ? "ok" : r.verdict.startsWith("not") ? "bad" : "warn";
    const d = r.document;
    fill(side, h("div", { class: "card stack" },
      h("div", {}, h("div", { class: "muted small" }, d.id), h("div", { class: `verdict ${kind}` }, r.verdict)),
      h("ul", { class: "reasons" }, r.reasons.map((x) => h("li", {}, x))),
      h("dl", { class: "kv small" },
        h("dt", {}, "Status"), h("dd", {}, badge(d.status, statusKind[d.status])),
        h("dt", {}, "Version"), h("dd", {}, `${d.version} (live ${d.live_version ?? "none"})`),
        h("dt", {}, "Catalog chunks"), h("dd", {}, d.chunks),
        h("dt", {}, "Qdrant"), h("dd", {}, r.vectors.error || `${r.vectors.live} live, ${r.vectors.all_versions} total`),
        r.graph ? [h("dt", {}, "Graph"), h("dd", {}, r.graph.error ||
          `${r.graph.chunks} chunks, ${r.graph.entities} entities`)] : null,
        h("dt", {}, "Readers"), h("dd", {}, d.readers.join(", ")),
        d.error ? [h("dt", {}, "Error"), h("dd", { class: "mono" }, d.error)] : null),
      r.pending.length ? [h("h3", {}, "Pending changes"), table([
        { label: "Op", get: (p) => p.op }, { label: "Version", num: true, get: (p) => p.version },
        { label: "Attempts", num: true, get: (p) => p.attempts },
        { label: "Last error", mono: true, get: (p) => p.last_error || (p.running ? "running now" : "waiting") },
      ], r.pending)] : null,
      h("div", { class: "row end" }, h("button", { class: "danger", onclick: async () => {
        if (!confirm(`Delete ${d.id} from every store?`)) return;
        if (await guard(() => api("DELETE", `/v1/collections/${encodeURIComponent(name)}/documents/${encodeURIComponent(d.id)}`))) {
          selectedDoc = null; fill(side); loadDocs();
        }
      } }, "Delete document"))));
  }

  const upload = h("div", { class: "card stack" }, h("h2", {}, "Add a document"),
    h("input", { type: "file", id: "up-file" }),
    h("div", { class: "muted small" }, "or paste text (Markdown):"),
    h("input", { id: "up-id", placeholder: "document id (defaults to the file name)" }),
    h("textarea", { id: "up-text", placeholder: "# Title\n\nText..." }),
    h("div", { class: "row end" }, h("button", { onclick: async () => {
      const file = $("#up-file").files[0];
      const id = $("#up-id").value.trim();
      const text = $("#up-text").value;
      const base = `/v1/collections/${encodeURIComponent(name)}/documents`;
      let ok;
      if (file) {
        const fd = new FormData(); fd.append("file", file); if (id) fd.append("id", id);
        ok = await guard(() => api("POST", base, fd));
      } else if (text.trim() && id) {
        ok = await guard(() => api("POST", `${base}/text`, { id, text }));
      } else { showError("Choose a file, or give an id and some text."); return; }
      if (ok) { $("#up-text").value = ""; $("#up-id").value = ""; $("#up-file").value = ""; loadDocs(); }
    } }, "Upload")));

  const filter = h("select", { onchange: (e) => { statusFilter = e.target.value; loadDocs(); } },
    ["", "ready", "queued", "processing", "failed", "deleting"].map((s) => h("option", { value: s }, s || "all")));
  root.append(h("div", { class: "row" }, collectionPicker((v) => { location.hash = `collections/${v}`; }, name),
                filter, h("button", { class: "ghost", onclick: loadDocs }, "Refresh")),
              h("div", { class: "split" }, list, h("div", { class: "stack" }, side, upload)));
  await loadDocs();
};

pages.search = async (root) => {
  const cols = await documentCollections();
  if (!cols.length) { root.append(h("div", { class: "empty" }, "No document collections in this tenant.")); return; }
  const results = h("div", { class: "stack" });
  const q = h("input", { placeholder: "Ask a question...", class: "grow", id: "q" });
  const coll = collectionPicker(() => {}, cols[0].name);
  const mode = h("select", {}, ["hybrid", "dense", "sparse"].map((m) => h("option", { value: m }, m)));
  const k = h("input", { type: "number", value: 5, min: 1, max: 50 });
  const hops = h("select", {}, [["", "graph: auto"], ["0", "no graph"], ["1", "1 hop"], ["2", "2 hops"]]
    .map(([v, l]) => h("option", { value: v }, l)));
  const principals = h("input", { placeholder: "as principals, e.g. user:42, group:hr (optional)" });
  async function run() {
    const body = { query: q.value, k: Number(k.value), mode: mode.value };
    if (hops.value !== "") body.graph_hops = Number(hops.value);
    const p = principals.value.split(",").map((s) => s.trim()).filter(Boolean);
    if (p.length) body.principals = p;
    if (!body.query.trim()) return;
    const r = await guard(() => api("POST", `/v1/collections/${encodeURIComponent(coll.value)}/query`, body));
    if (!r) return;
    fill(results, 
      h("div", { class: "muted small" }, `${r.hits.length} hits in ${r.took_ms} ms`,
        r.graph.used ? ` · graph added ${r.graph.added}` : ""),
      r.hits.length ? r.hits.map((x) => h("div", { class: `hit ${x.via === "graph" ? "graph" : ""}` },
        h("div", { class: "meta" }, badge(x.via, x.via === "graph" ? "info" : ""), h("strong", {}, x.document),
          x.page !== null && x.page !== undefined ? `page ${x.page}` : null, x.heading || null,
          h("span", { class: "mono" }, fmt(x.score, 4)), x.graph_path ? badge(x.graph_path.join(" → "), "info") : null),
        h("div", { class: "text" }, x.text))) : h("div", { class: "empty" }, "No hits."));
  }
  q.addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
  root.append(h("div", { class: "card stack" }, h("div", { class: "row" }, coll, q, h("button", { onclick: run }, "Search")),
    h("div", { class: "row" }, mode, h("label", { class: "small muted" }, "k ", k), hops, principals)), results);
  q.focus();
};

pages.graph = async (root) => {
  const cols = (await documentCollections()).filter((c) => c.info.documents.graph);
  if (!cols.length) { root.append(h("div", { class: "empty" }, "No collections with a graph.")); return; }
  let coll = cols[0].name;
  const out = h("div", { class: "stack" });
  const view = h("div", {});
  const cypher = (query, params) => api("POST", `/v1/collections/${encodeURIComponent(coll)}/cypher`,
    { query, params, read_only: true });

  async function top(filter) {
    const where = filter ? "WHERE toLower(e.name) CONTAINS toLower($q) " : "";
    const r = await guard(() => cypher(`MATCH (e:Entity) ${where}OPTIONAL MATCH (e)<-[:MENTIONS]-(c:Chunk) ` +
      "RETURN e.key, e.name, e.type, count(c) AS mentions ORDER BY mentions DESC LIMIT 50", filter ? { q: filter } : null));
    if (!r) return;
    fill(out, table([
      { label: "Entity", get: (row) => row[1] }, { label: "Type", get: (row) => badge(row[2]) },
      { label: "Mentioned in", num: true, get: (row) => `${row[3]} chunks` },
    ], r.rows, { onRow: (row) => focus(row[0], row[1]),
                 empty: "No entities. Set SWSDB_LLM_URL and SWSDB_LLM_MODEL to extract them while ingesting." }));
  }

  async function focus(key, name) {
    const rel = await guard(() => cypher("MATCH (e:Entity {key: $k})-[r:RELATED]-(o:Entity) RETURN o.key, o.name, r.type LIMIT 24", { k: key }));
    const co = await guard(() => cypher("MATCH (e:Entity {key: $k})<-[:MENTIONS]-(:Chunk)-[:MENTIONS]->(o:Entity) " +
      "WHERE o.key <> $k RETURN o.key, o.name, count(*) AS n ORDER BY n DESC LIMIT 24", { k: key }));
    const docs = await guard(() => cypher("MATCH (e:Entity {key: $k})<-[:MENTIONS]-(c:Chunk)<-[:HAS_CHUNK]-(d:Document) " +
      "RETURN d.external_id, count(c) AS n ORDER BY n DESC LIMIT 20", { k: key }));
    if (!rel || !co || !docs) return;
    const nodes = [...rel.rows.map((r) => ({ key: r[0], name: r[1], label: r[2], kind: "rel" })),
                   ...co.rows.filter((r) => !rel.rows.some((x) => x[0] === r[0]))
                     .map((r) => ({ key: r[0], name: r[1], label: `${r[2]}×`, kind: "entity" }))].slice(0, 24);
    fill(view, h("h3", {}, name), radial(name, nodes, (n) => focus(n.key, n.name)),
      h("h3", {}, "Mentioned in"), table([{ label: "Document", mono: true, get: (r) => r[0] },
        { label: "Chunks", num: true, get: (r) => r[1] }], docs.rows));
  }

  const filter = h("input", { placeholder: "find an entity" });
  filter.addEventListener("keydown", (e) => { if (e.key === "Enter") top(filter.value.trim()); });
  root.append(h("div", { class: "row" }, collectionPicker((v) => { coll = v; fill(view); top(); }, coll), filter,
    h("button", { class: "ghost", onclick: () => top(filter.value.trim()) }, "Find")),
    h("div", { class: "split" }, out, view));
  await top();
};

function radial(center, nodes, onClick) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "graphview");
  svg.setAttribute("viewBox", "0 0 600 380");
  const el = (tag, attrs, text) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  };
  const cx = 300, cy = 190, rad = 140;
  nodes.forEach((n, i) => {
    const a = (2 * Math.PI * i) / Math.max(nodes.length, 1) - Math.PI / 2;
    n.x = cx + rad * Math.cos(a); n.y = cy + rad * 0.85 * Math.sin(a);
    svg.append(el("line", { x1: cx, y1: cy, x2: n.x, y2: n.y }));
  });
  svg.append(el("circle", { cx, cy, r: 13, class: "center" }), el("text", { x: cx + 16, y: cy + 4 }, center));
  for (const n of nodes) {
    const c = el("circle", { cx: n.x, cy: n.y, r: 8, class: n.kind });
    c.addEventListener("click", () => onClick(n));
    const t = el("title", {}, `${n.name} (${n.label})`);
    c.append(t);
    svg.append(c, el("text", { x: n.x + 11, y: n.y + 4 }, n.name.length > 24 ? `${n.name.slice(0, 23)}…` : n.name));
  }
  return svg;
}

pages.eval = async (root) => {
  const cols = await documentCollections();
  if (!cols.length) { root.append(h("div", { class: "empty" }, "No document collections in this tenant.")); return; }
  let coll = cols[0].name;
  const sets = h("div", {});
  const report = h("div", { class: "stack" });
  const history = h("div", {});
  const base = () => `/v1/collections/${encodeURIComponent(coll)}/eval`;

  async function load() {
    const s = await guard(() => api("GET", `${base()}/sets`));
    if (s) fill(sets, table([
      { label: "Golden set", get: (x) => x.name }, { label: "Questions", num: true, get: (x) => x.cases },
      { label: "Updated", get: (x) => when(x.updated_at) },
      { label: "", get: (x) => h("button", { onclick: (e) => { e.stopPropagation(); run(x.name); } }, "Run") },
    ], s.sets, { empty: "No golden sets yet: add one on the right." }));
    const r = await guard(() => api("GET", `${base()}/runs`));
    if (r) fill(history, table([
      { label: "Run", get: (x) => `#${x.id}` }, { label: "Set", get: (x) => x.set },
      { label: "Best", get: (x) => badge(x.best, "ok") },
      { label: "Recall (best)", num: true, get: (x) => fmt(Math.max(...x.results.map((y) => y.metrics.recall))) },
      { label: "When", get: (x) => when(x.created_at) },
    ], r.runs, { onRow: show, empty: "No runs yet." }));
  }

  function show(run) {
    const cols2 = ["hit", "recall", "mrr", "ndcg", "answer_found", "evidence", "latency_p50_ms", "latency_p95_ms"]
      .filter((k) => run.results.some((r) => k in r.metrics));
    const best = run.best;
    fill(report, h("h3", {}, `Run #${run.id ?? ""} · ${run.set} · ${run.cases} questions · k=${run.k}`),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Configuration"),
        cols2.map((c) => h("th", { class: "num" }, c.replace("latency_", "").replace("_ms", " ms").replace("_", " "))))),
        h("tbody", {}, run.results.map((r) => h("tr", { class: r.config.name === best ? "best" : "" },
          h("td", {}, r.config.name, r.config.name === best ? " ★" : ""),
          cols2.map((c) => h("td", { class: "num" }, c.startsWith("latency") || c === "evidence"
            ? r.metrics[c] : fmt(r.metrics[c], 3))))))),
      h("div", { class: "muted small" }, "Recall, hit and MRR count the documents in the returned evidence; " +
        "evidence is the average number of passages returned (graph adds connected passages)."),
      run.results.some((r) => r.misses.length) ? [h("h3", {}, "Missed questions (best configuration)"),
        table([{ label: "Question", get: (m) => m.query }, { label: "Expected", mono: true, get: (m) => m.expected.join(", ") },
               { label: "Got", mono: true, get: (m) => m.got.join(", ") }],
              run.results.find((r) => r.config.name === best).misses, { empty: "None." })] : null);
  }

  async function run(name) {
    fill(report, h("div", { class: "muted" }, `Running ${name}...`));
    const r = await guard(() => api("POST", `${base()}/runs`, { set: name }));
    if (r) { show(r); load(); } else fill(report);
  }

  const setName = h("input", { placeholder: "set name, e.g. support-faq" });
  const cases = h("textarea", { placeholder: '[\n  {"query": "How long do refunds take?", "relevant": ["refund-policy"], "answer": "five business days"}\n]' });
  root.append(h("div", { class: "row" }, collectionPicker((v) => { coll = v; fill(report); load(); }, coll)),
    h("div", { class: "split" }, h("div", { class: "stack" }, sets, report, h("h3", {}, "History"), history),
      h("div", { class: "card stack" }, h("h2", {}, "Golden set"),
        h("div", { class: "muted small" }, "Questions and the ids of the documents that should answer them. " +
          "\"answer\" (optional) is a phrase the returned text should contain."),
        setName, cases, h("div", { class: "row end" }, h("button", { onclick: async () => {
          let parsed;
          try { parsed = JSON.parse(cases.value); } catch (e) { showError(`Not valid JSON: ${e.message}`); return; }
          if (await guard(() => api("PUT", `${base()}/sets/${encodeURIComponent(setName.value.trim())}`, { cases: parsed }))) load();
        } }, "Save set")))));
  await load();
};

pages.doctor = async (root) => {
  const out = h("div", { class: "stack" });
  const all = h("input", { type: "checkbox" });
  async function check(repair) {
    fill(out, h("div", { class: "muted" }, repair ? "Repairing..." : "Comparing the catalog with every store..."));
    const r = await guard(() => api("POST", "/v1/admin/reconcile", { dry_run: !repair, all_tenants: all.checked }));
    if (!r) { fill(out); return; }
    fill(out, 
      h("div", { class: `verdict ${r.issues ? "warn" : "ok"}` }, r.issues ? `${r.issues} issues ${repair ? "- repairs queued" : "found"}`
        : "All stores agree with the catalog"),
      h("div", { class: "muted small" }, `checked in ${r.took_s}s`),
      table([
        { label: "Collection", get: (c) => `${c.tenant}/${c.collection}` },
        { label: "Documents", num: true, get: (c) => c.documents },
        { label: "Findings", get: (c) => { const f = Object.entries(c.found).filter(([, v]) => v);
          return f.length ? f.map(([k, v]) => badge(`${k.replaceAll("_", " ")}: ${v}`, "warn")) : badge("consistent", "ok"); } },
      ], r.collections, { empty: "No document collections." }),
      r.issues && !repair ? h("div", { class: "row end" }, h("button", { onclick: () => check(true) }, "Repair")) : null);
  }
  root.append(h("div", { class: "card stack" },
    h("div", {}, "Compares what Postgres (the source of truth) says with what Qdrant and the graph actually hold: " +
      "missing or stale vectors, orphans, graph mismatches and stuck changes. Checking changes nothing."),
    h("div", { class: "row" }, h("button", { onclick: () => check(false) }, "Check now"),
      h("label", { class: "small" }, all, " all tenants"))), out);
};

// ------------------------------------------------------------------ shell

const titles = { overview: "Overview", collections: "Collections", search: "Search", graph: "Graph explorer",
                 eval: "Evaluation", doctor: "Doctor" };

async function route() {
  const [page, ...rest] = (location.hash.slice(1) || "overview").split("/");
  const name = pages[page] ? page : "overview";
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  $("#title").textContent = titles[name];
  const root = $("#page");
  fill(root);
  root.className = "stack";
  showError(null);
  await guard(() => pages[name](root, rest.length ? decodeURIComponent(rest.join("/")) : undefined));
}

async function health() {
  const box = $("#health");
  try {
    const r = await fetch("/readyz");
    const body = await r.json();
    fill(box, h("span", { class: `dot ${body.status === "ready" ? "ok" : body.status === "starting" ? "warn" : "bad"}` }),
      h("span", {}, body.status));
  } catch {
    fill(box, h("span", { class: "dot bad" }), h("span", {}, "unreachable"));
  }
}

async function init() {
  $("#tenant").value = state.tenant;
  $("#tenant").addEventListener("change", (e) => {
    state.tenant = e.target.value.trim() || "default";
    localStorage.setItem("swsdb.tenant", state.tenant);
    route();
  });
  $("#key-btn").addEventListener("click", () => $("#key-dialog").showModal());
  $("#key-save").addEventListener("click", () => {
    const v = $("#key-input").value.trim();
    if (v) sessionStorage.setItem("swsdb.key", v); else sessionStorage.removeItem("swsdb.key");
    $("#key-input").value = "";
    route();
  });
  window.addEventListener("hashchange", route);
  const root = await fetch("/").then((r) => r.json()).catch(() => null);
  if (root) $("#version").textContent = `swsdb ${root.version} · ${root.engines.join(", ")}`;
  health();
  setInterval(health, 5000);
  route();
}

init();
