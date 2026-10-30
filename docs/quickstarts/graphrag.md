# GraphRAG: questions that span documents

Some questions can't be answered from any single passage:

> *Which office does the lead of Project Orion work from?*

One document says Priya Nair leads Project Orion. Another says Priya Nair works from the Lisbon office, but never
mentions Orion. Similarity search finds the first and has no reason to find the second. swsdb's graph does: both
passages mention the entity *Priya Nair*.

## What the graph holds

Every documents collection with the graph engine gets one graph:

```text
(:Document)-[:HAS_CHUNK]->(:Chunk)-[:NEXT]->(:Chunk)
(:Chunk)-[:MENTIONS]->(:Entity {name, type})-[:RELATED {type}]-(:Entity)
```

Documents and passages are always there. Entities come from one of two places:

- **Names (the default with no model).** swsdb links capitalised names of two or more words: *Priya Nair*,
  *Project Orion*, *Northwind Analytics*. Sentence-starting words are skipped. This works offline, costs nothing, and
  is enough to connect passages that talk about the same people, projects and companies.
- **A chat model.** It extracts typed entities (person, organization, product, ...) and the relations the text states
  (`Priya Nair WORKS_AT Northwind Analytics`), which `graph_hops=2` follows:

```bash
docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data \
  -e SWSDB_LLM_URL=https://api.openai.com/v1 -e SWSDB_LLM_MODEL=gpt-4.1-mini \
  -e SWSDB_LLM_API_KEY_FILE=/run/secrets/llm -v ./llm-key:/run/secrets/llm:ro \
  ghcr.io/prosws2210/swsdb:latest
```

Any OpenAI-compatible endpoint works (OpenAI, Azure OpenAI, vLLM, Ollama, LM Studio). `SWSDB_ENTITIES` picks
explicitly: `auto` (the model if one is set, else names), `llm`, `names` or `off`. Extraction results are stored in
Postgres, so the graph can be rebuilt (by `swsdb doctor --repair`, or after a restore) without calling the model again.

## Querying with the graph

```python
from swsdb import SwsDB

kb = SwsDB().collection("kestrel")
hits = kb.query("Which office does the lead of Project Orion work from?", k=2, graph_hops=1, graph_k=3)
for h in hits:
    print(h["via"], h["document"], h.get("graph_path"))
```
```text
hybrid project-orion None
hybrid person-amara-okafor None
graph person-priya-nair ['mentions Priya Nair']
graph project-helix ['mentions Amara Okafor']
```

- `graph_hops=1` adds passages that mention the same entities as the top hits, plus neighbouring passages.
- `graph_hops=2` also follows extracted relations (`Priya Nair WORKS_AT Northwind Analytics`).
- `graph_k` is how many graph passages may be added after the `k` search hits.
- Leaving `graph_hops` out uses 1 when the collection has a graph.

Graph passages pass the same permission filter as search hits: a passage the caller may not read is never added.

## Does it help on your data?

[`examples/graphrag/compare.py`](https://github.com/prosws2210/SwsDB/blob/main/examples/graphrag/compare.py) runs the
same questions with and without the graph:

```bash
python examples/graphrag/compare.py
```
```text
7 questions, 2 search hits each, 15 entities in the graph

configuration             recall     MRR  answer found  passages   p50 ms
hybrid                     0.786   1.000         0.571       2.0     23.6
hybrid+graph (1 hop)       0.929   1.000         0.857       3.3     36.2
hybrid+graph (2 hops)      0.929   1.000         0.857       3.3     38.9
```

(With names only, there are no relations, so 2 hops adds nothing over 1. A chat model's relations change that.)

The graph adds passages, so compare at an equal number of passages too. On the handbook, one search hit plus one graph
passage finds the answer to 71% of the multi-hop questions, against 43–57% for two search hits. See
[Benchmarks](../benchmarks.md#retrieval-quality).
For your own questions, put them in a [golden set](../guides/evaluation.md) and compare the configurations in the
console.

## Exploring the graph

The console's **Graph** page lists entities by how often they're mentioned. It shows each one's relations and
co-mentions, and the documents that mention it. For anything else, Cypher is a call away (read-only queries work with
the read-only key on raw graphs; on documents collections Cypher needs the admin key because it would skip document
permissions):

```python
kb.cypher("MATCH (e:Entity)<-[:MENTIONS]-(c:Chunk) RETURN e.name, count(c) AS n ORDER BY n DESC LIMIT 10",
          read_only=True)
```
