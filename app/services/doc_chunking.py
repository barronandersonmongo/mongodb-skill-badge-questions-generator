"""Split a stored documentation page into the sections questions are written from.

A page was the wrong unit. The corpus holds pages up to 1.7 MB — driver tutorials
that repeat every example in a dozen languages — and one of those sent whole cost
$2.58 for three questions. Capping what a page contributes fixed the cost and
created a worse problem: everything past the cap became unreachable, so a page's
later material could never produce a question however many times a badge was walked.

So pages are split into sections, and a section is the unit that is embedded,
retrieved and written from. Retrieval gets sharper as a side effect: a section about
`$search` stops being buried inside a page about aggregation.

Heading-based recursive split, chosen against this corpus rather than by intuition.
Measured on 2026-08-19 over the 3,844 non-reference pages:

- Splitting at H1-H3 yields 40,561 sections, median 642 characters, with 47% under
  500. Sections are mostly *small*, so merging is the dominant operation, not
  splitting — a naive "split on headings" corpus would be full of heading stubs that
  embed poorly and support no question at all.
- Packing adjacent sections up to a ceiling and merging anything under a floor, at
  1,500/8,000, gives 18,421 chunks: median 2,133 characters, p90 7,603, nothing over
  the ceiling, 3.8% under 500. About 530 tokens for a typical chunk — enough to write
  several distinct questions from, small enough that no single call is expensive.
- The tail needs a paragraph pass: a handful of sections are hundreds of kilobytes
  because the heading structure gives out inside a giant code block or table. Those
  are cut on blank lines, and a single paragraph that is still too big is cut hard.

Every chunk keeps enough metadata to say where it came from and what it is about:
its page and that page's title, the heading it sits under and the full heading path
above it, its position in the page, and its size. The heading path is prepended to
the text that gets embedded — a section titled "Limitations" means nothing on its
own, and everything under "Atlas Vector Search > Filtering > Limitations".
"""

import hashlib
import re
from typing import Any

from app.config import Settings, get_settings

# Markdown ATX headings only. The corpus is generated Markdown and uses them
# consistently; setext underlining does not appear.
HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*$", re.MULTILINE)


def _sections(text: str, max_level: int) -> list[dict[str, Any]]:
    """The page cut at every heading up to `max_level`, in order.

    Any text before the first heading is kept as a section of its own: it is the
    page's opening, which is often the only prose that says what the page is for.
    """
    marks = [
        (m.start(), len(m.group(1)), m.group(2).strip())
        for m in HEADING.finditer(text)
        if len(m.group(1)) <= max_level
    ]
    if not marks:
        return [{"heading": None, "level": 0, "text": text}]

    out: list[dict[str, Any]] = []
    if marks[0][0] > 0:
        out.append({"heading": None, "level": 0, "text": text[: marks[0][0]]})
    for index, (start, level, heading) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        out.append({"heading": heading, "level": level, "text": text[start:end]})
    return out


def _heading_path(sections: list[dict[str, Any]], upto: int) -> list[str]:
    """The headings above this section, outermost first.

    Walks backwards for the nearest heading at each shallower level. "Limitations"
    is meaningless alone and clear as "Atlas Vector Search > Filtering >
    Limitations", so this is what makes a section self-describing once it has been
    cut away from its page.
    """
    level = sections[upto].get("level") or 0
    if not level:
        return []
    path: list[str] = []
    wanted = level - 1
    for candidate in reversed(sections[:upto]):
        candidate_level = candidate.get("level") or 0
        if candidate_level and candidate_level <= wanted and candidate.get("heading"):
            path.append(candidate["heading"])
            wanted = candidate_level - 1
            if wanted == 0:
                break
    return list(reversed(path))


def _split_oversize(text: str, ceiling: int) -> list[str]:
    """Cut a section that is over the ceiling, on blank lines where possible.

    Needed for the tail: a few sections run to hundreds of kilobytes because the
    heading structure gives out inside one enormous code block or table. Paragraph
    boundaries are tried first; a single paragraph still over the ceiling is cut
    bluntly, because the alternative is sending it whole and that is the bug this
    module exists to fix.
    """
    if len(text) <= ceiling:
        return [text]
    parts: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if current and len(current) + len(paragraph) + 2 > ceiling:
            parts.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        parts.append(current)

    out: list[str] = []
    for part in parts:
        while len(part) > ceiling:
            out.append(part[:ceiling])
            part = part[ceiling:]
        if part:
            out.append(part)
    return out


