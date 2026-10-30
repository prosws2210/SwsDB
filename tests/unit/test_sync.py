"""How sync decides between retrying and giving up."""

import httpx
import psycopg
import pytest

from swsdb_server.embed import EmbeddingError
from swsdb_server.extract import ExtractionError
from swsdb_server.stores import StoreError, Unavailable
from swsdb_server.sync import MAX_BACKOFF_S, Permanent, backoff, transient


@pytest.mark.parametrize("e", [
    Unavailable("qdrant down"), httpx.ConnectError("refused"), psycopg.OperationalError("server closed"),
    EmbeddingError("endpoint 503"), ExtractionError("llm timeout"), ConnectionResetError(), TimeoutError(),
    StoreError("LOADING Redis is loading the dataset in memory"),
])
def test_engine_trouble_is_retried(e):
    assert transient(e)


@pytest.mark.parametrize("e", [Permanent("no text"), StoreError("vector dimension error"), ValueError("bad"),
                               FileNotFoundError("gone")])
def test_problems_with_the_document_are_not(e):
    assert not transient(e)


def test_backoff_grows_and_is_capped():
    waits = [backoff(n) for n in range(1, 20)]
    assert waits[:4] == [2, 4, 8, 16]
    assert waits == sorted(waits) and max(waits) == MAX_BACKOFF_S
