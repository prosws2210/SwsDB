"""One swsdb in place of a project's own Postgres and Qdrant containers: a project's code connects with its usual tools
(a Postgres connection string for SQLAlchemy and friends, the standard Qdrant client) and reaches its own data only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import uuid
import warnings
from pathlib import Path

import httpx
import psycopg
import pytest
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from sqlalchemy import create_engine, text
from swsdb import SwsDB
from test_container import Box, free_port, sh

pytestmark = pytest.mark.image

ADMIN = "projects-admin-key"


@pytest.fixture(scope="module")
def box():
    """swsdb with Postgres published, on its own network so other containers (a Node app) reach it by name."""
    vol = f"swsdb-projects-{uuid.uuid4().hex[:8]}"
    net = f"{vol}-net"
    sh("docker", "network", "create", net)
    pg = free_port()
    b = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_POSTGRES_PUBLIC_PORT": str(pg)},
            extra=["-p", f"127.0.0.1:{pg}:5432", "--network", net]).start()
    b.pg_port, b.net = pg, net
    try:
        b.wait_ready()
        yield b
    finally:
        b.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)
        sh("docker", "network", "rm", net, check=False)


def api(box: Box, key: str = ADMIN, tenant: str | None = None) -> httpx.Client:
    headers = {"api-key": key, **({"swsdb-tenant": tenant} if tenant else {})}
    return httpx.Client(base_url=f"http://127.0.0.1:{box.port}", headers=headers, timeout=120)


def ok(r: httpx.Response, status: int = 200):
    assert r.status_code == status, f"{r.status_code}: {r.text[:400]}"
    return r.json()


@pytest.fixture(scope="module")
def keys(box):
    """An admin and a read-only key for project shop, and an admin key for project rival."""
    a = api(box)
    return {name: ok(a.post("/v1/keys", json={"role": role, "tenant": tenant, "name": name}), 201)["key"]
            for name, role, tenant in (("shop", "admin", "shop"), ("shop_read", "read", "shop"),
                                       ("rival", "admin", "rival"))}


def connect(url: str, **kw) -> psycopg.Connection:
    return psycopg.connect(url, connect_timeout=10, autocommit=True, **kw)


def refused(url: str) -> str:
    with pytest.raises(psycopg.OperationalError) as e:
        connect(url).close()
    return str(e.value)


# ------------------------------------------------------------------ Postgres


def test_a_project_connects_with_sqlalchemy_and_its_tables_belong_to_it(box, keys):
    shop = api(box, keys["shop"])
    assert ok(shop.get("/v1/postgres"))["issued"] is False
    cred = ok(shop.post("/v1/postgres/credentials"), 201)
    assert cred["user"] == "swsdb_t_shop__c" and cred["schema"] == "swsdb_t_shop"
    assert cred["url"].startswith(f"postgresql://swsdb_t_shop__c:{cred['password']}@127.0.0.1:{box.pg_port}/swsdb")

    engine = create_engine(cred["url"].replace("postgresql://", "postgresql+psycopg://"))
    with engine.begin() as conn:  # a migration: DDL and data in one transaction
        conn.execute(text("CREATE TABLE products (id serial PRIMARY KEY, name text NOT NULL, price numeric)"))
        conn.execute(text("INSERT INTO products (name, price) VALUES ('kettle', 24.5), ('lamp', 12)"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM products")).scalar() == 2
        info = ok(shop.get("/v1/postgres"))
        assert info["issued"] is True and info["sessions"] >= 1 and "PASSWORD" in info["url"]
    engine.dispose()

    # the same table through swsdb: the SQL API, the Data API and the console see it, owned by the project
    rows = ok(shop.post("/v1/sql", json={"query": "SELECT name FROM products ORDER BY id", "read_only": True}))
    assert rows["rows"] == [["kettle"], ["lamp"]]
    assert float(ok(shop.get("/v1/rest/products?name=eq.lamp"))["data"][0]["price"]) == 12
    assert box.psql("SELECT tableowner FROM pg_tables WHERE tablename = 'products'") == "swsdb_t_shop"


def test_a_project_login_reaches_its_own_schema_only(box, keys):
    ok(api(box).post("/v1/sql", json={"query": "CREATE TABLE default_secret (x int)"}))
    ok(api(box, keys["rival"]).post("/v1/sql", json={"query": "CREATE TABLE rival_secret (x int)"}))
    cred = ok(api(box, keys["shop"]).post("/v1/postgres/credentials"), 201)
    with connect(cred["url"]) as conn:
        assert conn.execute("SELECT current_user, session_user").fetchone() == ("swsdb_t_shop", "swsdb_t_shop__c")
        for stmt in ("SELECT * FROM public.default_secret", "SELECT * FROM swsdb_t_rival.rival_secret",
                     "SELECT * FROM swsdb.documents", "SELECT * FROM swsdb.api_keys",
                     "CREATE EXTENSION IF NOT EXISTS dblink", "CREATE ROLE sneaky", "SET ROLE swsdb_app",
                     "SET ROLE swsdb_t_rival", "COPY products TO '/tmp/out.csv'",
                     "SELECT pg_read_file('/swsdb/data/swsdb/secrets.json')"):
            with pytest.raises(psycopg.Error):
                conn.execute(stmt)
        # RESET ROLE goes back to the project's role; SET ROLE NONE to the login itself, which can do nothing: what a
        # project makes always belongs to the project
        conn.execute("RESET ROLE")
        assert conn.execute("SELECT current_user").fetchone() == ("swsdb_t_shop",)
        conn.execute("SET ROLE NONE")
        assert conn.execute("SELECT current_user").fetchone() == ("swsdb_t_shop__c",)
        for stmt in ("CREATE TABLE mine (x int)", "CREATE TABLE swsdb_t_shop.mine (x int)",
                     "SELECT * FROM swsdb_t_shop.products"):
            with pytest.raises(psycopg.Error):
                conn.execute(stmt)

    base = f"@127.0.0.1:{box.pg_port}"
    # from the network only project logins, only into swsdb's database: not the superuser, not a tenant's own role
    for user in ("swsdb", "swsdb_app", "swsdb_t_shop", "postgres"):
        refused(f"postgresql://{user}:{cred['password']}{base}/swsdb")
    refused(f"postgresql://swsdb_t_shop__c:wrong-password{base}/swsdb")


def test_a_projects_own_databases_stay_its_own(box, keys):
    """A project may create databases (Django's test database, Prisma's shadow database): they belong to it, and
    another project can't read or drop them."""
    shop = ok(api(box, keys["shop"]).post("/v1/postgres/credentials"), 201)
    rival = ok(api(box, keys["rival"]).post("/v1/postgres/credentials"), 201)
    with connect(shop["url"]) as conn:
        conn.execute("CREATE DATABASE shop_scratch")
    scratch = shop["url"].rsplit("/", 1)[0] + "/shop_scratch"
    with connect(scratch) as conn:  # its own public schema there
        conn.execute("CREATE TABLE t (secret text)")
        conn.execute("INSERT INTO t VALUES ('shop only')")
        assert conn.execute("SELECT current_user").fetchone() == ("swsdb_t_shop",)
    assert "shop_scratch" in ok(api(box, keys["shop"]).get("/v1/postgres"))["databases"]
    with connect(scratch.replace(f"swsdb_t_shop__c:{shop['password']}", f"swsdb_t_rival__c:{rival['password']}")) as c:
        for stmt in ("SELECT * FROM t", "DROP TABLE t", "CREATE TABLE mine (x int)"):
            with pytest.raises(psycopg.Error):
                c.execute(stmt)
    with connect(rival["url"]) as conn:
        for stmt in ("DROP DATABASE shop_scratch", "ALTER DATABASE shop_scratch RENAME TO taken",
                     "CREATE DATABASE x TEMPLATE shop_scratch"):  # a copy would carry shop's data
            with pytest.raises(psycopg.Error):
                conn.execute(stmt)
    # the HTTP SQL API never creates databases (every statement runs in a transaction)
    assert api(box, keys["shop"]).post("/v1/sql", json={"query": "CREATE DATABASE via_http"}).status_code == 400
    with connect(shop["url"]) as conn:
        conn.execute("DROP DATABASE shop_scratch")
    assert ok(api(box, keys["shop"]).get("/v1/postgres"))["databases"] == []


