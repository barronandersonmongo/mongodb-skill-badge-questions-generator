"""Find duplicate questions already in the collection.

An ad-hoc sweep, not a check on every generation run: authoring is where the time
and money go, and a duplicate costs nothing until someone builds a quiz from the
collection. So duplicates are found on request, over what is stored.

Two stages, neither of them a language model:

1. **Shortlist** — Atlas Vector Search over `embedding_text` proposes each
   question's nearest neighbours. Cheap, and it is only a shortlist: recall matters
   here, precision does not, so the score floor is deliberately loose.
2. **Decide** — a reranker (Voyage rerank-2.5) scores each shortlisted pair. A
   cross-encoder reads both texts together, which is what makes it able to
   distinguish "same question reworded" from "same topic", where a similarity score
   between two independently embedded texts cannot.

An LLM did this before. It was accurate but cost a generation round trip per pair;
a reranker answers the same question in one cheap call and returns a number.
"""

import logging
from typing import Any

from app.config import Settings, get_settings
from app.models.question import combined_text
from app.repositories import questions as questions_repo
from app.services.reranker import rerank_pairs

logger = logging.getLogger(__name__)


def _text(question: dict[str, Any]) -> str:
    """The text a pair is compared on: the same block the index embedded."""
    return question.get("embedding_text") or combined_text(
        question.get("stem", ""), question.get("explanation")
    )


def _rank(question: dict[str, Any]) -> tuple:
    """How much a question is worth keeping, highest first.

    Approved questions carry a review decision and must outlive a draft. Beyond
    that, prefer the one attributed to more badges — it is reachable from more
    places, so keeping it loses the least — and then the older one, which anything
    downstream is more likely to have already seen.
    """
    return (
        question.get("status") == "approved",
        len(question.get("skill_badges") or []),
        -(question.get("created_at").timestamp() if question.get("created_at") else 0),
    )


def choose_survivor(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (keep, drop) for a duplicate pair."""
    return (left, right) if _rank(left) >= _rank(right) else (right, left)


def shortlist_pairs(
    *, settings: Settings | None = None
) -> tuple[list[tuple[dict, dict, float]], list[str]]:
    """Every pair of stored questions close enough to be worth reranking.

    Each pair is returned once: A-B and B-A are the same pair, and reranking both
    would double the cost to reach the same answer.
    """
    settings = settings or get_settings()
    stored = questions_repo.list_questions()
    by_id = {q["question_id"]: q for q in stored}

    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[dict, dict, float]] = []
    errors: list[str] = []

    for question in stored:
        try:
            neighbours = questions_repo.similar_by_embedding_text(
                _text(question),
                settings.questions_vector_index_name,
                limit=settings.question_duplicate_neighbours,
                exclude_question_id=question["question_id"],
            )
        except Exception as exc:
            # One unsearchable question must not abandon the whole sweep.
            errors.append(f"{question['question_id']}: {exc}")
            continue

        for neighbour in neighbours:
            other = by_id.get(neighbour.get("question_id"))
            if not other:
                continue
            if (neighbour.get("score") or 0.0) < settings.question_duplicate_score_threshold:
                continue
            key = tuple(sorted((question["question_id"], other["question_id"])))
            if key in seen:
                continue
            seen.add(key)
            pairs.append((question, other, neighbour.get("score") or 0.0))
    return pairs, errors


def sweep(
    *, delete: bool = True, settings: Settings | None = None
) -> dict[str, Any]:
    """Find duplicate questions, and optionally delete one of each pair.

    `delete=False` is a dry run: the same pairs and scores, nothing removed. That is
    how the delete threshold should be checked against real data before it is
    trusted, because a deletion here has no judge behind it and cannot be undone.
    """
    settings = settings or get_settings()
    pairs, errors = shortlist_pairs(settings=settings)

    if not pairs:
        return {
            "source": "question-duplicate-sweep",
            "compared": 0,
            "deleted": [],
            "possible_duplicates": [],
            "errors": errors,
            "dry_run": not delete,
        }

    # One request per question, scoring all of its shortlisted partners together.
    grouped: dict[str, list[tuple[dict, dict, float]]] = {}
    for left, right, score in pairs:
        grouped.setdefault(left["question_id"], []).append((left, right, score))

    scored: list[dict[str, Any]] = []
    for group in grouped.values():
        query = _text(group[0][0])
        try:
            relevance = rerank_pairs(query, [_text(right) for _, right, _ in group])
        except Exception as exc:
            errors.append(f"rerank failed for {group[0][0]['question_id']}: {exc}")
            continue
        for (left, right, vector_score), rerank_score in zip(group, relevance):
            keep, drop = choose_survivor(left, right)
            scored.append(
                {
                    "keep": keep["question_id"],
                    "keep_stem": keep.get("stem"),
                    "drop": drop["question_id"],
                    "drop_stem": drop.get("stem"),
                    "vector_score": vector_score,
                    "rerank_score": rerank_score,
                }
            )

    scored.sort(key=lambda item: item["rerank_score"], reverse=True)

    deleted, possible = [], []
    removed: set[str] = set()
    for candidate in scored:
        if candidate["rerank_score"] < settings.question_rerank_delete_threshold:
            possible.append(candidate)
            continue
        if not delete:
            possible.append({**candidate, "would_delete": True})
            continue
        # A question already removed by an earlier pair cannot be deleted again, and
        # must not become the survivor of a later one either.
        if candidate["drop"] in removed or candidate["keep"] in removed:
            possible.append({**candidate, "skipped": "already resolved"})
            continue
        if questions_repo.delete_question(candidate["drop"]):
            removed.add(candidate["drop"])
            deleted.append(candidate)

    logger.info(
        "Duplicate sweep: %d pair(s) compared, %d deleted, %d reported, %d error(s)%s",
        len(scored),
        len(deleted),
        len(possible),
        len(errors),
        " (dry run)" if not delete else "",
    )
    return {
        "source": "question-duplicate-sweep",
        "compared": len(scored),
        "deleted": deleted,
        "possible_duplicates": possible,
        "errors": errors,
        "dry_run": not delete,
    }
