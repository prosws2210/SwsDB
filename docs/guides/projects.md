# Start a new project on swsdb

A new project usually starts with a Postgres container, a Qdrant container and maybe a graph database, each with its
own setup, passwords and backups. Instead, run one swsdb and give each project its own space in it:

- a **Postgres connection string** for SQLAlchemy, Alembic, Django, Prisma, node-postgres or psql;
- the **Qdrant API** for the standard Qdrant client;
- **documents, graphs and cited answers** through swsdb's own API and SDKs.

A project is a swsdb **tenant**. It gets its own schema, collections and keys, and it can't see another project's data.
One swsdb can hold many projects.

## 1. Start swsdb

```bash
docker run -d --name swsdb \
  -p 6464:6464 -p 5432:5432 \
  -v swsdb-data:/swsdb/data \
  -e SWSDB_API_KEY=change-me-to-a-long-random-key \
  ghcr.io/prosws2210/swsdb:latest
```

`6464` is swsdb's API, its console and the Qdrant API. `5432` is Postgres, for project connections. If 5432 is taken on
your machine, publish another port (`-p 5433:5432`) and set `SWSDB_POSTGRES_PUBLIC_PORT=5433` so connection strings
show it.

The key you set is the server's admin key: it reaches every project. Keep it for administration and give each
project its own key (next step).

## 2. Make the project's key