def test_new_passwords_and_revoking(box, keys):
    shop = api(box, keys["shop"])
    first = ok(shop.post("/v1/postgres/credentials"), 201)
    open_conn = connect(first["url"])
    second = ok(shop.post("/v1/postgres/credentials"), 201)
    assert second["password"] != first["password"]
    refused(first["url"])  # the old password stops working for new sessions
    assert open_conn.execute("SELECT 1").fetchone() == (1,)  # sessions already open keep running
    connect(second["url"]).close()

    assert ok(shop.delete("/v1/postgres/credentials"))["revoked"] is True
    refused(second["url"])
    with pytest.raises(psycopg.Error):  # and open sessions end
        open_conn.execute("SELECT 1")
    open_conn.close()
    assert ok(shop.get("/v1/postgres"))["issued"] is False
    assert shop.delete("/v1/postgres/credentials").status_code == 404
    # the data stays: the login was only a way in
    assert ok(shop.post("/v1/sql", json={"query": "SELECT count(*) FROM products", "read_only": True}))["rows"] == [[2]]


def test_only_a_projects_admin_key_issues_its_login(box, keys):
    assert api(box, keys["shop_read"]).post("/v1/postgres/credentials").status_code == 403
    assert api(box, keys["rival"], tenant="shop").post("/v1/postgres/credentials").status_code == 403
    # the environment's key may issue one for any project
    cred = ok(api(box, tenant="rival").post("/v1/postgres/credentials"), 201)
    with connect(cred["url"]) as conn:
        assert conn.execute("SELECT count(*) FROM rival_secret").fetchone() == (0,)


