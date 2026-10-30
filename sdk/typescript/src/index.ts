/**
 * TypeScript / JavaScript client for swsdb. No dependencies: it uses the global `fetch` (Node 18+, browsers, Deno, Bun).
 *
 *   import { SwsDB } from "swsdb";
 *   const db = new SwsDB({ url: "http://localhost:6464", apiKey: process.env.SWSDB_API_KEY });
 *   const kb = await db.createCollection("kb", { documents: true });
 *   await kb.addText("refunds", "Refunds take five business days.");
 *   await kb.waitReady("refunds");
 *   const { answer, passages } = await kb.answer("How long do refunds take?");
 */

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Where = Record<string, string | number | boolean | (string | number | boolean)[]>;
export type Mode = "hybrid" | "dense" | "sparse";

export interface Hit {
  chunk_id: string;
  score: number;
  via: "hybrid" | "dense" | "sparse" | "graph";
  text: string;
  document: string;
  title: string | null;
  page: number | null;
  heading: string | null;
  chunk: number;
  metadata: Record<string, Json>;
  graph_path?: string[];
  fusion_score?: number;
}

export interface Passage extends Omit<Hit, "metadata"> {
  n: number;
  cited: boolean;
}

export interface Answer {
  answer: string;
  cited: number[];
  passages: Passage[];
  mode: "generated" | "extractive";
  model: string | null;
  note?: string;
  retrieval_ms: number;
  took_ms: number;
}

export interface DocumentInfo {
  id: string;
  title: string | null;
  status: "queued" | "processing" | "ready" | "failed" | "deleting";
  version: number;
  live_version: number | null;
  chunks: number;
  entities: number;
  readers: string[];
  metadata: Record<string, Json>;
  error: string | null;
  updated_at: string;
  unchanged?: boolean;
  [key: string]: unknown;
}

export interface QueryOptions {
  k?: number;
  mode?: Mode;
  where?: Where;
  principals?: string[];
  graphHops?: number;
  graphK?: number;
  rerank?: boolean;
}

export interface Options {
  /** Default: SWSDB_URL, else http://localhost:6464 */
  url?: string;
  /** Default: SWSDB_API_KEY */
  apiKey?: string;
  /** Default: SWSDB_TENANT, else the key's own tenant, else "default" */
  tenant?: string;
  timeoutMs?: number;
  fetch?: typeof fetch;
}

/** Every failed call: `status` is the HTTP status (0 when the server can't be reached). */
export class SwsDBError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(`${status} ${code}: ${message}`);
    this.name = "SwsDBError";
  }
}

const env = (name: string): string | undefined =>
  typeof process !== "undefined" && process.env ? process.env[name] : undefined;
const seg = (value: string): string => encodeURIComponent(value);
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function queryBody(q: string, o: QueryOptions, key = "query"): Record<string, unknown> {
  const body: Record<string, unknown> = { [key]: q, mode: o.mode ?? "hybrid" };
  if (o.k !== undefined) body.k = o.k;
  if (o.where) body.where = o.where;
  if (o.principals) body.principals = o.principals;
  if (o.graphHops !== undefined) body.graph_hops = o.graphHops;
  if (o.graphK !== undefined) body.graph_k = o.graphK;
  if (o.rerank !== undefined) body.rerank = o.rerank;
  return body;
}

export class SwsDB {
  readonly url: string;
  readonly tenant?: string;
  private readonly headers: Record<string, string>;
  private readonly timeoutMs: number;
  private readonly fetcher: typeof fetch;

  constructor(options: Options = {}) {
    this.url = (options.url ?? env("SWSDB_URL") ?? "http://localhost:6464").replace(/\/+$/, "");
    this.tenant = options.tenant ?? env("SWSDB_TENANT");
    const key = options.apiKey ?? env("SWSDB_API_KEY");
    this.headers = { "user-agent": "swsdb-typescript" };
    if (this.tenant) this.headers["swsdb-tenant"] = this.tenant;
    if (key) this.headers["api-key"] = key;
    this.timeoutMs = options.timeoutMs ?? 60_000;
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
  }

  /** @internal one API call; JSON in, JSON out, errors as SwsDBError */
  async call<T = any>(method: string, path: string, body?: unknown, timeoutMs?: number): Promise<T> {
    const headers: Record<string, string> = { ...this.headers };
    let payload: BodyInit | undefined;
    if (body instanceof FormData) payload = body;
    else if (body !== undefined) {
      headers["content-type"] = "application/json";
      payload = JSON.stringify(body);
    }
    let r: Response;
    try {
      r = await this.fetcher(this.url + path, {
        method, headers, body: payload, signal: AbortSignal.timeout(timeoutMs ?? this.timeoutMs),
      });
    } catch (e) {
      throw new SwsDBError(0, "unreachable", `can't reach swsdb at ${this.url}: ${(e as Error).message}`);
    }
    const text = await r.text();
    let data: any = undefined;
    try { data = text ? JSON.parse(text) : undefined; } catch { data = undefined; }
    if (!r.ok) throw new SwsDBError(r.status, data?.error ?? "error", data?.message ?? (text.slice(0, 500) || r.statusText));
    return data as T;
  }

