# Quickstart: a cited answer in 10 minutes

You need Docker (with about 2 GB of memory for the container) and Python 3.10 or newer.

## 1. Start swsdb (2 minutes)

```bash
docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data ghcr.io/prosws2210/swsdb:latest
```

The first start creates the databases on the `swsdb-data` volume. Wait until it reports ready:

```bash
docker exec swsdb swsdb-admin status
```
```text
swsdb: ready
  sql     postgres  ready
  vector  qdrant    ready
  graph   falkordb  ready
```

!!! note "No API key yet"
    Without `SWSDB_API_KEY` the API is open, which is fine on your laptop. In open mode swsdb answers only to
    `localhost`, IP addresses and the container's own name, so a web page can't reach it through DNS rebinding. Set a
    key before exposing the port anywhere else (see [Security](guides/security.md)).

??? tip "Building the image yourself"
    ```bash
    git clone https://github.com/prosws2210/SwsDB && cd SwsDB
    docker build -f docker/Dockerfile -t swsdb/swsdb:dev .
    docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data swsdb/swsdb:dev
    ```

## 2. Install the client (1 minute)

```bash
pip install swsdb
```

## 3. The one-minute tour (2 minutes)

```bash
swsdb demo
```

This loads the handbook of Kestrel Labs, a made-up company (30 short documents), and asks it four questions:

```text
> Which office does the lead of Project Orion work from?
Project Orion is the new search platform, led by Priya Nair. [1] Priya Nair is a principal engineer who works from the Lisbon office. [5]
Sources:
  [1] project-orion (Project Orion)
  [5] person-priya-nair (Priya Nair)
```

Passage 5 was found through the knowledge graph: the question never mentions Priya Nair, but the passage about
Project Orion does, and swsdb links passages that mention the same name. On the laptop these docs were tested on,
everything up to here took under 30 seconds.

## 4. Your own documents (3 minutes)

```bash
swsdb ingest docs ./my-folder          # PDF, Markdown, HTML and text; folders are read recursively
swsdb ask docs "What is our refund policy?"
swsdb search docs "refund policy" -k 5 # the passages alone, with scores
```

`ingest` waits until every document is searchable and prints how many passages each became.

## 5. Look around (2 minutes)

Open <http://localhost:6464/console>:

- **Search** asks your documents and shows the answer with clickable citations, and how each passage was found
  (meaning, keywords or the graph).
- **Collections** shows every document's status in each store, and why anything isn't searchable.
- **Evaluation** compares retrieval configurations on your own questions.

![The console's search page](img/console-search.jpg)

## Better answers with a model

Without a chat model, `ask` answers *extractively*: it gives the sentences that best match your question, each cited.
To have a model write the answer, give swsdb any OpenAI-compatible endpoint. The same model then also extracts typed
entities and relations for the graph, instead of linking names only:

=== "OpenAI"

    ```bash
    docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data \
      -e SWSDB_LLM_URL=https://api.openai.com/v1 -e SWSDB_LLM_MODEL=gpt-4.1-mini \
      -e SWSDB_LLM_API_KEY_FILE=/run/secrets/llm -v ./llm-key:/run/secrets/llm:ro \
      ghcr.io/prosws2210/swsdb:latest
    ```

=== "Ollama (local)"

    ```bash
    docker run -d --name swsdb -p 6464:6464 -v swsdb-data:/swsdb/data \
      -e SWSDB_LLM_URL=http://host.docker.internal:11434/v1 -e SWSDB_LLM_MODEL=llama3.1 \
      ghcr.io/prosws2210/swsdb:latest
    ```

Documents added before the model was configured keep their name-linked graph. Re-add them to have the model
extract entities.

## Next

- Build an app on it: [RAG app with citations](quickstarts/rag-app.md).
- Connect an assistant: [agent memory over MCP](quickstarts/agent-memory.md).
- Before production: [security](guides/security.md) and [snapshots](guides/backups.md).
