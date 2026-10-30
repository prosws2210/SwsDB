# RAG app with citations

A command-line app that answers questions about a folder of documents and cites its sources. The code is
[`examples/rag-app/app.py`](https://github.com/prosws2210/SwsDB/blob/main/examples/rag-app/app.py), about 100 lines.

## Run it

```bash
docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data ghcr.io/prosws2210/swsdb:latest
pip install swsdb
cd examples/rag-app
python app.py --ingest ../handbook     # the sample handbook, or any folder of your own
python app.py
```
```text
Ask 'handbook' anything (empty line to quit).
? how long do refunds take
Refunds are issued to the original payment method within five business days of receiving the returned item. [1]

  [1] refund-policy
```

## How it works

Retrieval is one call. swsdb runs dense and keyword search, fuses them, expands through the graph, and filters by the
caller's permissions inside Qdrant:

```python
from swsdb import SwsDB

db = SwsDB()  # SWSDB_URL, SWSDB_API_KEY, SWSDB_TENANT from the environment
hits = db.collection("handbook").query("How long do refunds take?", k=6)
for h in hits:
    print(h["document"], h["page"], h["score"], h["via"], h["text"][:80])
```

Each hit carries its `document`, `title`, `page`, `heading`, `score`, `via` (`hybrid`, `dense`, `sparse` or `graph`)
and, for graph hits, the `graph_path` that led to it.

There are two ways to write the answer:

=== "swsdb writes it"

    ```python
    r = db.collection("handbook").answer("How long do refunds take?")
    print(r["answer"])                  # "... five business days ... [1]"
    for p in r["passages"]:
        if p["cited"]:
            print(p["n"], p["document"], p["page"])
    ```
    With `SWSDB_LLM_URL` and `SWSDB_LLM_MODEL` set on the server, a model writes the answer from the passages (any
    OpenAI-compatible endpoint). Without them the answer is extractive. Either way, every `[n]` is a passage the caller
    may read; citations of passages that don't exist are removed.

=== "Your own model"

    ```python
    import anthropic

    hits = db.collection("handbook").query(question, k=6)
    passages = "\n\n".join(f"[{n}] ({h['title'] or h['document']})\n{h['text']}" for n, h in enumerate(hits, 1))
    msg = anthropic.Anthropic().messages.create(
        model="claude-opus-5-5", max_tokens=1024,
        system="Answer only from the numbered passages and cite them as [n].",
        messages=[{"role": "user", "content": f"Passages:\n\n{passages}\n\nQuestion: {question}"}],
    )
    ```
    The app does this when `ANTHROPIC_API_KEY` is set.

## Answering as a user

Documents can be restricted when they're added: `readers=["group:hr"]` (the default is everyone in the tenant). Your
backend knows who is asking, so it passes their principals with the admin key:

```python
db.collection("handbook").answer("What is the salary band?", principals=["user:42", "group:hr"])
```

```bash
python app.py --as user:42,group:hr
```

A read-only key can't act for principals: it sees only documents readable by everyone. See
[Documents and retrieval](../guides/documents.md#permissions).
