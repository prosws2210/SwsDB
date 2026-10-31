"""The pure parts of project access: which Qdrant calls only read, how bodies are pointed at the tenant's own
collections, and the SCRAM verifier swsdb computes for project logins."""

import base64
import hashlib
import hmac

import pytest

from swsdb_server.qdrant_proxy import Refused, is_read, rewrite_body
from swsdb_server.stores import connect_role, scram_verifier


@pytest.mark.parametrize(("method", "rest", "read"), [
    ("GET", [], True),
    ("GET", ["points", "abc"], True),
    ("POST", ["points"], True),  # retrieve by ids
    ("POST", ["points", "query"], True),
    ("POST", ["points", "query", "batch"], True),
    ("POST", ["points", "search", "groups"], True),
    ("POST", ["points", "scroll"], True),
    ("POST", ["points", "count"], True),
    ("POST", ["facet"], True),
    ("PUT", [], False),  # create the collection
    ("DELETE", [], False),
    ("PATCH", [], False),
    ("PUT", ["points"], False),  # upsert
    ("POST", ["points", "delete"], False),
    ("POST", ["points", "payload"], False),
    ("PUT", ["index"], False),
])
def test_reads_and_writes(method, rest, read):
    assert is_read(method, rest) is read


def test_lookups_point_at_the_tenants_own_collections():
    body = {"query": {"recommend": {"positive": [1]}}, "lookup_from": {"collection": "other", "vector": "v"},
            "searches": [{"lookup_from": {"collection": "third"}}], "with_lookup": "people"}
    out = rewrite_body(body, "shop")
    assert out["lookup_from"] == {"collection": "sw_shop__other", "vector": "v"}
    assert out["searches"][0]["lookup_from"]["collection"] == "sw_shop__third"
    assert out["with_lookup"] == "sw_shop__people"
    assert rewrite_body({"with_lookup": {"collection": "p", "with_payload": True}}, "shop")["with_lookup"] == {
        "collection": "sw_shop__p", "with_payload": True}


def test_payloads_are_data_not_names():
    body = {"points": [{"id": 1, "vector": [0.1], "payload": {"lookup_from": {"collection": "x"}}}]}
    assert rewrite_body(body, "shop") == body


def test_copying_another_collection_and_bad_names_are_refused():
    with pytest.raises(Refused):
        rewrite_body({"vectors": {"size": 3, "distance": "Cosine"}, "init_from": {"collection": "x"}}, "shop")
    with pytest.raises(Refused):
        rewrite_body({"lookup_from": {"collection": "../sw_other__x"}}, "shop")


def test_scram_verifier_matches_the_password():
    v = scram_verifier("correct horse")
    head, keys = v.split("$", 1)[1].split("$")
    iterations, salt = head.split(":")
    stored, server = (base64.b64decode(x) for x in keys.split(":"))
    salted = hashlib.pbkdf2_hmac("sha256", b"correct horse", base64.b64decode(salt), int(iterations))
    assert hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest() == stored
    assert hmac.new(salted, b"Server Key", hashlib.sha256).digest() == server
    assert "correct horse" not in v and scram_verifier("correct horse") != v  # salted


def test_connect_roles_fit_and_never_clash():
    assert connect_role("shop") == "swsdb_t_shop__c"
    long = "a" * 63
    assert len(connect_role(long)) <= 63 and connect_role(long).endswith("__c")
