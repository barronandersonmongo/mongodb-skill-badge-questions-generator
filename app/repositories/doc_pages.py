"""Persistence for the doc_pages collection — a local copy of MongoDB's docs.

Keyed on `url`, because that is what the published index gives us and what a
question's citation should point at.

Pages are stored so question authoring can read its source material from the
database instead of fetching it mid-run. A generation run that searches and fetches
the web spends most of its wall-clock time waiting on that, and two runs on the same
badge see different source text; a stored corpus fixes both.

`content_hash` exists so a refresh can tell "unchanged" from "updated" without
diffing text, which is what makes a re-run cheap and makes `updated` a meaningful
number on the admin screen.

A refresh replaces the whole corpus. Rather than emptying the collection first, each
page is stamped with the run that wrote it and pages left over from earlier runs are
deleted at the end — the same end state, except a crawl that dies half way through
leaves the previous corpus in place instead of nothing.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, UpdateOne
from pymongo.collection import Collection

from app.config import get_settings
from app.db import get_database

# The page text itself is excluded from listings: 10,000 pages of Markdown would
# otherwise be carried into every screen render.
LIST_PROJECTION = {"_id": False, "text": False}


def collection() -> Collection:
    return get_database()[get_settings().doc_pages_collection]


def ensure_indexes() -> None:
    coll = collection()
    coll.create_index([("url", ASCENDING)], unique=True, name="url_unique")
    coll.create_index([("source", ASCENDING)], name="source")
    coll.create_index([("fetched_at", ASCENDING)], name="fetched_at")
    # No text index: the corpus is searched by meaning through an Atlas Vector Search
    # index (see `search_pages`), whose definition lives in Atlas rather than here —
    # Atlas Search indexes are not created through create_index.


def content_hash(text: str) -> str:
    """Identify a page's content, so an unchanged refetch is recognisable as such."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upsert_pages(pages: list[dict[str, Any]], run_id: str | None = None) -> dict[str, int]:
    """Store fetched pages. Returns how many were new, changed, or unchanged.

    `first_seen_at` is set on insert only, so it records when this corpus first
    learned of a page rather than when it was last crawled.
    """
    if not pages:
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    ensure_indexes()
    coll = collection()
    now = datetime.now(timezone.utc)

    known = {
        doc["url"]: doc.get("content_hash")
        for doc in coll.find(
            {"url": {"$in": [p["url"] for p in pages]}},
            {"_id": False, "url": True, "content_hash": True},
        )
    }

    operations, inserted, updated, unchanged = [], 0, 0, 0
    for page in pages:
        digest = content_hash(page["text"])
        if page["url"] not in known:
            inserted += 1
        elif known[page["url"]] == digest:
            unchanged += 1
            # Still record that it was checked, so a stale page is distinguishable
            # from one that simply has not changed.
            operations.append(
                UpdateOne(
                    {"url": page["url"]},
                    {"$set": {"fetched_at": now, "refresh_run_id": run_id}},
                )
            )
            continue
        else:
            updated += 1

        operations.append(
            UpdateOne(
                {"url": page["url"]},
                {
                    "$set": {
                        "refresh_run_id": run_id,
                        "source": page.get("source"),
                        "title": page.get("title"),
                        "text": page["text"],
                        "bytes": len(page["text"].encode("utf-8")),
                        "content_hash": digest,
                        "fetched_at": now,
                    },
                    "$setOnInsert": {"first_seen_at": now},
                },
                upsert=True,
            )
        )

    if operations:
        coll.bulk_write(operations, ordered=False)
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


def sources_summary() -> list[dict[str, Any]]:
    """Per-source page counts and freshness, for the admin screen."""
    pipeline = [
        {
            "$group": {
                "_id": "$source",
                "pages": {"$sum": 1},
                "bytes": {"$sum": "$bytes"},
                "newest": {"$max": "$fetched_at"},
                "oldest": {"$min": "$fetched_at"},
            }
        },
        {"$sort": {"_id": ASCENDING}},
    ]
    return [
        {
            "source": row["_id"],
            "pages": row["pages"],
            "bytes": row.get("bytes") or 0,
            "newest": row.get("newest"),
            "oldest": row.get("oldest"),
        }
        for row in collection().aggregate(pipeline)
    ]


def totals() -> dict[str, Any]:
    """Whole-corpus totals, so the screen can say what is stored without listing it."""
    rows = list(
        collection().aggregate(
            [
                {
                    "$group": {
                        "_id": None,
                        "pages": {"$sum": 1},
                        "bytes": {"$sum": "$bytes"},
                        "newest": {"$max": "$fetched_at"},
                    }
                }
            ]
        )
    )
    if not rows:
        return {"pages": 0, "bytes": 0, "newest": None}
    row = rows[0]
    return {
        "pages": row["pages"],
        "bytes": row.get("bytes") or 0,
        "newest": row.get("newest"),
    }