In the console (<http://localhost:6464/console>), open **API keys**, pick a tenant name such as `shop`, and make an
admin key. Or with curl:

```bash
curl -X POST localhost:6464/v1/keys -H "api-key: $SWSDB_ADMIN_KEY" \
  -H "content-type: application/json" -d '{"tenant": "shop", "role": "admin", "name": "shop backend"}'
```

The key is shown once. Every call made with it lands in project `shop`, so your code never has to name the project.
Make a `read` key too for code that only searches.

## 3. Postgres: a connection string

In the console, sign in with the project's key, open **Connect** and click **Create connection string**. Or:

```bash
curl -X POST localhost:6464/v1/postgres/credentials -H "api-key: $SHOP_KEY"
```

```json
{"user": "swsdb_t_shop__c", "password": "…", "database": "swsdb", "schema": "swsdb_t_shop",
 "url": "postgresql://swsdb_t_shop__c:…@localhost:5432/swsdb",
 "prisma_url": "postgresql://swsdb_t_shop__c:…@localhost:5432/swsdb?schema=swsdb_t_shop"}
```

The password is shown only this once; asking again gives a new one (the old one stops working for new connections).
Put the URL in your app's environment as `DATABASE_URL`. Use `url` everywhere except Prisma, which takes `prisma_url`.
SQLAlchemy, Django, Prisma 6 and 7, and psycopg run against a real swsdb in its test suite:

=== "SQLAlchemy / Alembic"

    ```python
    import os
    from sqlalchemy import create_engine

    engine = create_engine(os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://"))
    ```

    Alembic: set `sqlalchemy.url` from the same variable in `env.py`.

=== "Django"

    ```python
    # settings.py (pip install dj-database-url "psycopg[binary]")
    import dj_database_url
    DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"])}
    ```

    `python manage.py migrate` and `manage.py test` work: the test runner creates its test database, migrates it and
    drops it afterwards. Database names are shared by every project on the server, so if several projects run Django
    tests on one swsdb, give each its own test database name: `DATABASES["default"]["TEST"] = {"NAME": "test_shop"}`.

=== "Prisma 7"

    Prisma works in schema `public` unless the URL names one, so give it **`prisma_url`** (the connection string with
    `?schema=<the project's schema>`) as `DATABASE_URL`, and the schema to the adapter:

    ```ts
    // prisma.config.ts
    import { defineConfig, env } from "prisma/config";
    export default defineConfig({ schema: "prisma/schema.prisma", datasource: { url: env("DATABASE_URL") } });
    ```

    ```ts
    // the client
    import { PrismaClient } from "./generated/prisma/client";
    import { PrismaPg } from "@prisma/adapter-pg";
    const adapter = new PrismaPg({ connectionString: process.env.DATABASE_URL }, { schema: "swsdb_t_shop" });
    const prisma = new PrismaClient({ adapter });
    ```

    `prisma migrate dev` (with the shadow database it creates and drops), `migrate deploy`, `migrate status`,
    `db pull`, `generate` and the client are tested against swsdb.

=== "Prisma 6"

    ```prisma
    datasource db {
      provider = "postgresql"
      url      = env("DATABASE_URL")   // the prisma_url: ...?schema=swsdb_t_shop
    }
    ```

    `prisma db push` and the client are tested against swsdb.

=== "Node (pg)"

    ```js
    import pg from "pg";
    const pool = new pg.Pool({ connectionString: process.env.DATABASE_URL });
    ```

=== "psql"

    ```bash
    psql "$DATABASE_URL"
    ```

The login works in the project's own schema, and tables need no schema prefix. Those are the same tables you see in the
console's **Tables** page, query with `/v1/sql`, and serve through the [Data API](../reference/api.md#data-api-and-functions).
Everything a migration creates belongs to the project.

A project may also create databases of its own, such as Django's test database and Prisma's shadow database. They
belong to the project, other projects can't read or drop them, and the Connect drawer lists them. They are scratch
space: snapshots cover swsdb's database, which holds the project's schema, and not these.

What a project login can't do: read other projects' schemas, databases or swsdb's own catalog, create roles or
extensions, switch to another role, or read and write files on the server. It allows 20 connections at a time, so size
your connection pool below that.

**Revoke** in the Connect drawer (or `DELETE /v1/postgres/credentials`) removes the login and ends its open sessions.
The data stays.

**Backups.** A [snapshot](backups.md) includes each project's schema and its login. Restore it on another server and
the same `DATABASE_URL` works there, with only the host changing.

## 4. Vectors: the standard Qdrant client

swsdb serves Qdrant's REST API at `/qdrant`. The key is your project's swsdb key, so the client sees only that project's
collections.

=== "Python"

    ```python
    import os
    from qdrant_client import QdrantClient, models

    q = QdrantClient(url="http://localhost:6464", prefix="qdrant", api_key=os.environ["SHOP_KEY"])
    q.create_collection("items", vectors_config=models.VectorParams(size=384, distance=models.Distance.COSINE))
    q.upsert("items", points=[models.PointStruct(id=1, vector=[...], payload={"sku": "K-1"})])
    hits = q.query_points("items", query=[...], limit=5).points
    ```

=== "TypeScript"

    ```ts
    import { QdrantClient } from "@qdrant/js-client-rest";

    const q = new QdrantClient({ url: "http://localhost:6464", prefix: "qdrant", apiKey: process.env.SHOP_KEY });
    await q.getCollections();
    ```

The client may warn about an API key over plain HTTP. On your machine that's fine; elsewhere, run swsdb with
[HTTPS](deployment.md).

Collections you make this way are ordinary swsdb collections: listed in the console, searchable through
`/v1/collections/{name}/search`, and included in snapshots.

- **REST only.** Leave the client on HTTP (the default), not gRPC.
- **Collections and points only.** Cluster, aliases, snapshots and shards aren't offered, because they would reach
  beyond the project. Take backups with swsdb's [snapshots](backups.md).
- **Writes need the admin key.** The read key can read and search.
- **Documents collections are swsdb's to manage.** You can read one with the admin key but not write it here. Upload
  documents through `/v1/collections/{name}/documents` instead.
- **Not in the lite profile.** There, vectors live in Postgres and there is no Qdrant.

## 5. Documents, graphs and answers

For uploads, hybrid search, entity graphs and cited answers, use swsdb's SDKs with the same key:

```python
from swsdb import SwsDB

db = SwsDB("http://localhost:6464", api_key=os.environ["SHOP_KEY"])
kb = db.create_collection("handbook", documents=True)
kb.add_text("refunds", "Refunds are accepted within 30 days.")
kb.wait_ready("refunds")
print(kb.answer("How long do refunds take?")["answer"])
```

See [Documents and retrieval](documents.md).

## Docker Compose

Your app and swsdb side by side. From the app's container, the host is the service name:

```yaml
services:
  swsdb:
    image: ghcr.io/prosws2210/swsdb:latest
    ports: ["6464:6464"]          # the console from your machine; add "5432:5432" for psql or a desktop client
    volumes: [swsdb-data:/swsdb/data]
    environment:
      SWSDB_API_KEY: ${SWSDB_ADMIN_KEY}

  app:
    build: .
    depends_on:
      swsdb: { condition: service_healthy }
    environment:
      DATABASE_URL: postgresql://swsdb_t_shop__c:${SHOP_DB_PASSWORD}@swsdb:5432/swsdb
      SWSDB_URL: http://swsdb:6464
      SWSDB_API_KEY: ${SHOP_KEY}

volumes:
  swsdb-data:
```

The image has a health check, so `service_healthy` waits until every engine is ready. Make the project's key and
connection string once (steps 2 and 3) and keep them in your `.env`. They are stored in the volume, so they survive
restarts and upgrades.

## Turning project connections off

`SWSDB_POSTGRES_ACCESS=false` keeps Postgres on localhost inside the container. Connection strings are then refused,
and everything else works as before. See [configuration](../reference/configuration.md) and
[security](security.md#project-connections).
