// Unit tests with a stand-in fetch, and (when SWSDB_URL is set) an integration test against a real server.
import assert from "node:assert/strict";
import { test } from "node:test";
import { SwsDB, SwsDBError } from "../dist/index.js";

function fakeFetch(handler) {
  const calls = [];
  const fn = async (url, init) => {
    const body = init.body && typeof init.body === "string" ? JSON.parse(init.body) : init.body;
    calls.push({ url, method: init.method, headers: init.headers, body });
    const [status, data] = handler(url, init.method, body);
    return new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json" } });
  };
  fn.calls = calls;
  return fn;
}

test("requests carry the key, tenant, snake_case bodies and encoded ids", async () => {
  const f = fakeFetch((url) => {
    if (url.endsWith("/query")) return [200, { hits: [{ document: "a", score: 1 }] }];
    if (url.includes("/documents/a%2Fb")) return [200, { id: "a/b", status: "ready" }];
    return [201, { name: "kb" }];
  });
  const db = new SwsDB({ url: "http://x/", apiKey: "k", tenant: "acme", fetch: f });
  const kb = await db.createCollection("kb", { documents: true });
  const hits = await kb.query("refunds", { k: 3, graphHops: 1, rerank: true, principals: ["user:7"] });
  assert.equal(hits[0].document, "a");
  assert.equal((await kb.waitReady("a/b")).status, "ready");
  const q = f.calls[1];
  assert.equal(q.url, "http://x/v1/collections/kb/query");
  assert.deepEqual(q.body, { query: "refunds", mode: "hybrid", k: 3, principals: ["user:7"], graph_hops: 1, rerank: true });
  assert.equal(q.headers["api-key"], "k");
  assert.equal(q.headers["swsdb-tenant"], "acme");
  assert.equal(f.calls[2].url, "http://x/v1/collections/kb/documents/a%2Fb");
});

test("errors carry the status, code and message", async () => {
  const db = new SwsDB({ url: "http://x", fetch: fakeFetch(() => [403, { error: "forbidden", message: "not yours" }]) });
  await assert.rejects(db.status(), (e) => e instanceof SwsDBError && e.status === 403 && e.code === "forbidden");
  const down = new SwsDB({ url: "http://x", fetch: async () => { throw new TypeError("fetch failed"); } });
  await assert.rejects(down.collections(), (e) => e instanceof SwsDBError && e.status === 0);
});

const url = process.env.SWSDB_URL;
test("against a real server", { skip: !url && "set SWSDB_URL (and SWSDB_API_KEY) to run" }, async () => {
  const db = new SwsDB({ url, apiKey: process.env.SWSDB_API_KEY, tenant: `ts-${Date.now() % 100000}` });
  assert.ok(await db.ready());
  const kb = await db.createCollection("handbook", { documents: true });
  await kb.addText("refunds", "# Refunds\n\nRefunds are issued within five business days.", { title: "Refund policy" });
  await kb.upload(new TextEncoder().encode("Orders ship from Rotterdam within 24 hours."), "shipping.txt");
  await kb.waitReady("refunds");
  await kb.waitReady("shipping.txt");
  const hits = await kb.query("where do orders ship from", { k: 1, graphHops: 0 });
  assert.equal(hits[0].document, "shipping.txt");
  const a = await kb.answer("How long do refunds take?", { generate: false });
  assert.match(a.answer, /five business days/);
  assert.deepEqual(a.cited, [1]);
  const sql = await db.sql("SELECT %s::text AS v", ["hi"]);
  assert.deepEqual(sql.rows, [["hi"]]);
  await kb.deleteDocument("refunds");
  await assert.rejects(kb.text("missing"), (e) => e.status === 404);
  await db.deleteCollection("handbook");
});
