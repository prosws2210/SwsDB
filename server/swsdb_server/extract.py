"""Entities and relations from chunk text, with any OpenAI-compatible chat model (optional).

Without a model configured the graph still holds documents, chunks and their order; with one, chunks also link to the
entities they mention and entities to each other, which is what multi-hop retrieval follows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("swsdb.extract")

PROMPT = """Extract the named entities and the relationships between them from the text.
Entities: specific people, organizations, products, places, projects, policies, technologies, events or documents.
Skip generic words (e.g. "customer", "the company" without a name).
Relationships: only ones the text states, between two extracted entities, as a short UPPER_SNAKE verb phrase
(e.g. WORKS_FOR, ACQUIRED, LOCATED_IN, PART_OF, DEPENDS_ON).
Answer with JSON only:
{"entities": [{"name": "...", "type": "Person|Organization|Product|Place|Project|Policy|Technology|Event|Other"}],
 "relations": [{"source": "...", "target": "...", "type": "..."}]}

Text:
"""

TYPES = {"Person", "Organization", "Product", "Place", "Project", "Policy", "Technology", "Event", "Other"}


@dataclass
class Extraction:
    entities: list[dict] = field(default_factory=list)  # {"key", "name", "type"}
    relations: list[dict] = field(default_factory=list)  # {"source", "target", "type"} as keys


def entity_key(name: str) -> str:
    """One graph node per real-world name: case, accents, punctuation and company suffixes don't split it."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    n = re.sub(r"\b(inc|corp|corporation|ltd|llc|plc|gmbh|co)$", "", n).strip()
    return n[:200]


def parse_extraction(raw: str) -> Extraction:
    """Model output -> clean entities and relations. Anything malformed is dropped, never raised."""
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0) if m else raw)
    except (json.JSONDecodeError, TypeError):
        return Extraction()
    if not isinstance(data, dict):
        return Extraction()
    ents: dict[str, dict] = {}
    for e in data.get("entities") or []:
        if not isinstance(e, dict) or not isinstance(e.get("name"), str):
            continue
        name = e["name"].strip()[:200]
        key = entity_key(name)
        if not key or len(key) < 2:
            continue
        typ = e.get("type") if e.get("type") in TYPES else "Other"
        ents.setdefault(key, {"key": key, "name": name, "type": typ})
    rels, seen = [], set()
    for r in data.get("relations") or []:
        if not isinstance(r, dict):
            continue
        s, t = entity_key(str(r.get("source", ""))), entity_key(str(r.get("target", "")))
        typ = re.sub(r"[^A-Z0-9_]", "", str(r.get("type", "RELATED_TO")).upper().replace(" ", "_"))[:60] or "RELATED_TO"
        if s in ents and t in ents and s != t and (s, t, typ) not in seen:
            seen.add((s, t, typ))
            rels.append({"source": s, "target": t, "type": typ})
    return Extraction(list(ents.values()), rels)


class Extractor:
    def __init__(self, url: str, model: str, api_key: str | None = None, concurrency: int = 4):
        self.url, self.model, self.api_key = url.rstrip("/"), model, api_key
        self.http = httpx.AsyncClient(timeout=120)
        self.sem = asyncio.Semaphore(concurrency)

    @property
    def name(self) -> str:
        return self.model

    async def extract(self, text: str) -> Extraction:
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": PROMPT + text[:6000]}]}
        async with self.sem:
            for attempt in range(3):
                try:
                    r = await self.http.post(f"{self.url}/chat/completions", headers=headers, json=body)
                    if r.status_code == 400 and "response_format" in r.text:  # endpoints without JSON mode
                        body.pop("response_format", None)
                        continue
                    if r.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(2 ** attempt)
                        continue
                    r.raise_for_status()
                    return parse_extraction(r.json()["choices"][0]["message"]["content"] or "")
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                    if attempt == 2:
                        raise ExtractionError(f"entity extraction failed: {type(e).__name__}: {e}") from e
                    await asyncio.sleep(2 ** attempt)
        raise ExtractionError("entity extraction failed after retries")

    async def close(self) -> None:
        await self.http.aclose()


class ExtractionError(RuntimeError):
    pass


# ------------------------------------------------------------------ without a model

NAME = re.compile(r"\b[A-Z][A-Za-z0-9&'-]*(?: [A-Z][A-Za-z0-9&'-]*){1,4}\b")
LEADING = {  # capitalised only because they start a sentence or heading: never the start of a name
    "a", "an", "the", "our", "your", "my", "their", "his", "her", "its", "this", "that", "these", "those", "every",
    "each", "any", "all", "some", "no", "new", "before", "after", "when", "while", "if", "in", "on", "at", "for",
    "from", "with", "to", "by", "of", "and", "but", "or", "as", "it", "we", "you", "they", "he", "she", "there",
    "here", "please", "send", "use", "book", "ask", "see", "read", "note", "since", "until", "during", "about",
}
CALENDAR = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january", "february",
            "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}


def heuristic_entities(text: str) -> Extraction:
    """Names written in capitals, two to five words long ("Priya Nair", "Project Orion", "Northwind Analytics"), with
    sentence-starting words removed. No types, no relations: enough to link passages that mention the same person,
    project or company, with no model and no network."""
    ents: dict[str, dict] = {}
    for line in text.splitlines():
        for m in NAME.finditer(line.lstrip("# ")):
            words = m.group(0).split()
            while words and words[0].lower() in LEADING:
                words = words[1:]
            if len(words) < 2 or all(w.lower() in CALENDAR for w in words):
                continue
            name = " ".join(words)
            key = entity_key(name)
            if len(key) >= 4:
                ents.setdefault(key, {"key": key, "name": name, "type": "Other"})
    return Extraction(list(ents.values()), [])


class NameExtractor:
    """Entity linking without a model (SWSDB_ENTITIES=names, the default when no chat model is configured)."""

    name = "names"

    async def extract(self, text: str) -> Extraction:
        return heuristic_entities(text)

    async def close(self) -> None:
        return None