  // -- server

  async ready(): Promise<boolean> {
    try {
      return (await this.fetcher(`${this.url}/readyz`, { signal: AbortSignal.timeout(5000) })).ok;
    } catch {
      return false;
    }
  }

  status(): Promise<Record<string, any>> { return this.call("GET", "/v1/status"); }

  // -- collections

  async collections(): Promise<{ name: string; vector: boolean; graph: boolean }[]> {
    return (await this.call("GET", "/v1/collections")).collections;
  }

  collection(name: string): Collection { return new Collection(this, name); }

  async createCollection(name: string, o: { documents?: boolean; vectorSize?: number; distance?: string; graph?: boolean } = {}):
    Promise<Collection> {
    const body: Record<string, unknown> = { documents: o.documents ?? false, graph: o.graph ?? null };
    if (o.vectorSize !== undefined) body.vector = { size: o.vectorSize, distance: o.distance ?? "cosine" };
    await this.call("PUT", `/v1/collections/${seg(name)}`, body);
    return new Collection(this, name);
  }

  deleteCollection(name: string) { return this.call("DELETE", `/v1/collections/${seg(name)}`); }

  // -- SQL, consistency, tenants, keys

  sql(query: string, params?: unknown[] | Record<string, unknown>, o: { readOnly?: boolean; timeoutS?: number; maxRows?: number } = {}):
    Promise<{ columns: string[]; rows: Json[][]; row_count: number; truncated: boolean }> {
    return this.call("POST", "/v1/sql", {
      query, params: params ?? null, read_only: o.readOnly ?? false, timeout_s: o.timeoutS ?? 30, max_rows: o.maxRows ?? 1000,
    });
  }

  reconcile(o: { repair?: boolean; allTenants?: boolean } = {}) {
    return this.call("POST", "/v1/admin/reconcile", { dry_run: !o.repair, all_tenants: o.allTenants ?? false });
  }

  async tenants(): Promise<Record<string, any>[]> { return (await this.call("GET", "/v1/admin/tenants")).tenants; }

  createKey(role: "read" | "admin" = "read", o: { name?: string; tenant?: string } = {}):
    Promise<{ id: string; key: string; tenant: string; role: string }> {
    return this.call("POST", "/v1/keys", { role, name: o.name ?? null, tenant: o.tenant ?? null });
  }

  async keys(allTenants = false): Promise<Record<string, any>[]> {
    return (await this.call("GET", `/v1/keys?all_tenants=${allTenants}`)).keys;
  }

  revokeKey(id: string) { return this.call("DELETE", `/v1/keys/${seg(id)}`); }

  // -- snapshots and backups (the admin key from the server's environment)

  createSnapshot(name?: string) { return this.call("POST", "/v1/admin/snapshots", { name: name ?? null }, 3_600_000); }
  async snapshots(): Promise<Record<string, any>[]> { return (await this.call("GET", "/v1/admin/snapshots")).snapshots; }
  restoreSnapshot(id: string, backupFirst = true) {
    return this.call("POST", `/v1/admin/snapshots/${seg(id)}/restore`, { backup_first: backupFirst }, 3_600_000);
  }
  deleteSnapshot(id: string) { return this.call("DELETE", `/v1/admin/snapshots/${seg(id)}`); }
  backups() { return this.call("GET", "/v1/admin/backups"); }
  runBackup() { return this.call("POST", "/v1/admin/backups/run", undefined, 3_600_000); }

  // -- webhooks

  createWebhook(url: string, o: { events?: string[]; collections?: string[]; secret?: string } = {}) {
    const body: Record<string, unknown> = { url, collections: o.collections ?? null, secret: o.secret ?? null };
    if (o.events) body.events = o.events;
    return this.call("POST", "/v1/webhooks", body);
  }
  async webhooks(): Promise<Record<string, any>[]> { return (await this.call("GET", "/v1/webhooks")).webhooks; }
  deleteWebhook(id: string) { return this.call("DELETE", `/v1/webhooks/${seg(id)}`); }
}

export class Collection {
  private readonly p: string;

  constructor(readonly db: SwsDB, readonly name: string) {
    this.p = `/v1/collections/${seg(name)}`;
  }

  info() { return this.db.call("GET", this.p); }
  drop() { return this.db.deleteCollection(this.name); }

  // -- documents

  addText(id: string, text: string, o: { title?: string; metadata?: Record<string, Json>; readers?: string[];
    format?: "markdown" | "text" | "html" } = {}): Promise<DocumentInfo> {
    return this.db.call("POST", `${this.p}/documents/text`, {
      id, text, title: o.title ?? null, format: o.format ?? "markdown", metadata: o.metadata ?? null, readers: o.readers ?? null,
    });
  }

