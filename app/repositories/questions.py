"""Persistence for the questions collection.

Questions are keyed on `question_id`, a generated identifier rather than a slug:
two questions on the same topic are legitimately different questions, so nothing
about a question's content is its identity.

`status` is a human decision and is only set on insert; a later generation run
never revisits it.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING
from pymongo.collection import Collection

from app.config import get_settings
from app.db import get_database
from app.models.question import GeneratedQuestion, combined_text

STATUSES = ("draft", "approved", "rejected")

# The field a vector search index is expected to point at. Kept as a named
# constant because it is part of an external contract: an Atlas index definition
# names this path, so renaming the field silently breaks that index.
EMBEDDING_FIELD = "embedding_text"

# The stored bytes of a question are small, so listings carry the whole document
# apart from Mongo's own key, which is not part of this program's model.
LIST_PROJECTION = {"_id": False}


def collection() -> Collection:
    return get_database()[get_settings().questions_collection]


def ensure_indexes() -> None:
    coll = collection()
    coll.create_index([("question_id", ASCENDING)], unique=True, name="question_id_unique")
    # The three fields the review screen filters on.
    coll.create_index([("skill_badges", ASCENDING)], name="skill_badges")
    coll.create_index([("categories", ASCENDING)], name="categories")
    coll.create_index([("status", ASCENDING)], name="status")


def insert_questions(questions: list[GeneratedQuestion]) -> dict[str, Any]:
    """Store newly generated questions as drafts. Returns a summary for the UI."""
    if not questions:
        return {"run_id": None, "inserted": 0, "question_ids": []}

    ensure_indexes()
    run_id = uuid4().hex
    now = datetime.now(timezone.utc)

    docs = []
    for question in questions:
        docs.append(
            {
                **question.model_dump(),
                "question_id": uuid4().hex,
                "created_at": now,
                "generation_run_id": run_id,
                "status": "draft",
                # Composed on write, so a question is embeddable the moment it
                # lands rather than after some later maintenance step.
                EMBEDDING_FIELD: combined_text(question.stem, question.explanation),
            }
        )
    collection().insert_many(docs)
    return {
        "run_id": run_id,
        "inserted": len(docs),
        "question_ids": [d["question_id"] for d in docs],
    }


def list_questions(
    status: str | None = None,
    skill_badge: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """Questions matching every filter given, newest first.

    Newest first because the author's usual question is "what did that run just
    produce?", and a generation run appends to the end of the collection.
    """
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if skill_badge:
        query["skill_badges"] = skill_badge
    if category:
        query["categories"] = category
    return list(
        collection().find(query, LIST_PROJECTION).sort("created_at", DESCENDING)
    )


def set_status(question_id: str, status: str) -> bool:
    result = collection().update_one(
        {"question_id": question_id}, {"$set": {"status": status}}
    )
    return result.matched_count == 1


def delete_question(question_id: str) -> bool:
    """Permanently remove a question.

    Unlike a badge, a question has no upstream source to re-derive it from, so a
    delete really is final — the UI asks first.
    """
    return collection().delete_one({"question_id": question_id}).deleted_count == 1


def similar_by_embedding_text(
    text: str,
    index_name: str,
    *,
    limit: int = 5,
    exclude_question_id: str | None = None,
    num_candidates: int = 100,
) -> list[dict[str, Any]]:
    """Nearest stored questions by meaning, most similar first.

    The index is configured with autoEmbed, so the query is the text itself — Atlas
    embeds it with the same model it used for the stored questions, and this program
    stores no vectors.

    A question is excluded by id rather than by text so that a question already
    stored does not come back as its own nearest neighbour when it is rescreened.
    """
    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": index_name,
                "path": EMBEDDING_FIELD,
                "query": text,
                "numCandidates": num_candidates,
                "limit": limit + (1 if exclude_question_id else 0),
            }
        },
        {
            "$project": {
                "_id": False,
                "question_id": True,
                "stem": True,
                "explanation": True,
                "options": True,
                "skill_badges": True,
                "categories": True,
                "difficulty": True,
                "status": True,
                "source_urls": True,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]
    results = list(collection().aggregate(pipeline))
    if exclude_question_id:
        results = [r for r in results if r.get("question_id") != exclude_question_id]
    return results[:limit]


SEARCH_PROJECTION = {
    "_id": False,
    "question_id": True,
    "stem": True,
    "explanation": True,
    "options": True,
    "skill_badges": True,
    "categories": True,
    "difficulty": True,
    "status": True,
    "source_urls": True,
    "embedding_text": True,
}


def reranked_by_embedding_text(
    text: str,
    index_name: str,
    *,
    model: str,
    limit: int = 5,
    exclude_question_id: str | None = None,
    num_candidates: int = 100,
) -> list[dict[str, Any]]:
    """Nearest questions, re-scored by the reranker, best first.

    Two stages in one aggregation: $vectorSearch shortlists cheaply, then $rerank
    re-scores each candidate against the query with a cross-encoder that reads both
    texts together. The cluster runs both models, so this needs no API key and makes
    no second round trip.

    The returned `score` is the rerank score, not the vector score — they are not on
    the same scale, so a caller must not compare one to a threshold meant for the
    other. Use `similar_by_embedding_text` when the vector score is what is wanted.
    """
    wanted = limit + (1 if exclude_question_id else 0)
    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": index_name,
                "path": EMBEDDING_FIELD,
                "query": text,
                "numCandidates": num_candidates,
                "limit": wanted,
            }
        },
        {
            "$rerank": {
                "path": EMBEDDING_FIELD,
                "query": {"text": text},
                "model": model,
                "numDocsToRerank": wanted,
            }
        },
        {"$project": {**SEARCH_PROJECTION, "score": {"$meta": "score"}}},
    ]
    results = list(collection().aggregate(pipeline))
    if exclude_question_id:
        results = [r for r in results if r.get("question_id") != exclude_question_id]
    return results[:limit]


def backfill_embedding_text() -> dict[str, Any]:
    """Compose the embedding field for stored questions that lack it, or drifted.

    Needed because questions written before the field existed carry none, and a
    vector index would silently skip them — an empty search result looks the same
    as a question that does not exist. Recomposing when the text no longer matches
    the stem and explanation also covers a question edited by hand in Atlas.
    """
    written, already_correct = 0, 0
    for doc in collection().find(
        {}, {"_id": False, "question_id": True, "stem": True, "explanation": True,
             EMBEDDING_FIELD: True}
    ):
        wanted = combined_text(doc.get("stem", ""), doc.get("explanation"))
        if doc.get(EMBEDDING_FIELD) == wanted:
            already_correct += 1
            continue
        collection().update_one(
            {"question_id": doc["question_id"]}, {"$set": {EMBEDDING_FIELD: wanted}}
        )
        written += 1
    return {"written": written, "already_correct": already_correct}


def source_urls_for_badge(slug: str) -> set[str]:
    """Every documentation page this badge already has questions from.

    This is what makes the page walk resumable: a run skips the pages already written
    from, so walking a badge twice covers new material instead of re-mining the same
    pages. Read as one projection — the set is the whole answer, and asking per page
    would be one round trip per candidate.
    """
    return {
        url
        for doc in collection().find(
            {"skill_badges": slug}, {"_id": False, "source_urls": True}
        )
        for url in (doc.get("source_urls") or [])
    }


def counts_by_badge() -> dict[str, dict[str, int]]:
    """Per-badge question counts by status, for the coverage screen.

    A question filed under several badges counts once for each, because the question
    that matters is "does this badge have enough", not "how many questions exist".
    """
    pipeline = [
        {"$unwind": "$skill_badges"},
        {
            "$group": {
                "_id": {"slug": "$skill_badges", "status": "$status"},
                "n": {"$sum": 1},
            }
        },
    ]
    counts: dict[str, dict[str, int]] = {}
    for row in collection().aggregate(pipeline):
        slug = row["_id"]["slug"]
        status = row["_id"].get("status") or "draft"
        entry = counts.setdefault(slug, {"draft": 0, "approved": 0, "rejected": 0, "total": 0})
        entry[status] = entry.get(status, 0) + row["n"]
        entry["total"] += row["n"]
    return counts


def categories_in_use() -> list[str]:
    """Every category any stored question carries, sorted, for the filter menu."""
    found: set[str] = set()
    for doc in collection().find({}, {"_id": False, "categories": True}):
        found.update(doc.get("categories") or [])
    return sorted(found)