# ------------------------------------------------------------------ Qdrant


def qdrant(box: Box, key: str) -> QdrantClient:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "api key over an insecure connection": a local test
        return QdrantClient(url=f"http://127.0.0.1:{box.port}", prefix="qdrant", api_key=key, timeout=60)


def test_the_standard_qdrant_client_works_on_the_projects_collections(box, keys):
    q = qdrant(box, keys["shop"])
    q.create_collection("items", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))
    assert q.collection_exists("items")
    q.upsert("items", points=[models.PointStruct(id=i, vector=[1.0, i, 0.5, 0.0], payload={"n": i, "kind": k})
                              for i, k in ((1, "a"), (2, "b"), (3, "a"))])
    hits = q.query_points("items", query=[1.0, 1.0, 0.5, 0.0], limit=2,
                          query_filter=models.Filter(must=[models.FieldCondition(
                              key="kind", match=models.MatchValue(value="a"))])).points
    assert [h.payload["kind"] for h in hits] == ["a", "a"]
    assert q.count("items").count == 3
    assert q.retrieve("items", ids=[2])[0].payload == {"n": 2, "kind": "b"}
    assert [c.name for c in q.get_collections().collections] == ["items"]
    q.delete("items", points_selector=models.PointIdsList(points=[3]))
    assert q.count("items").count == 2

    # it is a swsdb collection like any other: listed, searchable through swsdb's API, and in snapshots
    listed = ok(api(box, keys["shop"]).get("/v1/collections"))["collections"]
    assert {"name": "items", "vector": True, "graph": False} in listed
    found = ok(api(box, keys["shop"]).post("/v1/collections/items/search", json={"vector": [1, 2, 0.5, 0], "limit": 1}))
    assert found["hits"][0]["id"] == 2


