"""Find duplicate questions already in the collection.

An ad-hoc sweep, not a check on every generation run: authoring is where the time
and the money go, and a duplicate costs nothing until someone builds a quiz from
the collection. So duplicates are found on request, over what is stored.

No language model is involved, and nothing here needs an API key. One aggregation
per question does both stages on the cluster:

1. `$vectorSearch` over `embedding_text` shortlists the nearest questions. Recall
   matters here and precision does not, so the floor is loose.
2. `$rerank` re-scores each candidate with a cross-encoder that reads both texts
   together — which is what separates "the same question reworded" from "the same
   topic", something two independently embedded vectors cannot do.

An earlier design asked Claude to judge each pair. It was accurate but cost a
generation round trip per pair; the reranker answers the same question inside the
query the shortlist already ran.
"""

import logging
from typing import Any

from app.config import Settings, get_settings
from app.models.question import combined_text
from app.repositories import questions as questions_repo

logger = logging.getLogger(__name__)


def _text(question: dict[str, Any]) -> str:
    """The text a pair is compared on: the same block the index embedded."""
    return question.get("embedding_text") or combined_text(
        question.get("stem", ""), question.get("explanation")
    )


def _rank(question: dict[str, Any]) -> tuple:
    """How much a question is worth keeping, highest first.

    There is no review state to prefer, so this turns on findability and age: prefer
    the question attributed to more badges, because it is reachable from more places
    and dropping it loses the most, and then the older one, which anything downstream
    is more likely to have seen already.
    """
    created = question.get("created_at")
    return (
        len(question.get("skill_badges") or []),
        -(created.timestamp() if created else 0),
    )


def choose_survivor(
    left: dict[str, Any], right: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (keep, drop) for a duplicate pair."""
    return (left, right) if _rank(left) >= _rank(right) else (right, left)


def find_pairs(*, settings: Settings | None = None) -> tuple[list[dict], list[str]]:
    """Every pair of stored questions the reranker scored, most similar first.

    Each pair is scored once: A-B and B-A are the same pair, and scoring both would
    double the work to reach the same answer.
    """
    settings = settings or get_settings()
    stored = questions_repo.list_questions()
    by_id = {q["question_id"]: q for q in stored}

    seen: set[tuple[str, str]] = set()
    pairs: list[dict[str, Any]] = []
    errors: list[str] = []

    for question in stored:
        try:
            neighbours = questions_repo.reranked_by_embedding_text(
                _text(question),
                settings.questions_vector_index_name,
                model=settings.rerank_model,
                limit=settings.question_duplicate_neighbours,
                exclude_question_id=question["question_id"],
            )
        except Exception as exc:
            # One unsearchable question must not abandon the whole sweep, and must
            # never be reported as "no duplicates here".
            errors.append(f"{question['question_id']}: {exc}")
            continue

        for neighbour in neighbours:
            other = by_id.get(neighbour.get("question_id"))
            if not other:
                # The index lags deletions, so a neighbour may already be gone.
                continue
            key = tuple(sorted((question["question_id"], other["question_id"])))
            if key in seen:
                continue
            seen.add(key)
            keep, drop = choose_survivor(question, other)
            pairs.append(
                {
                    "keep": keep["question_id"],
                    "keep_stem": keep.get("stem"),
                    "drop": drop["question_id"],
                    "drop_stem": drop.get("stem"),
                    "rerank_score": neighbour.get("score") or 0.0,
                }
            )

    pairs.sort(key=lambda item: item["rerank_score"], reverse=True)
    return pairs, errors


def sweep(*, delete: bool = True, settings: Settings | None = None) -> dict[str, Any]:
    """Find duplicate questions, and optionally delete one of each confident pair.

    `delete=False` is a dry run: the same pairs and scores, nothing removed. A
    deletion here has no judge behind it and cannot be undone, so the threshold
    should be re-checked this way whenever the collection changes character.
    """
    settings = settings or get_settings()
    pairs, errors = find_pairs(settings=settings)

    deleted, possible = [], []
    removed: set[str] = set()
    for pair in pairs:
        if pair["rerank_score"] < settings.question_rerank_delete_threshold:
            possible.append(pair)
            continue
        if not delete:
            possible.append({**pair, "would_delete": True})
            continue
        # A question already removed by an earlier pair cannot be deleted again, and
        # must not become the survivor of a later one either — otherwise three near
        # copies could leave none.
        if pair["drop"] in removed or pair["keep"] in removed:
            possible.append({**pair, "skipped": "already resolved"})
            continue
        if questions_repo.delete_question(pair["drop"]):
            removed.add(pair["drop"])
            deleted.append(pair)

    logger.info(
        "Duplicate sweep: %d pair(s) compared, %d deleted, %d reported, %d error(s)%s",
        len(pairs),
        len(deleted),
        len(possible),
        len(errors),
        " (dry run)" if not delete else "",
    )
    return {
        "source": "question-duplicate-sweep",
        "compared": len(pairs),
        "deleted": deleted,
        "possible_duplicates": possible,
        "errors": errors,
        "dry_run": not delete,
    }
