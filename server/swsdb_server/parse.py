"""Turn an uploaded file into text sections (with page and heading), then into chunks for retrieval."""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser

SUPPORTED = {".pdf", ".md", ".markdown", ".txt", ".text", ".html", ".htm", ".csv", ".json", ".rst"}


class UnsupportedDocument(ValueError):
    pass


@dataclass
class Section:
    text: str
    page: int | None = None
    heading: str | None = None  # "Chapter 2 > Refunds"


@dataclass
class Chunk:
    ordinal: int
    text: str
    page: int | None
    heading: str | None
    hash: str


def kind_of(filename: str, mime: str | None = None) -> str:
    name = filename.lower()
    ext = name[name.rfind("."):] if "." in name else ""
    if ext in SUPPORTED:
        return {".markdown": ".md", ".htm": ".html", ".text": ".txt"}.get(ext, ext)
    if mime:
        if "pdf" in mime:
            return ".pdf"
        if "html" in mime:
            return ".html"
        if "markdown" in mime:
            return ".md"
        if mime.startswith("text/"):
            return ".txt"
    raise UnsupportedDocument(f"unsupported file type for {filename!r}: use PDF, Markdown, HTML or plain text")


def parse(data: bytes, filename: str, mime: str | None = None) -> list[Section]:
    kind = kind_of(filename, mime)
    if kind == ".pdf":
        return _pdf(data)
    text = _decode(data)
    if kind == ".html":
        return _markdown(_html_to_text(text))
    if kind == ".md":
        return _markdown(text)
    return [Section(text)]


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _pdf(data: bytes) -> list[Section]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        pages = [Section(page.extract_text() or "", page=i + 1) for i, page in enumerate(reader.pages)]
    except (PdfReadError, ValueError, KeyError) as e:
        raise UnsupportedDocument(f"can't read this PDF: {e}") from e
    if not any(p.text.strip() for p in pages):
        raise UnsupportedDocument("this PDF has no extractable text (scanned images need OCR, not supported yet)")
    return pages


HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _markdown(text: str) -> list[Section]:
    sections: list[Section] = []
    path: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush() -> None:
        if "".join(buf).strip():
            sections.append(Section("\n".join(buf).strip(), heading=" > ".join(h for _, h in path) or None))
        buf.clear()

    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = [p for p in path if p[0] < level] + [(level, m.group(2).strip())]
        buf.append(line)
    flush()
    return sections or [Section(text)]


class _Text(HTMLParser):
    BLOCK = frozenset({"p", "div", "br", "li", "tr", "section", "article", "table", "ul", "ol", "pre", "blockquote"})
    SKIP = frozenset({"script", "style", "noscript", "head", "nav", "footer", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def _html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out))


# ------------------------------------------------------------------ chunking

SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _units(text: str) -> list[str]:
    """Paragraphs, and sentences inside paragraphs that are too long on their own."""
    out = []
    for raw in re.split(r"\n\s*\n", text):
        para = re.sub(r"[ \t]+", " ", raw).strip()
        if not para:
            continue
        out.extend(SENTENCE_END.split(para) if len(para.split()) > 120 else [para])
    return out


def chunk(sections: list[Section], max_words: int = 220, overlap_words: int = 40) -> list[Chunk]:
    """Pack paragraphs into chunks of up to `max_words`, never across a heading or page boundary, with a short overlap
    so an answer split across a boundary is still found. Word counts approximate the embedding model's token limit
    (bge-small reads 512 tokens, roughly 380 words)."""
    chunks: list[Chunk] = []

    def emit(words: list[tuple[int, str]], s: Section) -> None:
        """Words are (paragraph number, word): paragraphs stay separated by a blank line in the chunk's text."""
        parts: list[str] = []
        for i, (para, word) in enumerate(words):
            if i:
                parts.append("\n\n" if para != words[i - 1][0] else " ")
            parts.append(word)
        text = "".join(parts).strip()
        if text:
            h = hashlib.sha256(text.encode()).hexdigest()[:32]
            chunks.append(Chunk(len(chunks), text, s.page, s.heading, h))

    for s in sections:
        cur: list[tuple[int, str]] = []
        fresh = 0  # words added since the last chunk (the overlap tail doesn't count)
        for n, unit in enumerate(_units(s.text)):
            words = [(n, w) for w in unit.split()]
            while words:
                room = max_words - len(cur)
                if len(words) <= room:
                    cur += words
                    fresh += len(words)
                    words = []
                elif fresh and len(words) <= max_words - overlap_words:
                    emit(cur, s)  # end the chunk at a paragraph boundary
                    cur, fresh = cur[-overlap_words:], 0
                else:  # longer than a chunk: fill this one and carry on
                    cur += words[:room]
                    fresh += room
                    words = words[room:]
                    emit(cur, s)
                    cur, fresh = cur[-overlap_words:], 0
        if fresh:
            emit(cur, s)
    return chunks
