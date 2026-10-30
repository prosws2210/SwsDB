# TypeScript SDK

A client for [swsdb](https://github.com/prosws2210/SwsDB), one database for AI applications. No dependencies: it uses
the global `fetch`, so it runs in Node 18+, Deno, Bun and browsers.

```bash
npm install swsdb
```

```ts
import { readFile } from "node:fs/promises";
import { SwsDB } from "swsdb";

const db = new SwsDB({ url: "http://localhost:6464", apiKey: process.env.SWSDB_API_KEY });
const kb = await db.createCollection("handbook", { documents: true });

await kb.addText("refunds", "# Refunds\n\nRefunds are issued within five business days.");
await kb.upload(await readFile("policy.pdf"), "policy.pdf");
await kb.waitReady("refunds");

const { answer, passages } = await kb.answer("How long do refunds take?");
const hits = await kb.query("refund policy", { k: 5, graphHops: 1, principals: ["user:42", "group:staff"] });
```

Options are camelCase (`graphHops`, `readOnly`); everything else follows the
[REST API](https://github.com/prosws2210/SwsDB/blob/main/docs/reference/api.md). Failed calls throw `SwsDBError`
with `status` and `code`.

Don't put an admin key in browser code: browsers should call your backend, which calls swsdb.
