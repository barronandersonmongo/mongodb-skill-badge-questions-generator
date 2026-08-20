"""Resolve a skill badge to the documentation sections its questions come from.

Two things live here. `chunk_set_for_badge` enumerates a badge's material — the set
of stored chunks that badge's questions should be written from, which the walk then
reads one at a time. `pages_for_badges` is the older single-prompt selection over
whole pages, kept for the case where a badge resolves to no chunks at all.

Chunks rather than pages, because a page was the wrong unit twice over: sent whole a
1.7 MB page cost $2.58 for three questions, and capped, everything past the cap was
unreachable. A chunk is a section of a page with its heading path attached, so it is
both affordable to send and specific enough that retrieval can tell a section about
`$search` from the aggregation page it happens to live in.

The page set is the important one. Asking a model for a badge's worth of questions
out of one prompt caps it at whatever fits in a request; enumerating the badge's
pages instead turns "write questions for this badge" into a walk over a list, where
each page is read once, is worth several questions, and coverage is a counter rather
than a guess. It also means the cost of a badge arrives when somebody asks for that
badge, not as one sweep of the whole corpus.

Question authoring used to research each run with server-side web search: minutes
of wall clock spent waiting, and no two runs on the same badge reading the same
text. The corpus already holds MongoDB's documentation page by page, so the
material can be selected here and handed to the authoring turn as context.

One search per topic area, not one per badge. A single badge-wide query returns
its top pages clustered on whichever topic embeds closest to the badge
description, and five questions written off one page is exactly the outcome this
tool exists to avoid. Searching each of the badge's categories separately spreads
the material across the syllabus the badge actually claims to cover.

Retrieval is best-effort by design. If the corpus is empty, or its Atlas index is
missing or still building, this returns nothing and authoring falls back to web
search — a badge whose material has not been crawled yet is a reason to research
the slow way, not a reason to refuse to write questions.
"""

import logging
import re
from typing import Any

from app.config import Settings, get_settings
from app.repositories import doc_chunks, doc_pages

logger = logging.getLogger(__name__)


class ChunkSetUnavailable(RuntimeError):
    """A badge's section set could not be resolved, so nothing is known about it.

    Distinct from an empty set on purpose. An empty set means "this badge has no
    material left"; this means "we could not find out" — and the two were the same
    value, so a transient Atlas 503 was reported to the operator as a badge having
    exhausted its documentation, which is a permanent-sounding conclusion drawn from a
    momentary failure. Observed on 2026-08-19: a 503 on one topic query made Search
    Fundamentals, which has 291 sections, report as used up, and the run walked nothing
    for it while still paying for the attempt.
    """


def queries_for_badge(badge: dict[str, Any]) -> list[str]:
    """The searches that cover one badge's syllabus.

    The badge as a whole first — it is what scopes the questions — then each topic
    area on its own. A topic area is queried with the badge name attached because
    "indexes" alone matches the whole corpus, while "Atlas Search indexes" is the
    thing the badge means by it.
    """
    name = (badge.get("name") or badge.get("slug") or "").strip()
    overall = " ".join(part for part in (name, badge.get("description") or "") if part)
    queries = [overall.strip()] if overall.strip() else []
    for category in badge.get("categories") or []:
        category = (category or "").strip()
        if category:
            queries.append(f"{name} {category}".strip())
    return list(dict.fromkeys(queries))


def is_reference_url(url: str, settings: Settings | None = None) -> bool:
    """Whether a URL names reference material rather than something to teach from.

    Roughly half the corpus is parameter lists, CLI synopses and command references.
    A question written from a parameter list tests whether the candidate can look up a
    flag, which is not a skill the badges claim to certify — and it is the failure mode
    the Cluster Reliability page set showed most of.
    """
    settings = settings or get_settings()
    return bool(re.search(settings.doc_reference_url_pattern, url or ""))


