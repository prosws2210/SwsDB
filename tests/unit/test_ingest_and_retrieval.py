"""Parsing, chunking, extraction parsing and retrieval filters (no engines needed)."""

import pytest

from swsdb_server.documents import Documents, check_readers, chunk_uuid, doc_uuid
from swsdb_server.extract import entity_key, parse_extraction
from swsdb_server.parse import UnsupportedDocument, chunk, kind_of, parse
from swsdb_server.stores import StoreError


def test_markdown_keeps_heading_paths():
    secs = parse(b"# Guide\n\nIntro.\n\n## Refunds\n\nWithin 5 days.\n\n```\n# not a heading\n```\n", "a.md")
    assert [s.heading for s in secs] == ["Guide", "Guide > Refunds"]
    assert "# not a heading" in secs[1].text


def test_html_drops_scripts_and_keeps_structure():
    html = b"<html><head><title>x</title><script>alert(1)</script></head><body><h1>Policy</h1><p>Refunds in 5 " \
           b"days.</p><nav>menu</nav><h2>Shipping</h2><p>2 days.</p></body></html>"
    secs = parse(html, "p.html")
    text = " ".join(s.text for s in secs)
    assert "alert" not in text and "menu" not in text and "Refunds in 5 days." in text
    assert [s.heading for s in secs] == ["Policy", "Policy > Shipping"]


def test_unsupported_types_are_refused():
    with pytest.raises(UnsupportedDocument):
        kind_of("photo.png")
    assert kind_of("README", "text/markdown") == ".md"


def test_chunks_respect_boundaries_and_overlap():
    words = " ".join(f"w{i}" for i in range(500))
    cs = chunk(parse(f"# A\n\nshort.\n\n# B\n\n{words}".encode(), "x.md"), max_words=200, overlap_words=40)
    assert cs[0].heading == "A" and "short." in cs[0].text
    b = [c for c in cs if c.heading == "B"]
    assert all(len(c.text.split()) <= 200 for c in b)
    assert b[1].text.split()[0] == b[0].text.split()[-40]  # 40 words of overlap
    assert "w499" in b[-1].text
    assert [c.ordinal for c in cs] == list(range(len(cs)))
    assert len({c.hash for c in cs}) == len(cs)


def test_no_chunk_is_only_overlap():
    cs = chunk(parse(" ".join(f"w{i}" for i in range(200)).encode(), "x.txt"), max_words=200, overlap_words=40)
    assert len(cs) == 1


def test_ids_are_deterministic():
    assert doc_uuid("t", "c", "a") == doc_uuid("t", "c", "a") != doc_uuid("t", "c", "b")
    assert doc_uuid("t1", "c", "a") != doc_uuid("t2", "c", "a")
    assert chunk_uuid("d", 1, 0) != chunk_uuid("d", 2, 0)


def test_readers():
    assert check_readers(None) == ["*"]
    assert check_readers(["group:b", "user:1", "user:1"]) == ["group:b", "user:1"]
    for bad in ([], [""], [" x"], [1], "user:1"):
        with pytest.raises(StoreError):
            check_readers(bad)


def test_entity_keys_merge_spellings():
    assert entity_key("Acme Corp.") == entity_key("ACME corp") == entity_key("Acme") == "acme"
    assert entity_key("Café Müller") == "cafe muller"


def test_extraction_output_is_cleaned_not_trusted():
    ex = parse_extraction('Sure! {"entities": [{"name": "Maria Lopez", "type": "Person"}, {"name": "Acme Inc",'
                          ' "type": "Spaceship"}, {"name": "x"}, {"type": "Person"}],'
                          ' "relations": [{"source": "Maria Lopez", "target": "ACME", "type": "works for"},'
                          ' {"source": "Maria Lopez", "target": "Nobody", "type": "KNOWS"}]}')
    assert {e["key"] for e in ex.entities} == {"maria lopez", "acme"}
    assert next(e for e in ex.entities if e["key"] == "acme")["type"] == "Other"
    assert ex.relations == [{"source": "maria lopez", "target": "acme", "type": "WORKS_FOR"}]
    assert parse_extraction("no json here").entities == []
    assert parse_extraction("[1, 2]").entities == []


def test_retrieval_filters_enforce_permissions():
    d = Documents.__new__(Documents)
    live = {"key": "live", "match": {"value": True}}
    assert d._filter(None, "admin", None) == {"must": [live]}  # the backend, acting for nobody: everything
    assert d._filter(None, "read", None)["must"][1] == {"key": "readers", "match": {"any": ["*"]}}
    f = d._filter(["user:1", "group:a"], "admin", {"team": "x", "year": [1, 2], "document": "policy"})
    assert {"key": "readers", "match": {"any": ["*", "group:a", "user:1"]}} in f["must"]
    assert {"key": "meta.team", "match": {"value": "x"}} in f["must"]
    assert {"key": "meta.year", "match": {"any": [1, 2]}} in f["must"]
    assert {"key": "doc", "match": {"value": "policy"}} in f["must"]
    for bad in ({"_x": 1}, {"x": {"$gt": 1}}):
        with pytest.raises(StoreError):
            d._filter(None, "admin", bad)


def test_chunks_keep_paragraph_breaks_and_headings_on_their_own_line():
    from swsdb_server.parse import chunk, parse
    c = chunk(parse(b"# Priya Nair\n\nPriya Nair is a principal engineer.\n\nShe mentors   engineers.", "a.md"))
    assert c[0].text == "# Priya Nair\n\nPriya Nair is a principal engineer.\n\nShe mentors engineers."
    assert c[0].heading == "Priya Nair"


def test_names_link_passages_without_a_model():
    from swsdb_server.extract import heuristic_entities
    text = ("# Project Orion\n\nProject Orion is led by Priya Nair. Before joining Kestrel Labs she worked at "
            "Northwind Analytics. The Lisbon office opens on Monday. Every Pull Request needs review in March.")
    names = {e["name"] for e in heuristic_entities(text).entities}
    assert names == {"Project Orion", "Priya Nair", "Kestrel Labs", "Northwind Analytics", "Pull Request"}
    assert heuristic_entities("The Monday Meeting").entities[0]["name"] == "Monday Meeting"
    assert heuristic_entities("Refunds take five days. Store credit is instant.").entities == []