def list_pages(
    source: str | None = None, limit: int = 200, contains: str | None = None
) -> list[dict[str, Any]]:
    """Stored pages, without their text, for drilling into one source.

    `contains` filters on title and URL in Python rather than with a database regex:
    the largest source is under a thousand pages, and matching here keeps the query a
    plain indexed equality instead of a scan with an unanchored pattern.
    """
    query = {"source": source} if source else {}
    found = list(collection().find(query, LIST_PROJECTION).sort("url", ASCENDING))
    if contains:
        needle = contains.strip().casefold()
        found = [
            page
            for page in found
            if needle in (page.get("title") or "").casefold()
            or needle in page["url"].casefold()
        ]
    return found[:limit]


EXCERPT_RADIUS = 130


def excerpt(text: str, terms: list[str]) -> str:
    """A short window of the page around the first matching term.

    Search results need enough context to tell "this is the page I meant" from "this
    merely mentions the word", and the alternative — opening each result to find out —
    is what the search exists to avoid.
    """
    lowered = text.casefold()
    position = min(
        (lowered.find(term.casefold()) for term in terms if term.casefold() in lowered),
        default=-1,
    )
    if position < 0:
        window = text[: EXCERPT_RADIUS * 2]
        prefix, suffix = "", "…" if len(text) > EXCERPT_RADIUS * 2 else ""
    else:
        start = max(0, position - EXCERPT_RADIUS)
        window = text[start : position + EXCERPT_RADIUS]
        prefix = "…" if start > 0 else ""
        suffix = "…" if position + EXCERPT_RADIUS < len(text) else ""
    # Collapsed to one line: the excerpt sits in a list row, and Markdown newlines and
    # heading marks make it unreadable there.
    return prefix + " ".join(window.split()) + suffix


def search_pages(query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Pages semantically closest to a query, best match first, each with an excerpt.

    Semantic rather than keyword: an author looking for source material knows the
    topic, not the wording. "How do I model a one-to-many relationship" has to reach
    the embedded-versus-referenced page, which never uses that phrase, and a keyword
    search returns nothing for it. The cost is that a term-for-term match is no longer
    guaranteed to rank first — but the corpus is being read for meaning, not grepped.

    The Atlas index is configured with autoEmbed on `text`, so the query sent is the
    text itself: the cluster embeds both sides with the same model, and this program
    stores no vectors and needs no embedding key.

    Searches the whole corpus rather than one source. Which of the 74 sources holds a
    topic is not something an author knows — the C# driver's real documentation is not
    under the drivers index, it is under its own — so requiring them to guess makes the
    corpus unusable for writing questions.
    """
    query = (query or "").strip()
    if not query:
        return []

    settings = get_settings()
    # An explicit inclusion projection. Reusing LIST_PROJECTION (an exclusion) and
    # adding "text": True would silently return only the included fields — no title, no
    # url — because MongoDB treats a projection naming any field as inclusion-only.
    projection = {
        "_id": False,
        "url": True,
        "source": True,
        "title": True,
        "bytes": True,
        "fetched_at": True,
        "text": True,
        "score": {"$meta": "vectorSearchScore"},
    }
    found = list(
        collection().aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": settings.doc_pages_vector_index_name,
                        "path": settings.doc_pages_vector_path,
                        "query": query,
                        "numCandidates": settings.doc_search_num_candidates,
                        "limit": limit,
                    }
                },
                {"$project": projection},
            ]
        )
    )

    terms = [t.strip('"') for t in query.split() if t.strip('"')]
    results = []
    for page in found:
        text = page.pop("text", "") or ""
        results.append({**page, "excerpt": excerpt(text, terms)})
    return results


def delete_stubs(smaller_than: int) -> int:
    """Remove navigation stubs stored before they were being skipped.

    A refresh would drop them anyway, since they are no longer stored and the sweep
    removes what a run did not write — but that means waiting for a full crawl to see
    the listings stop being cluttered by pages nothing can be written from.
    """
    return collection().delete_many({"bytes": {"$lt": smaller_than}}).deleted_count


def stored_urls() -> set[str]:
    """Every URL already in the corpus.

    Read as one projection rather than checked per page: a resume has ~7,000 URLs to
    test, and asking the database once for the set it already has is a single round trip
    against seven thousand.
    """
    return {doc["url"] for doc in collection().find({}, {"_id": False, "url": True})}


def count_pages(source: str | None = None) -> int:
    return collection().count_documents({"source": source} if source else {})


def get_page(url: str) -> dict[str, Any] | None:
    return collection().find_one({"url": url}, {"_id": False})


def delete_not_in_run(run_id: str) -> int:
    """Remove pages an earlier refresh stored and this one did not see.

    This is what makes a refresh a replacement rather than an accumulation: a page
    withdrawn upstream, or moved to a new URL, disappears from the corpus instead of
    lingering as documentation that no longer exists.
    """
    return collection().delete_many({"refresh_run_id": {"$ne": run_id}}).deleted_count


def delete_all() -> int:
    """Empty the corpus."""
    return collection().delete_many({}).deleted_count
