"""Configuration and safety rules that run before any engine starts."""

import json

import pytest

from swsdb_server import DATA_FORMAT
from swsdb_server.bootstrap import (
    BootstrapError,
    check_layout,
    check_pg_major,
    new_secrets,
    render_pg_conf,
    render_pg_hba,
    render_qdrant_config,
    render_redis_conf,
)
from swsdb_server.engines import EngineSelectionError, parse_engines
from swsdb_server.settings import Settings


@pytest.mark.parametrize(("value", "expected"), [
    (None, ("sql", "vector", "graph")),
    ("", ("sql", "vector", "graph")),
    ("all", ("sql", "vector", "graph")),
    ("vector", ("vector",)),
    ("graph, qdrant", ("vector", "graph")),  # stable order whatever the input order
    ("POSTGRES;FalkorDB", ("sql", "graph")),
    ("vector vector", ("vector",)),
])
def test_engine_selection(value, expected):
    assert parse_engines(value) == expected


def test_unknown_engines_are_an_error_not_silently_dropped():
    with pytest.raises(EngineSelectionError, match="'mongo'"):
        parse_engines("vector,mongo")


def s(**kw) -> Settings:
    return Settings(**kw)


def test_engines_listen_on_localhost_unless_exposed():
    secret = new_secrets()
    local, exposed = s(), s(expose_native=True)
    assert "listen_addresses = '127.0.0.1'" in render_pg_conf(local)
    assert "listen_addresses = '*'" in render_pg_conf(exposed)
    assert "0.0.0.0/0" not in render_pg_hba(local) and "0.0.0.0/0" in render_pg_hba(exposed)
    assert json.loads(render_qdrant_config(local, secret))["service"]["host"] == "127.0.0.1"
    assert json.loads(render_qdrant_config(exposed, secret))["service"]["host"] == "0.0.0.0"
    assert "bind 127.0.0.1 -::1" in render_redis_conf(local, secret)


def test_every_engine_requires_its_secret_and_qdrant_telemetry_is_off():
    secret = new_secrets()
    q = json.loads(render_qdrant_config(s(), secret))
    assert q["service"]["api_key"] == secret["qdrant_api_key"] and q["telemetry_disabled"] is True
    assert f'requirepass "{secret["falkordb_password"]}"' in render_redis_conf(s(), secret)
    hba = render_pg_hba(s(expose_native=True))
    assert "trust" not in hba and hba.count("scram-sha-256") == 4
    assert len({*secret.values()}) == 3 and all(len(v) >= 40 for v in secret.values())


def test_engine_data_lives_on_the_volume():
    st = s(data_dir="/data")
    q = json.loads(render_qdrant_config(st, new_secrets()))
    assert q["storage"]["storage_path"].replace("\\", "/") == "/data/qdrant/storage"
    assert "appendonly yes" in render_redis_conf(st, new_secrets())


def test_newer_data_is_refused_and_older_or_new_is_accepted():
    check_layout(None)
    check_layout({"format": DATA_FORMAT})
    with pytest.raises(BootstrapError, match="newer than this image"):
        check_layout({"format": DATA_FORMAT + 1, "written_by": "9.9.9"})


def test_postgres_data_from_another_major_is_refused(tmp_path):
    (tmp_path / "PG_VERSION").write_text("16\n")
    with pytest.raises(BootstrapError, match="PostgreSQL 16"):
        check_pg_major(tmp_path)
    (tmp_path / "PG_VERSION").write_text("17\n")
    check_pg_major(tmp_path)