def anchor_for(heading: str | None) -> str:
    """A GitHub-style anchor slug for a heading, or "" if there is none.

    Used to identify a chunk within its page and to link to roughly the right place
    in the rendered source. Approximate by nature — the anchor MongoDB's site
    generates is its business — so nothing depends on it resolving.
    """
    if not heading:
        return ""
    slug = re.sub(r"[^\w\s-]", "", heading.lower())
    return re.sub(r"[\s_]+", "-", slug).strip("-")


def chunk_id_for(url: str, ordinal: int) -> str:
    """A stable identity for a chunk: its page and its position in it.

    Position rather than content, so re-chunking an unchanged page produces the same
    ids and the questions written from it stay attributable. Hashed so the id is a
    fixed length whatever the URL.
    """
    digest = hashlib.sha256(f"{url}#{ordinal}".encode("utf-8")).hexdigest()
    return digest[:24]


def split_page(
    page: dict[str, Any], *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """One stored page as the chunks it should be retrieved and written from.

    Three passes, in this order: cut at headings, cut anything still over the
    ceiling, then pack neighbours together until each chunk clears the floor. The
    packing pass is the important one — nearly half of all heading sections are under
    500 characters, and a corpus of heading stubs would embed badly and support no
    questions.
    """
    settings = settings or get_settings()
    text = page.get("text") or ""
    url = page.get("url") or ""
    if not text.strip() or not url:
        return []

    sections = _sections(text, settings.chunk_heading_depth)
    # Each piece keeps the heading it came from, so packing can record what it spans.
    pieces: list[dict[str, Any]] = []
    for index, section in enumerate(sections):
        path = _heading_path(sections, index)
        for part in _split_oversize(section["text"], settings.chunk_ceiling_chars):
            pieces.append(
                {
                    "heading": section["heading"],
                    "level": section["level"],
                    "heading_path": path,
                    "text": part,
                }
            )

    packed: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for piece in pieces:
        if current is None:
            current = {**piece, "text": piece["text"]}
            continue
        combined = len(current["text"]) + len(piece["text"]) + 2
        if combined > settings.chunk_ceiling_chars:
            packed.append(current)
            current = {**piece}
        elif len(current["text"]) < settings.chunk_floor_chars:
            # Absorbed into the chunk before it. The earlier heading stays the
            # chunk's own, since it is the broader one and describes the whole.
            current["text"] = f"{current['text']}\n\n{piece['text']}"
        else:
            packed.append(current)
            current = {**piece}
    if current is not None:
        packed.append(current)

    return [
        _as_chunk(page, chunk, ordinal, settings)
        for ordinal, chunk in enumerate(packed)
        if chunk["text"].strip()
    ]


def _as_chunk(
    page: dict[str, Any], chunk: dict[str, Any], ordinal: int, settings: Settings
) -> dict[str, Any]:
    """One chunk as it is stored, with the metadata that says what it is."""
    url = page["url"]
    heading = chunk.get("heading")
    path = list(chunk.get("heading_path") or [])
    text = chunk["text"]
    return {
        "chunk_id": chunk_id_for(url, ordinal),
        "url": url,
        "anchor": anchor_for(heading),
        # Where in the corpus this came from, carried so a chunk can be traced back
        # without joining to the page.
        "source": page.get("source"),
        "page_title": page.get("title"),
        "heading": heading,
        "heading_path": path,
        "heading_level": chunk.get("level") or 0,
        "ordinal": ordinal,
        "text": text,
        # What gets embedded and what the model reads: the heading path first, so a
        # section called "Limitations" carries the subject it limits.
        "embed_text": _embed_text(page, heading, path, text),
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _embed_text(
    page: dict[str, Any], heading: str | None, path: list[str], text: str
) -> str:
    """The chunk with its context in front of it.

    A chunk is embedded and read out of context by definition, so the context has to
    travel inside it. Page title, then the heading path, then the text.
    """
    trail = [page.get("title") or "", *path]
    if heading and heading not in trail:
        trail.append(heading)
    label = " > ".join(part for part in trail if part)
    return f"{label}\n\n{text}" if label else text