def test_projects_never_see_each_others_collections(box, keys):
    rival = qdrant(box, keys["rival"])
    assert "items" not in [c.name for c in rival.get_collections().collections]
    assert not rival.collection_exists("items")
    with pytest.raises(UnexpectedResponse) as e:
        rival.count("items")
    assert e.value.status_code == 404 and "sw_" not in str(e.value)  # errors name the project's own collection
    # rival's own "items" is another collection; a lookup in another collection stays within the project
    rival.create_collection("items", vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))
    rival.upsert("items", points=[models.PointStruct(id=7, vector=[0.0, 0.0, 1.0, 1.0])])
    with pytest.raises(UnexpectedResponse):  # shop's point 1 doesn't exist in rival's collection
        rival.query_points("items", query=1, lookup_from=models.LookupLocation(collection="items"), limit=1)
    assert qdrant(box, keys["shop"]).count("items").count == 2

    raw = httpx.Client(base_url=f"http://127.0.0.1:{box.port}/qdrant", headers={"api-key": keys["rival"]}, timeout=30)
    for method, path, body in (
            ("GET", "/aliases", None), ("GET", "/cluster", None), ("GET", "/collections/aliases", None),
            ("POST", "/collections/items/snapshots", None), ("GET", "/collections/items/shards", None),
            ("PUT", "/collections/copy", {"vectors": {"size": 4, "distance": "Cosine"},
                                          "init_from": {"collection": "sw_shop__items"}}),
            ("PUT", "/collections/sw_shop__items/points", {"points": []})):
        r = raw.request(method, path, json=body)
        assert r.status_code in (403, 404, 422), (method, path, r.status_code, r.text)
    assert qdrant(box, keys["shop"]).count("items").count == 2


def test_keys_roles_and_documents_collections(box, keys):
    read = qdrant(box, keys["shop_read"])
    assert read.count("items").count == 2  # the read-only key reads and searches
    with pytest.raises(UnexpectedResponse) as e:
        read.upsert("items", points=[models.PointStruct(id=9, vector=[1, 1, 1, 1])])
    assert e.value.status_code == 403
    with pytest.raises(UnexpectedResponse):
        read.delete_collection("items")

    shop = api(box, keys["shop"])
    ok(shop.put("/v1/collections/docs", json={"documents": True}), 201)
    q = qdrant(box, keys["shop"])
    assert q.count("docs").count == 0  # the admin key may read a documents collection
    with pytest.raises(UnexpectedResponse) as e:
        q.upsert("docs", points=[models.PointStruct(id=1, vector={"dense": [0.0] * 384})])
    assert e.value.status_code == 403
    with pytest.raises(UnexpectedResponse):
        q.delete_collection("docs")
    with pytest.raises(UnexpectedResponse):
        read.count("docs")  # it would skip document readers

    assert httpx.get(f"http://127.0.0.1:{box.port}/qdrant/collections", timeout=10).status_code == 401


# ------------------------------------------------------------------ the tools projects use: Django, Prisma, Qdrant JS

DJANGO_APP = """
import json, os, sys
import django
import dj_database_url
from django.conf import settings

sys.path.insert(0, os.environ["APP_DIR"])
settings.configure(
    DATABASES={"default": dj_database_url.parse(os.environ["DATABASE_URL"])},
    INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "django.contrib.sessions", "shopapp"],
    USE_TZ=True, DEFAULT_AUTO_FIELD="django.db.models.BigAutoField")
django.setup()
from django.core.management import call_command
call_command("makemigrations", "shopapp", verbosity=0)
if os.environ.get("MODE") == "test":  # Django's test runner: creates test_swsdb, migrates it, drops it
    call_command("test", "shopapp", interactive=False, verbosity=1)
    raise SystemExit(0)
call_command("migrate", verbosity=0)
from django.contrib.auth.models import User
from shopapp.models import Order
u = User.objects.create_user("ana", password="pw-for-a-test-user")
Order.objects.create(customer=u, item="kettle", qty=2)
Order.objects.create(customer=u, item="lamp", qty=1)
print(json.dumps({"orders": Order.objects.filter(customer__username="ana").count(),
                  "qty": sum(o.qty for o in Order.objects.all())}))
"""

DJANGO_MODELS = """
from django.conf import settings
from django.db import models

class Order(models.Model):
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    item = models.CharField(max_length=100, db_index=True)
    qty = models.PositiveIntegerField()
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["customer", "item"], name="one_line_per_item")]
"""


DJANGO_TESTS = """
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from .models import Order

class OrderTests(TestCase):
    def test_one_line_per_item(self):
        u = User.objects.create_user("t")
        Order.objects.create(customer=u, item="a", qty=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(customer=u, item="a", qty=2)
        self.assertEqual(Order.objects.count(), 1)
"""


