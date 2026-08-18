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


def list_pages(source: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    query = {"source": source} if source else {}
    return list(
        collection().find(query, LIST_PROJECTION).sort("url", ASCENDING).limit(limit)
    )


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
