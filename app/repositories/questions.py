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
from app.models.question import GeneratedQuestion

STATUSES = ("draft", "approved", "rejected")

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


def categories_in_use() -> list[str]:
    """Every category any stored question carries, sorted, for the filter menu."""
    found: set[str] = set()
    for doc in collection().find({}, {"_id": False, "categories": True}):
        found.update(doc.get("categories") or [])
    return sorted(found)
