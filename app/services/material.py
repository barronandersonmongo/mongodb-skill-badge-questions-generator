"""How much documentation a badge still has to draw on, and how concentrated it is.

The coverage screen answers "which badges are thin". This answers the question
behind it: how likely is a badge to be *saturated* — to have nothing left worth
writing from — which is not the same as having few sections left.

The distinction is the whole point of this module. A walk takes one section per
article before it takes a second from any of them, because a section is only as
fresh as the article it came from: measured on the live corpus, a badge whose 25
sections came from six articles produced almost nothing, while one whose sections
spread over 24 articles produced 72 questions. So the number that predicts
saturation is the count of **distinct articles**, not the count of sections. A
badge with 252 sections across 25 articles has about 25 sections' worth of new
material and 227 helpings of what it already read.

Resolving a badge's section set is dozens of vector searches, so this is asked for
on demand and never in the path of an ordinary page load.
"""

import logging
from typing import Any

from app.config import Settings, get_settings
from app.repositories import doc_chunks, questions as questions_repo, skill_badges
from app.services import doc_retrieval

logger = logging.getLogger(__name__)


def _matches(chunk: dict[str, Any], term: str) -> bool:
    """Whether a section is about `term`, by the words attached to it.

    Matched against the heading, its path, the article title and the URL — the four
    places a topic name shows up in this corpus. A section's body is not searched:
    the body mentions everything the section relates to, so matching on it would put
    "the article that name-drops Voyage AI once" in the same bucket as "the article
    about Voyage AI".
    """
    haystack = " ".join(
        [
            str(chunk.get("heading") or ""),
            " ".join(chunk.get("heading_path") or []),
            str(chunk.get("page_title") or ""),
            str(chunk.get("url") or ""),
        ]
    ).lower()
    return term.lower() in haystack


def badge_material(
    *,
    skill_badge: str | None = None,
    contains: str | None = None,
    category: str | None = None,
    difficulty: str | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """A row per badge: what it has written, and what it has left to write from.

    `contains` narrows the documentation side — the sections a badge resolves to,
    matched on their headings and titles — which is what answers "how much of this
    badge is actually about Voyage AI". `category` and `difficulty` narrow the
    question side. They are separate filters because they act on different things,
    and a screen that merged them would report numbers that cannot both be true.
    """
    settings = settings or get_settings()
    counts = questions_repo.counts_by_badge()
    filtered_counts = (
        _counts_for(category, difficulty) if (category or difficulty) else None
    )

    rows: list[dict[str, Any]] = []
    for badge in skill_badges.list_badges():
        slug = badge["slug"]
        if skill_badge and slug != skill_badge:
            continue

        row: dict[str, Any] = {
            "skill_badge": slug,
            "name": badge.get("name"),
            "status": badge.get("status"),
            "questions": (filtered_counts or counts).get(slug, 0),
            "questions_all": counts.get(slug, 0),
        }
        try:
            used = questions_repo.source_chunk_ids_for_badge(slug)
            available = doc_retrieval.chunk_set_for_badge(
                badge, exclude_chunk_ids=used, settings=settings
            )
            if contains:
                available = [c for c in available if _matches(c, contains)]
            row.update(_shape(available))
            row["sections_used"] = len(used)
            row["articles_used"] = len(_urls_for_chunk_ids(used))
        except Exception as exc:
            # The section set needs the Atlas index. The question counts are still
            # worth showing without it, so this row reports what it knows.
            logger.warning("Material could not resolve sections for %s: %s", slug, exc)
            row.update(
                sections=None,
                articles=None,
                sections_per_article=None,
                largest_article=None,
                largest_article_url=None,
                sections_used=None,
                articles_used=None,
                error=str(exc),
            )
        rows.append(row)

    # Nearest to saturation first: fewest fresh articles is the badge about to run out
    # of new material, whatever its section count says.
    return sorted(
        rows,
        key=lambda r: (
            r["articles"] if isinstance(r.get("articles"), int) else 10**9,
            r["questions"],
        ),
    )


def _shape(available: list[dict[str, Any]]) -> dict[str, Any]:
    """The section set described by how it is spread, not only how big it is."""
    by_article: dict[str, int] = {}
    for chunk in available:
        by_article[chunk.get("url") or ""] = by_article.get(chunk.get("url") or "", 0) + 1
    articles = len(by_article)
    largest_url, largest = ("", 0)
    if by_article:
        largest_url, largest = max(by_article.items(), key=lambda item: item[1])
    return {
        "sections": len(available),
        "articles": articles,
        # How many helpings the average article is being asked for. Above about 3 the
        # walk is re-reading articles rather than finding new ones — 3 is the per-page
        # cap a walk applies, so it is where "more sections" stops meaning "more
        # material".
        "sections_per_article": round(len(available) / articles, 1) if articles else None,
        "largest_article": largest,
        "largest_article_url": largest_url,
    }


def _counts_for(category: str | None, difficulty: str | None) -> dict[str, int]:
    """Per-badge question counts under the question-side filters."""
    match: dict[str, Any] = {}
    if category:
        match["categories"] = category
    if difficulty:
        match["difficulty"] = difficulty
    pipeline: list[dict[str, Any]] = []
    if match:
        pipeline.append({"$match": match})
    pipeline += [
        {"$unwind": "$skill_badges"},
        {"$group": {"_id": "$skill_badges", "n": {"$sum": 1}}},
    ]
    return {
        row["_id"]: row["n"] for row in questions_repo.collection().aggregate(pipeline)
    }


def _urls_for_chunk_ids(chunk_ids: set[str]) -> set[str]:
    """Which articles a set of already-written-from sections came from.

    Looked up rather than derived: a section's identifier does not carry its article,
    and "we have written from 23 sections" says nothing about whether that was 23
    articles or one article twenty-three times.
    """
    if not chunk_ids:
        return set()
    rows = doc_chunks.collection().find(
        {"chunk_id": {"$in": list(chunk_ids)}}, {"_id": False, "url": True}
    )
    return {row.get("url") for row in rows if row.get("url")}
