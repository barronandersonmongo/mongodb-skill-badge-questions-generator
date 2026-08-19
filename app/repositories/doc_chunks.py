"""Persistence for the doc_chunks collection — the sections questions come from.

Derived from `doc_pages`, never crawled. That separation is deliberate: the chunk
band is a judgement that will want re-tuning, and rebuilding chunks from stored
pages takes seconds where re-crawling takes twelve minutes. It also leaves whole
pages available for the page viewer, which should show what MongoDB published rather
than how this program chose to cut it up.

Keyed on `chunk_id`, derived from the page URL and the chunk's position in it — so
re-chunking an unchanged page produces the same ids, and questions written from a
chunk stay attributable across a rebuild.

A page's chunks are replaced as a set. Chunking is deterministic, so a page whose
text has not changed produces identical chunks; a page that has changed produces a
different number of them, and replacing the set is the only way to avoid leaving
orphans from the previous shape behind.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING, DeleteMany, InsertOne
from pymongo.collection import Collection

from app.config import get_settings
from app.db import get_database

logger = logging.getLogger(__name__)

# The chunk text is excluded from listings: 18,000 chunks of Markdown would
# otherwise be carried into every screen render.
LIST_PROJECTION = {"_id": False, "text": False, "embed_text": False}


def collection() -> Collection:
    return get_database()[get_settings().doc_chunks_collection]


def ensure_indexes() -> None:
    coll = collection()
    coll.create_index([("chunk_id", ASCENDING)], unique=True, name="chunk_id_unique")
    # Replacing a page's chunks, and listing a page's chunks, both key on the URL.
    coll.create_index([("url", ASCENDING), ("ordinal", ASCENDING)], name="url_ordinal")
    coll.create_index([("source", ASCENDING)], name="source")
    # No vector index: retrieval runs on an Atlas Search index whose definition lives
    # in Atlas, and Atlas Search indexes are not created through create_index.


def replace_page_chunks(
    url: str, chunks: list[dict[str, Any]], run_id: str | None = None
) -> int:
    """Store a page's chunks, replacing whatever it had before. Returns the count.

    One bulk write, so a page is never briefly chunkless: the delete and the inserts
    land together rather than leaving a window in which retrieval cannot see the page
    at all.

    Stamped with the run that wrote them, exactly as pages are, so a refresh can sweep
    chunks the same way it sweeps pages — by what this run did not write — rather than
    by comparing two collections.
    """
    ensure_indexes()
    now = datetime.now(timezone.utc)
    operations: list[Any] = [DeleteMany({"url": url})]
    for chunk in chunks:
        operations.append(
            InsertOne({**chunk, "chunked_at": now, "refresh_run_id": run_id})
        )
    if operations:
        collection().bulk_write(operations, ordered=True)
    return len(chunks)


def delete_not_in_run(run_id: str) -> int:
    """Remove chunks that this run did not write.

    The counterpart of the page sweep. A chunk outliving its page is invisible and
    harmful: retrieval keeps offering it, and a question written from it cites a URL
    that no longer exists.
    """
    return collection().delete_many(
        {"refresh_run_id": {"$ne": run_id}}
    ).deleted_count


def delete_all() -> int:
    """Remove every chunk. Used when the corpus is replaced wholesale."""
    return collection().delete_many({}).deleted_count


def delete_orphans(known_urls: set[str]) -> int:
    """Remove chunks whose page is no longer in the corpus.

    A refresh sweeps pages MongoDB no longer publishes; their chunks would otherwise
    stay retrievable, so a question could be written from a page that no longer
    exists and cite a URL that 404s.
    """
    if not known_urls:
        return collection().delete_many({}).deleted_count
    return collection().delete_many({"url": {"$nin": list(known_urls)}}).deleted_count


def count() -> int:
    return collection().count_documents({})


def totals() -> dict[str, Any]:
    """Corpus-wide chunk figures, for the documentation screen."""
    pipeline = [
        {
            "$group": {
                "_id": None,
                "chunks": {"$sum": 1},
                "chars": {"$sum": "$chars"},
                "pages": {"$addToSet": "$url"},
            }
        }
    ]
    rows = list(collection().aggregate(pipeline))
    if not rows:
        return {"chunks": 0, "chars": 0, "pages": 0, "mean_chars": 0}
    row = rows[0]
    chunks = row.get("chunks") or 0
    return {
        "chunks": chunks,
        "chars": row.get("chars") or 0,
        "pages": len(row.get("pages") or []),
        "mean_chars": round((row.get("chars") or 0) / chunks) if chunks else 0,
    }


def chunks_for_page(url: str) -> list[dict[str, Any]]:
    """One page's chunks in order, without their text."""
    return list(
        collection().find({"url": url}, LIST_PROJECTION).sort("ordinal", ASCENDING)
    )


def get_chunk(chunk_id: str) -> dict[str, Any] | None:
    """One chunk in full — the unit a question is written from."""
    return collection().find_one({"chunk_id": chunk_id}, {"_id": False})


def _vector_search(
    query: str, limit: int, *, include_text: bool, settings=None
) -> list[dict[str, Any]]:
    """Chunks closest in meaning to a query, best first.

    The Atlas index is configured with autoEmbed on `embed_text`, so the query sent is
    the text itself and the cluster embeds both sides.
    """
    # Taken from the caller when it has them: retrieval already holds a Settings, and
    # reaching for a fresh one here would make a search depend on a connection string
    # it never uses.
    settings = settings or get_settings()
    projection: dict[str, Any] = {
        "_id": False,
        "chunk_id": True,
        "url": True,
        "anchor": True,
        "source": True,
        "page_title": True,
        "heading": True,
        "heading_path": True,
        "ordinal": True,
        "chars": True,
        "score": {"$meta": "vectorSearchScore"},
    }
    if include_text:
        projection["text"] = True
        projection["embed_text"] = True
    return list(
        collection().aggregate(
            [
                {
                    "$vectorSearch": {
                        "index": settings.doc_chunks_vector_index_name,
                        "path": settings.doc_chunks_vector_path,
                        "query": query,
                        "numCandidates": settings.doc_search_num_candidates,
                        "limit": limit,
                    }
                },
                {"$project": projection},
            ]
        )
    )


def search_chunk_refs(query: str, *, limit: int = 60, settings=None) -> list[dict[str, Any]]:
    """Ranked chunks carrying only what identifies and scores them.

    Resolving a badge to its chunk set ranks several hundred candidates and decides
    which belong; reading their text to judge relevance would move megabytes.
    """
    query = (query or "").strip()
    if not query:
        return []
    return _vector_search(query, limit, include_text=False, settings=settings)


def search_chunks(query: str, *, limit: int = 50, settings=None) -> list[dict[str, Any]]:
    """Ranked chunks with their text, for the corpus search screen."""
    query = (query or "").strip()
    if not query:
        return []
    return _vector_search(query, limit, include_text=True, settings=settings)
