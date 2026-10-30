"""Evaluation metrics and the diagnosis verdict."""

import pytest

from swsdb_server.evaluate import score_case, summarise, verdict


def test_perfect_and_missing_results():
    s = score_case(["a", "a", "b"], ["x", "y", "the refund takes five days"], ["a"], "five days")
    assert s == {"hit": 1.0, "recall": 1.0, "mrr": 1.0, "ndcg": 1.0, "evidence": 3, "answer_found": 1.0}
    s = score_case(["c", "d"], ["", ""], ["a", "b"], "five days")
    assert s["hit"] == s["recall"] == s["mrr"] == s["ndcg"] == s["answer_found"] == 0.0


def test_rank_and_partial_recall():
    s = score_case(["x", "b", "y", "a"], [""] * 4, ["a", "b", "z"], None)
    assert s["recall"] == pytest.approx(2 / 3)
    assert s["mrr"] == 0.5  # first relevant document at rank 2
    assert 0 < s["ndcg"] < 1 and "answer_found" not in s


def test_documents_count_once_whatever_the_number_of_chunks():
    assert score_case(["x", "x", "x", "a"], [""] * 4, ["a"], None)["mrr"] == 0.5


def test_summary():
    out = summarise([{"hit": 1.0, "recall": 1.0, "mrr": 1.0, "ndcg": 1.0, "evidence": 4},
                     {"hit": 0.0, "recall": 0.0, "mrr": 0.0, "ndcg": 0.0, "evidence": 6}], [0.010, 0.030])
    assert out["hit"] == 0.5 and out["evidence"] == 5 and out["latency_p50_ms"] == 30.0


def doc(**kw):
    base = {"status": "ready", "version": 1, "live_version": 1, "chunks": 3, "error": None, "readers": ["*"]}
    return {**base, **kw}


def test_verdicts_explain_why():
    coll = {"graph": True}
    v, r = verdict(doc(), [], {"live": 3, "all_versions": 3}, {"chunks": 3}, coll)
    assert v == "searchable"
    v, r = verdict(doc(status="failed", error="no text"), [], {}, None, coll)
    assert v == "not searchable" and "no text" in r[0]
    v, r = verdict(doc(status="queued", live_version=None), [{"attempts": 2, "last_error": "Qdrant down"}],
                   {"live": 0}, None, coll)
    assert v == "not searchable yet" and any("Qdrant down" in x for x in r)
    v, r = verdict(doc(), [], {"live": 1, "all_versions": 1}, {"chunks": 3}, coll)
    assert v == "not fully searchable" and any("doctor --repair" in x for x in r)
    v, r = verdict(doc(readers=["group:hr"]), [], {"live": 3}, {"chunks": 3}, coll)
    assert v == "searchable" and any("group:hr" in x for x in r)
    v, _ = verdict(doc(status="deleting"), [], {}, None, coll)
    assert v == "not searchable"