def django(tmp_path: Path, url: str, mode: str = "app") -> subprocess.CompletedProcess:
    app = tmp_path / "shopapp"
    (app / "migrations").mkdir(parents=True, exist_ok=True)
    (app / "__init__.py").write_text("")
    (app / "migrations" / "__init__.py").write_text("")
    (app / "models.py").write_text(DJANGO_MODELS)
    (app / "tests.py").write_text(DJANGO_TESTS)
    script = tmp_path / "run.py"
    script.write_text(DJANGO_APP)
    p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=300, check=False,
                       env={**os.environ, "DATABASE_URL": url, "APP_DIR": str(tmp_path), "MODE": mode})
    assert p.returncode == 0, p.stdout[-2000:] + p.stderr[-3000:]
    return p


def test_django_migrates_and_its_orm_works(box, tmp_path):
    """Django's own migrations (auth, sessions, contenttypes) and an app's, then the ORM, on a project login."""
    cred = ok(api(box, tenant="djangoshop").post("/v1/postgres/credentials"), 201)
    p = django(tmp_path, cred["url"])
    assert json.loads(p.stdout.strip().splitlines()[-1]) == {"orders": 2, "qty": 3}
    # the tables are the project's: swsdb's SQL API sees them, and they're owned by the project's role
    tables = ok(api(box, tenant="djangoshop").post("/v1/sql", json={
        "query": "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = current_schema() ORDER BY 1",
        "read_only": True}))["rows"]
    assert {"auth_user", "django_migrations", "django_session", "shopapp_order"} <= {r[0] for r in tables}
    assert {r[1] for r in tables} == {"swsdb_t_djangoshop"}


def test_djangos_test_runner_makes_and_drops_its_test_database(box, tmp_path):
    cred = ok(api(box, tenant="djangotests").post("/v1/postgres/credentials"), 201)
    p = django(tmp_path, cred["url"], mode="test")
    assert "Ran 1 test" in p.stderr and "OK" in p.stderr
    # the test database was the project's own, and it's gone again
    assert ok(api(box, tenant="djangotests").get("/v1/postgres"))["databases"] == []


PRISMA7 = {
    "package.json": '{"type": "module", "private": true}',
    "prisma.config.ts": """
        import { defineConfig, env } from "prisma/config";
        export default defineConfig({
          schema: "prisma/schema.prisma",
          migrations: { path: "prisma/migrations" },
          datasource: { url: env("DATABASE_URL") },
        });
    """,
    "prisma/schema.prisma": """
        generator client {
          provider = "prisma-client"
          output   = "../generated/prisma"
        }
        datasource db {
          provider = "postgresql"
        }
        model Order {
          id        Int      @id @default(autoincrement())
          item      String   @unique
          qty       Int
          createdAt DateTime @default(now())
        }
    """,
    "prisma/migrations/migration_lock.toml": 'provider = "postgresql"',
    "prisma/migrations/20260101000000_init/migration.sql": """
        CREATE TABLE "Order" (
            "id" SERIAL NOT NULL,
            "item" TEXT NOT NULL,
            "qty" INTEGER NOT NULL,
            "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "Order_pkey" PRIMARY KEY ("id")
        );
        CREATE UNIQUE INDEX "Order_item_key" ON "Order"("item");
    """,
    "prisma/customer.part": """
        model Customer {
          id    Int    @id @default(autoincrement())
          email String @unique
        }
    """,
    "app.ts": """
        import { PrismaClient } from "./generated/prisma/client";
        import { PrismaPg } from "@prisma/adapter-pg";
        const adapter = new PrismaPg({ connectionString: process.env.DATABASE_URL }, { schema: process.env.DB_SCHEMA });
        const prisma = new PrismaClient({ adapter });
        await prisma.order.create({ data: { item: "kettle", qty: 2 } });
        await prisma.order.upsert({ where: { item: "lamp" }, create: { item: "lamp", qty: 1 }, update: {} });
        const total = await prisma.order.aggregate({ _sum: { qty: true } });
        console.log("RESULT " + JSON.stringify({ count: await prisma.order.count(), qty: total._sum.qty }));
        await prisma.$disconnect();
    """,
}

