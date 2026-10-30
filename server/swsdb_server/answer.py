"""Cited answers: retrieve with the caller's permissions, then answer only from those passages, citing them as [n].

With a chat model configured (SWSDB_LLM_URL / SWSDB_LLM_MODEL, or SWSDB_ANSWER_MODEL to use another model than
entity extraction) the model writes the answer. Without one, swsdb answers extractively: the passages' sentences that
best match the question, each with its citation. Either way every [n] points at a passage the caller may read.
"""

from __future__ import annotations

import asyncio
import re

import httpx

SYSTEM = """You answer questions using only the numbered passages you are given.
Cite every statement with the number of the passage it comes from, in square brackets: [1], or [2][3].
If the passages don't contain the answer, say that the documents don't say, and don't use other knowledge.
Answer in the language of the question, briefly."""

STOP = {"the", "and", "for", "are", "was", "were", "what", "who", "how", "why", "when", "where", "which", "does",
        "did", "can", "with", "from", "this", "that", "have", "has", "our", "you", "your", "about", "into", "they",
        "their", "there", "will", "would", "should", "could", "its", "them", "then", "than", "but", "not", "any",
        "all", "out", "get", "long", "much", "many", "take", "takes", "tell", "know", "need", "use", "used"}
WORD = re.compile(r"[\w'-]{3,}", re.UNICODE)
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
CITE = re.compile(r"\[(\d{1,3})\]")


def terms(text: str) -> set[str]:
    return {w for w in (m.group(0).lower().strip("'-") for m in WORD.finditer(text)) if w not in STOP and len(w) > 2}


def stem(w: str) -> str:
    """A crude stem that maps word forms together: change/changes/changed, price/pricing, approve/approves."""
    for suffix in ("ing", "ies", "es", "ed", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            w = w[: -len(suffix)]
            break
    return w[:-1] if len(w) > 4 and w.endswith("e") else w


def names(text: str) -> set[str]:
    """Capitalised names in a sentence (Priya Nair, Project Orion), to follow a chain from one sentence to the next."""
    from .extract import heuristic_entities  # extract imports nothing from here; keep the module light
    return {e["key"] for e in heuristic_entities(text).entities}


def context(hits: list[dict]) -> str:
    blocks = []
    for n, h in enumerate(hits, 1):
        where = ", ".join(x for x in (h.get("title") or h.get("document"),
                                      f"page {h['page']}" if h.get("page") is not None else None,
                                      h.get("heading")) if x)
        blocks.append(f"[{n}] ({where})\n{(h.get('text') or '').strip()}")
    return "\n\n".join(blocks)


def citations(answer: str, n: int) -> list[int]:
    """The passage numbers an answer cites, in order of first use, ignoring numbers that aren't passages."""
    out: list[int] = []
    for m in CITE.finditer(answer):
        i = int(m.group(1))
        if 1 <= i <= n and i not in out:
            out.append(i)
    return out


def strip_unknown(answer: str, n: int) -> str:
    """Remove citations of passages that don't exist (a model can invent [7] when there were 5)."""
    return CITE.sub(lambda m: m.group(0) if 1 <= int(m.group(1)) <= n else "", answer)


def body(hit: dict) -> str:
    """A passage's prose: Markdown heading markers removed, and the heading itself when it opens the passage (chunks
    start with their section's heading, sometimes run together with the first sentence)."""
    heading = (hit.get("heading") or "").strip()
    out = []
    for line in (hit.get("text") or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            s = s.lstrip("#").strip()
            if heading and s.startswith(heading):
                s = s[len(heading):].strip()
            elif s[-1:] not in ".!?" and len(s.split()) <= 12:  # a heading line on its own
                s = ""
        if s:
            out.append(s)
    return " ".join(out)


def extractive(question: str, hits: list[dict], max_sentences: int = 3) -> str:
    """The sentence that best covers the question, then sentences that add what it doesn't say: each must cover
    question words not covered yet, and either continue a chain (share a name with what is already picked, the way
    the graph links passages) or add at least two new words. Best passages first, each sentence cited."""
    want = {stem(w) for w in terms(question)}
    if not want:
        return ""
    cands: list[tuple[float, int, int, str, set[str]]] = []
    for n, h in enumerate(hits, 1):
        for i, raw in enumerate(SENTENCE.split(body(h))):
            sent = " ".join(raw.split())
            if len(sent) < 12:
                continue
            covers = want & {stem(w) for w in terms(sent)}
            if covers:
                cands.append((len(covers) / len(want) + 0.5 / n, n, i, sent, covers))
    if not cands:
        return ""
    cands.sort(key=lambda c: (-c[0], c[1], c[2]))
    first = cands[0]
    picked = [(first[1], first[2], first[3])]
    covered, linked = set(first[4]), names(first[3])
    for _, n, i, sent, covers in cands[1:]:
        if len(picked) == max_sentences:
            break
        new = covers - covered
        chained = bool(names(sent) & linked)
        if new and (chained or len(new) >= 2):
            picked.append((n, i, sent))
            covered |= covers
            linked |= names(sent)
    picked.sort(key=lambda p: (p[0], p[1]))  # read in passage order
    return " ".join(f"{s.rstrip()} [{n}]" if s.rstrip()[-1:] in ".!?" else f"{s.rstrip()}. [{n}]"
                    for n, _, s in picked)


class AnswerError(RuntimeError):
    pass


class Answerer:
    """Any OpenAI-compatible chat endpoint."""

    def __init__(self, url: str, model: str, api_key: str | None = None):
        self.url, self.model, self.api_key = url.rstrip("/"), model, api_key
        self.http = httpx.AsyncClient(timeout=120)

    async def generate(self, question: str, hits: list[dict], max_tokens: int = 600) -> str:
        headers = {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "temperature": 0, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": f"Passages:\n\n{context(hits)}\n\nQuestion: {question}"}]}
        for attempt in range(3):
            try:
                r = await self.http.post(f"{self.url}/chat/completions", headers=headers, json=body)
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                return (r.json()["choices"][0]["message"]["content"] or "").strip()
            except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                if attempt == 2:
                    raise AnswerError(f"{type(e).__name__}: {e}") from e
                await asyncio.sleep(2 ** attempt)
        raise AnswerError("no answer after retries")

    async def close(self) -> None:
        await self.http.aclose()