  /** Upload a file's bytes (PDF, Markdown, HTML, text). `filename` decides how it is parsed. */
  upload(data: Blob | Uint8Array | ArrayBuffer, filename: string,
    o: { id?: string; title?: string; metadata?: Record<string, Json>; readers?: string[] } = {}): Promise<DocumentInfo> {
    const form = new FormData();
    const blob = data instanceof Blob ? data : new Blob([data as BlobPart]);
    form.append("file", blob, filename);
    form.append("id", o.id ?? filename);
    if (o.title) form.append("title", o.title);
    if (o.metadata) form.append("metadata", JSON.stringify(o.metadata));
    if (o.readers) form.append("readers", JSON.stringify(o.readers));
    return this.db.call("POST", `${this.p}/documents`, form, 600_000);
  }

  document(id: string): Promise<DocumentInfo> { return this.db.call("GET", `${this.p}/documents/${seg(id)}`); }

  documents(o: { limit?: number; offset?: number; status?: string } = {}): Promise<{ documents: DocumentInfo[]; total: number }> {
    const q = new URLSearchParams({ limit: String(o.limit ?? 100), offset: String(o.offset ?? 0) });
    if (o.status) q.set("status", o.status);
    return this.db.call("GET", `${this.p}/documents?${q}`);
  }

  deleteDocument(id: string) { return this.db.call("DELETE", `${this.p}/documents/${seg(id)}`); }

  /** Resolve once the document is searchable; reject if its processing failed or `timeoutMs` passes. */
  async waitReady(id: string, timeoutMs = 600_000, pollMs = 500): Promise<DocumentInfo> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const d = await this.document(id);
      if (d.status === "ready") return d;
      if (d.status === "failed") throw new SwsDBError(422, "document_failed", `${id}: ${d.error}`);
      if (Date.now() > deadline) throw new SwsDBError(408, "timeout", `${id} still ${d.status} after ${timeoutMs} ms`);
      await sleep(pollMs);
    }
  }

  async query(query: string, o: QueryOptions = {}): Promise<Hit[]> {
    return (await this.db.call("POST", `${this.p}/query`, queryBody(query, o))).hits;
  }

  answer(question: string, o: QueryOptions & { generate?: boolean } = {}): Promise<Answer> {
    const body = queryBody(question, o, "question");
    if (o.generate !== undefined) body.generate = o.generate;
    return this.db.call("POST", `${this.p}/answer`, body, 180_000);
  }

  text(id: string, principals?: string[]): Promise<{ id: string; text: string; version: number; title: string | null }> {
    const q = principals ? `?principals=${encodeURIComponent(principals.join(","))}` : "";
    return this.db.call("GET", `${this.p}/text/${seg(id)}${q}`);
  }

  diagnose(id: string) { return this.db.call("GET", `${this.p}/diagnose/${seg(id)}`); }

  putEvalSet(name: string, cases: { query: string; relevant: string[]; answer?: string }[]) {
    return this.db.call("PUT", `${this.p}/eval/sets/${seg(name)}`, { cases });
  }

  evaluate(set: string, o: { k?: number; configs?: Record<string, unknown>[]; principals?: string[] } = {}) {
    const body: Record<string, unknown> = { set, k: o.k ?? 5 };
    if (o.configs) body.configs = o.configs;
    if (o.principals) body.principals = o.principals;
    return this.db.call("POST", `${this.p}/eval/runs`, body, 600_000);
  }

  // -- raw vectors and graphs

  async upsert(points: { id: string | number; vector: number[]; payload?: Record<string, Json> }[], batchSize = 500):
    Promise<number> {
    let total = 0;
    for (let i = 0; i < points.length; i += batchSize) {
      total += (await this.db.call("PUT", `${this.p}/points`, { points: points.slice(i, i + batchSize) })).upserted;
    }
    return total;
  }

  async search(vector: number[], o: { limit?: number; where?: Where; withPayload?: boolean } = {}):
    Promise<{ id: string | number; score: number; payload?: Record<string, Json> }[]> {
    return (await this.db.call("POST", `${this.p}/search`, {
      vector, limit: o.limit ?? 10, where: o.where ?? null, with_payload: o.withPayload ?? true,
    })).hits;
  }

  async get(ids: (string | number)[]) { return (await this.db.call("POST", `${this.p}/points/get`, { ids })).points; }
  async delete(ids: (string | number)[]): Promise<number> {
    return (await this.db.call("POST", `${this.p}/points/delete`, { ids })).deleted;
  }

  cypher(query: string, params?: Record<string, Json>, o: { readOnly?: boolean; timeoutS?: number } = {}):
    Promise<{ columns: string[]; rows: Json[][]; stats: string[] }> {
    return this.db.call("POST", `${this.p}/cypher`, {
      query, params: params ?? null, read_only: o.readOnly ?? false, timeout_s: o.timeoutS ?? 30,
    });
  }
}