PRISMA6 = {
    "package.json": '{"private": true}',
    "prisma/schema.prisma": """
        generator client {
          provider = "prisma-client-js"
        }
        datasource db {
          provider = "postgresql"
          url      = env("DATABASE_URL")
        }
        model Note {
          id    Int      @id @default(autoincrement())
          title String
          tags  String[]
        }
    """,
    "app.mjs": """
        import { PrismaClient } from "@prisma/client";
        const prisma = new PrismaClient();
        await prisma.note.create({ data: { title: "first", tags: ["a", "b"] } });
        console.log("RESULT " + JSON.stringify({ count: await prisma.note.count() }));
        await prisma.$disconnect();
    """,
}

QDRANT_JS = {
    "package.json": '{"type": "module", "private": true}',
    "app.mjs": """
        import { QdrantClient } from "@qdrant/js-client-rest";
        const q = new QdrantClient({ url: process.env.SWSDB_URL, prefix: "qdrant", apiKey: process.env.SWSDB_KEY,
                                     checkCompatibility: false });
        await q.createCollection("js_items", { vectors: { size: 3, distance: "Cosine" } });
        await q.upsert("js_items", { wait: true, points: [
          { id: 1, vector: [1, 0, 0], payload: { name: "one" } },
          { id: 2, vector: [0, 1, 0], payload: { name: "two" } }] });
        const hits = await q.query("js_items", { query: [0.9, 0.1, 0], limit: 1, with_payload: true });
        const names = (await q.getCollections()).collections.map((c) => c.name).sort();
        console.log("RESULT " + JSON.stringify({ top: hits.points[0].payload.name, collections: names }));
    """,
}


def write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(body).strip() + "\n")


@pytest.fixture(scope="module")
def node(box):
    """A Node container on swsdb's network, with openssl for Prisma's schema engine."""
    name = f"{box.net}-node"
    sh("docker", "run", "-d", "--name", name, "--network", box.net, "node:22-bookworm-slim", "sleep", "3600")
    try:
        sh("docker", "exec", name, "sh", "-c",
           "apt-get update -qq && apt-get install -y -qq openssl ca-certificates >/dev/null", timeout=300)
        yield name
    finally:
        sh("docker", "rm", "-f", name, check=False)


def run_node(node: str, tmp_path: Path, files: dict[str, str], env: dict[str, str], *steps: str) -> dict:
    """Copy a small project into the Node container, run its steps, and return what it printed after RESULT."""
    src = tmp_path / "app"
    write_tree(src, files)
    target = f"/work/{tmp_path.name}"
    sh("docker", "exec", node, "mkdir", "-p", "/work")
    sh("docker", "cp", str(src), f"{node}:{target}")
    envs = [a for k, v in env.items() for a in ("-e", f"{k}={v}")]
    out = ""
    for step in steps:  # Node tools print UTF-8 symbols: read them as such on any host
        p = subprocess.run(["docker", "exec", "-w", target, *envs, node, "sh", "-c", step], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=600, check=False)
        assert p.returncode == 0, f"{step} failed ({p.returncode}): {p.stdout[-2000:]}{p.stderr[-2000:]}"
        out = p.stdout + p.stderr
    line = next(ln for ln in out.splitlines() if ln.startswith("RESULT "))
    return json.loads(line[len("RESULT "):])


def in_network(box: Box, url: str) -> str:
    """The connection string as another container on the network uses it: the swsdb container's name, port 5432."""
    return url.replace(f"@127.0.0.1:{box.pg_port}/", f"@{box.name}:5432/")