def chunk_set_for_badge(
    badge: dict[str, Any],
    *,
    exclude_chunk_ids: set[str] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """The documentation sections this badge's questions are written from, best first.

    Drawn from the same per-topic searches as before, but over chunks: not "the best
    few pages that fit in one prompt" but "the sections that make up this badge's
    material". Candidates are filtered three ways — a similarity floor, so a tag like
    "Cluster IP" cannot drag VPC peering into a reliability badge; the reference-page
    exclusion, since a parameter list tests lookup rather than skill; and any chunk
    already written from, so a walk resumes rather than repeating.

    Returned in relevance order, so a run that walks only part of the set walks the
    most relevant part of it.
    """
    settings = settings or get_settings()
    exclude_chunk_ids = exclude_chunk_ids or set()

    best: dict[str, dict[str, Any]] = {}
    for query in queries_for_badge(badge):
        try:
            found = doc_chunks.search_chunk_refs(
                query, limit=settings.doc_page_set_per_topic, settings=settings
            )
        except Exception as exc:
            # Raised rather than returned as an empty set: the caller cannot tell those
            # apart, and treating one as the other tells the operator a badge is
            # exhausted when the search simply failed.
            logger.warning("Chunk set search failed for %r: %s", query, exc)
            raise ChunkSetUnavailable(
                f"could not resolve this badge's sections: {exc}"
            ) from exc
        for chunk in found:
            chunk_id = chunk.get("chunk_id")
            url = chunk.get("url") or ""
            score = chunk.get("score") or 0.0
            if not chunk_id or chunk_id in exclude_chunk_ids:
                continue
            if score < settings.doc_page_set_score_floor:
                continue
            if is_reference_url(url, settings):
                continue
            # A chunk found by several topic queries keeps its best score: it is as
            # relevant as its strongest match, not its weakest.
            if chunk_id not in best or score > best[chunk_id]["score"]:
                best[chunk_id] = {
                    "chunk_id": chunk_id,
                    "url": url,
                    "anchor": chunk.get("anchor"),
                    "page_title": chunk.get("page_title"),
                    "heading": chunk.get("heading"),
                    "heading_path": chunk.get("heading_path") or [],
                    "source": chunk.get("source"),
                    "chars": chunk.get("chars"),
                    "score": score,
                }

    ranked = sorted(best.values(), key=lambda c: c["score"], reverse=True)
    return _spread_across_pages(ranked, settings)[: settings.doc_page_set_size]


def _spread_across_pages(
    ranked: list[dict[str, Any]], settings: Settings
) -> list[dict[str, Any]]:
    """Reorder a ranked set so consecutive sections come from different pages.

    Measured on the live corpus: the 25 sections a Vector Search Fundamentals run walked
    came from six pages, and 85 of that badge's 252 sections were hard-split slices of
    one 1.7 MB page — the same code sample repeated in a dozen languages under an
    identical heading. Twenty of those 25 sections produced no question at all, while a
    badge whose sections spread across 24 pages produced 72.

    So a page's sections are taken in rounds: the best section from every page, then the
    second best from every page. Pure relevance order is not enough, because one page
    that scores well throughout crowds out every other page in the badge.

    This is the same fairness the old page-level retrieval applied across topic queries.
    It was not carried over when the unit became a section, and this is what that cost.
    """
    by_page: dict[str, list[dict[str, Any]]] = {}
    for chunk in ranked:
        by_page.setdefault(chunk.get("url") or "", []).append(chunk)

    # Pages in order of their best section, so the strongest page still leads.
    pages = sorted(by_page.values(), key=lambda group: group[0]["score"], reverse=True)
    limit = settings.doc_sections_per_page
    spread: list[dict[str, Any]] = []
    depth = 0
    while True:
        took = False
        for group in pages:
            if depth < min(len(group), limit):
                spread.append(group[depth])
                took = True
        if not took:
            break
        depth += 1

    # Anything held back by the per-page limit is appended rather than dropped: it is
    # still this badge's material, and a badge with few pages would otherwise resolve to
    # almost nothing.
    taken = {id(chunk) for chunk in spread}
    spread.extend(chunk for chunk in ranked if id(chunk) not in taken)
    return spread


def pages_for_badges(
    badges: list[dict[str, Any]], *, settings: Settings | None = None
) -> list[dict[str, Any]]:
    """Documentation pages to write from, deduplicated and within budget.

    Pages are taken in rounds — the best page for every query, then the second best
    for every query, and so on — rather than query by query. Filling the budget in
    query order would spend it all on the first topic area and leave the last ones
    with nothing, which is the imbalance the per-topic search was for.
    """
    settings = settings or get_settings()

    ranked: list[list[dict[str, Any]]] = []
    for badge in badges:
        for query in queries_for_badge(badge):
            try:
                found = doc_pages.search_page_texts(
                    query, limit=settings.doc_context_pages_per_topic
                )
            except Exception as exc:
                # Never fatal: the caller falls back to researching on the web.
                logger.warning("Corpus search failed for %r: %s", query, exc)
                return []
            if found:
                ranked.append(found)

    selected: dict[str, dict[str, Any]] = {}
    spent = 0
    depth = 0
    while ranked and depth < max((len(r) for r in ranked), default=0):
        for results in ranked:
            if depth >= len(results):
                continue
            page = results[depth]
            url = page.get("url")
            if not url or url in selected:
                continue
            text = (page.get("text") or "")[: settings.doc_context_page_chars]
            if not text:
                continue
            if spent + len(text) > settings.doc_context_char_budget:
                # Budget reached. Stopping here rather than skipping to a shorter
                # page keeps the material in relevance order.
                return list(selected.values())
            selected[url] = {
                "url": url,
                "title": page.get("title"),
                "source": page.get("source"),
                "score": page.get("score"),
                "text": text,
                "truncated": len(page.get("text") or "") > len(text),
            }
            spent += len(text)
        depth += 1

    return list(selected.values())


def format_pages(pages: list[dict[str, Any]]) -> str:
    """The retrieved pages as one block of prompt text.

    Each page is labelled with the URL it came from so a question can cite the page
    it was written from — the citation is the reason an author can check a question
    without re-researching it.
    """
    blocks = []
    for page in pages:
        header = f"### {page.get('title') or page['url']}\nSource: {page['url']}"
        if page.get("truncated"):
            header += "\n(This page is longer than shown; it has been cut short.)"
        blocks.append(f"{header}\n\n{page['text']}")
    return "\n\n---\n\n".join(blocks)
