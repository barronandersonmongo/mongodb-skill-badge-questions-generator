"""Assemble a badge's source material out of the stored documentation corpus.

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
from typing import Any

from app.config import Settings, get_settings
from app.repositories import doc_pages

logger = logging.getLogger(__name__)


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
