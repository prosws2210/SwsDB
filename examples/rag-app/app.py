"""A question-answering app over your documents, with citations, in one file.

    pip install swsdb anthropic          # anthropic is optional
    python app.py --ingest ../handbook   # once: add a folder of documents
    python app.py                        # then ask questions

Retrieval (hybrid search, graph expansion, document permissions) is swsdb's. The answer is written by Claude when
ANTHROPIC_API_KEY is set, otherwise by swsdb's own /answer endpoint (extractive without a model on the server).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

from swsdb import SwsDB, SwsDBError

SYSTEM = """Answer the question using only the numbered passages. Cite every statement with its passage number in
square brackets, like [1] or [2][3]. If the passages don't contain the answer, say so. Be brief."""


def passages_block(hits: list[dict]) -> str:
    blocks = []
    for n, h in enumerate(hits, 1):
        where = h["title"] or h["document"]
        if h.get("page") is not None:
            where += f", page {h['page']}"
        blocks.append(f"[{n}] ({where})\n{h['text']}")
    return "\n\n".join(blocks)


def ask_claude(question: str, hits: list[dict]) -> str:
    import anthropic

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    msg = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=1024,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Passages:\n\n{passages_block(hits)}\n\nQuestion: {question}"}],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


def answer(db: SwsDB, collection: str, question: str, who: list[str] | None) -> None:
    col = db.collection(collection)
    if os.environ.get("ANTHROPIC_API_KEY"):
        hits = col.query(question, k=6, principals=who)
        text = ask_claude(question, hits) if hits else "Nothing in the documents matches that."
        cited = sorted({int(n) for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= len(hits)})
        sources = [(n, hits[n - 1]) for n in cited]
    else:
        r = col.answer(question, k=5, principals=who)
        text = r["answer"]
        sources = [(p["n"], p) for p in r["passages"] if p["cited"]]
    print(f"\n{text}\n")
    for n, s in sources:
        page = f", page {s['page']}" if s.get("page") is not None else ""
        print(f"  [{n}] {s['document']}{page}")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="Ask your documents, with citations.")
    p.add_argument("--collection", default="handbook")
    p.add_argument("--ingest", metavar="FOLDER", help="add a folder of documents first")
    p.add_argument("--as", dest="who", help="answer as these principals, e.g. user:42,group:hr (admin key)")
    a = p.parse_args()
    db = SwsDB()  # SWSDB_URL, SWSDB_API_KEY, SWSDB_TENANT
    if a.ingest:
        return subprocess.call([sys.executable, "-m", "swsdb.cli", "ingest", a.collection, a.ingest])
    who = [x.strip() for x in a.who.split(",")] if a.who else None
    print(f"Ask {a.collection!r} anything (empty line to quit).")
    while (q := input("? ").strip()):
        try:
            answer(db, a.collection, q, who)
        except SwsDBError as e:
            print(f"swsdb: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