def test_prisma_7_migrates_and_its_client_works(box, node, tmp_path):
    cred = ok(api(box, tenant="prisma7").post("/v1/postgres/credentials"), 201)
    assert cred["prisma_url"] == cred["url"] + "?schema=swsdb_t_prisma7"
    got = run_node(node, tmp_path, PRISMA7, {"DATABASE_URL": in_network(box, cred["prisma_url"]),
                                             "DB_SCHEMA": cred["schema"]},
                   "npm install --silent --no-audit --no-fund prisma@7 @prisma/client@7 @prisma/adapter-pg@7 tsx",
                   "npx prisma migrate deploy",
                   "npx prisma migrate status",  # the migrations and the database agree
                   "npx prisma db pull --print | grep -q 'model Order'",  # introspection reads the catalog
                   # migrate dev: a new model, diffed in a shadow database Prisma creates and drops ("script" gives
                   # it the terminal it insists on)
                   "cat prisma/customer.part >> prisma/schema.prisma",
                   'script -qec "npx prisma migrate dev --name add_customer" /dev/null',
                   "ls prisma/migrations | grep -q add_customer",
                   "npx prisma migrate status",
                   "npx prisma generate",
                   "npx tsx app.ts")
    assert got == {"count": 2, "qty": 3}
    owners = ok(api(box, tenant="prisma7").post("/v1/sql", json={
        "query": "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = current_schema()",
        "read_only": True}))["rows"]
    assert owners == [["swsdb_t_prisma7"]]
    tables = ok(api(box, tenant="prisma7").post("/v1/sql", json={
        "query": "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema() AND tablename = 'Customer'",
        "read_only": True}))["rows"]
    assert tables == [[1]]
    assert ok(api(box, tenant="prisma7").get("/v1/postgres"))["databases"] == []  # the shadow database is gone


def test_prisma_6_db_push_and_its_client_work(box, node, tmp_path):
    cred = ok(api(box, tenant="prisma6").post("/v1/postgres/credentials"), 201)
    got = run_node(node, tmp_path, PRISMA6, {"DATABASE_URL": in_network(box, cred["prisma_url"])},
                   "npm install --silent --no-audit --no-fund prisma@6 @prisma/client@6",
                   "npx prisma db push",
                   "node app.mjs")
    assert got == {"count": 1}
    rows = ok(api(box, tenant="prisma6").post("/v1/sql", json={
        "query": 'SELECT title, tags FROM "Note"', "read_only": True}))["rows"]
    assert rows == [["first", ["a", "b"]]]


def test_the_typescript_qdrant_client_works(box, keys, node, tmp_path):
    got = run_node(node, tmp_path, QDRANT_JS, {"SWSDB_URL": f"http://{box.name}:6464", "SWSDB_KEY": keys["shop"]},
                   "npm install --silent --no-audit --no-fund @qdrant/js-client-rest",
                   "node app.mjs")
    assert got["top"] == "one"
    # exactly the project's own vector collections: the same list swsdb's API gives
    own = sorted(c["name"] for c in ok(api(box, keys["shop"]).get("/v1/collections"))["collections"] if c["vector"])
    assert got["collections"] == own and "js_items" in own


# ------------------------------------------------------------------ backups


def test_a_snapshot_restored_on_a_new_server_keeps_connection_strings(box, tmp_path):
    """A project's DATABASE_URL keeps working after its data moves to another machine (only the port differs here,
    because both test servers run on one host)."""
    cred = ok(api(box, tenant="moved").post("/v1/postgres/credentials"), 201)
    with connect(cred["url"]) as conn:
        conn.execute("CREATE TABLE kept (note text)")
        conn.execute("INSERT INTO kept VALUES ('still here')")
    src = SwsDB(f"http://127.0.0.1:{box.port}", api_key=ADMIN, timeout=600)
    snap = src.create_snapshot("move")
    path = src.download_snapshot(snap["id"], tmp_path / "move.tar")

    vol = f"swsdb-projects-{uuid.uuid4().hex[:8]}"
    pg = free_port()
    new = Box(vol, {"SWSDB_API_KEY": ADMIN, "SWSDB_POSTGRES_PUBLIC_PORT": str(pg)},
              extra=["-p", f"127.0.0.1:{pg}:5432"]).start()
    try:
        new.wait_ready()
        dst = SwsDB(f"http://127.0.0.1:{new.port}", api_key=ADMIN, timeout=600)
        dst.upload_snapshot(path)
        dst.restore_snapshot("move", backup_first=False)
        with connect(cred["url"].replace(f":{box.pg_port}/", f":{pg}/")) as conn:
            assert conn.execute("SELECT note FROM kept").fetchone() == ("still here",)
            assert conn.execute("SELECT current_user").fetchone() == ("swsdb_t_moved",)
        assert ok(api(new, tenant="moved").get("/v1/postgres"))["issued"] is True
    finally:
        new.remove()
        sh("docker", "volume", "rm", "-f", vol, check=False)
