# CLI

`pip install swsdb` installs the `swsdb` command. Global options go before the command:

```text
swsdb [--url URL] [--api-key KEY] [--tenant TENANT] [--json] [--insecure] <command> ...
```

They default to `SWSDB_URL`, `SWSDB_API_KEY` and `SWSDB_TENANT`. `--json` prints the raw API response. `--insecure`
skips TLS certificate checks (self-signed test servers only).

| Command | Does |
|---|---|
| `swsdb demo` | load a sample handbook and answer questions with citations |
| `swsdb status` | engines, versions and latency; exits 1 unless ready |
| `swsdb collections` | list collections |
| `swsdb create NAME [--no-graph]` | create a documents collection |
| `swsdb ingest NAME PATH... [--readers group:hr] [--no-wait]` | add files and folders; waits until searchable |
| `swsdb search NAME "query" [-k 5] [--mode hybrid\|dense\|sparse]` | passages with scores |
| `swsdb ask NAME "question" [-k 5]` | a cited answer and its sources |
| `swsdb sql "SELECT ..." [--read-only]` | one SQL statement in the tenant's schema |
| `swsdb cypher NAME "MATCH ..." [--read-only]` | one Cypher query on a collection's graph |
| `swsdb doctor [--repair] [--all-tenants]` | compare stores with the catalog; queue fixes |
| `swsdb snapshot create [--name N] [-o FILE]` | snapshot every engine (and download it) |
| `swsdb snapshot list` / `download ID [-o FILE]` / `delete ID` | manage snapshots |
| `swsdb snapshot restore ID\|FILE [--no-backup] [-y]` | replace all data with a snapshot |
| `swsdb eval NAME SET [--questions FILE] [-k 5] [--rerank]` | compare configurations on a golden set |
| `swsdb webhooks create URL [--events ...]` / `list` / `deliveries ID` / `ping ID` / `delete ID` | webhooks |
| `swsdb tenants` | tenants and what they hold (environment admin key) |
| `swsdb keys create [--role read\|admin] [--name N]` | a key for `--tenant`, printed once |
| `swsdb keys list [--all-tenants]` / `keys revoke ID` | manage keys |
| `swsdb mcp [--read-write] [--http PORT]` | run the MCP server (stdio, or HTTP); needs `swsdb[mcp]` |

Inside the container, `swsdb-admin` has operator commands that need no key from you:

| Command | Does |
|---|---|
| `swsdb-admin status` | readiness of each engine |
| `swsdb-admin credentials` | the engines' own connection strings and passwords |
| `swsdb-admin snapshot create\|list\|restore ...` | `swsdb snapshot` with the container's admin key |
