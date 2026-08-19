"""Find duplicate questions already in the collection.

An ad-hoc sweep, not a check on every generation run: authoring is where the time
and the money go, and a duplicate costs nothing until someone builds a quiz from
the collection. So duplicates are found on request, over what is stored.

Finding never deletes. There used to be a delete mode and a dry-run mode, which
made the operator choose before seeing the collection — and since the safe mode is
strictly more informative, nobody should ever have run the other one first. Now the
sweep reports, and deleting is a separate act on a list somebody has read. That also
demotes the score threshold: it shortlists pairs worth looking at rather than
deciding which questions die.

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
from collections.abc import Callable
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


def find_pairs(
    *,
    settings: Settings | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict], list[str]]:
    """Every pair of stored questions the reranker scored, most similar first.

    Each pair is scored once: A-B and B-A are the same pair, and scoring both would
    double the work to reach the same answer.

    `progress` is called after each question with how far the sweep has got. The work
    is one round trip per stored question, and how many there are is known before the
    first one — so on a collection of thousands this is minutes of a bar that would
    otherwise sit at nothing, with no way to tell a slow sweep from a stuck one.
    """
    settings = settings or get_settings()
    stored = questions_repo.list_questions()
    by_id = {q["question_id"]: q for q in stored}

    seen: set[tuple[str, str]] = set()
    pairs: list[dict[str, Any]] = []
    errors: list[str] = []
    total = len(stored)

    def report_progress(done: int) -> None:
        if progress is None:
            return
        progress(
            {
                "phase": "comparing",
                "compared": done,
                "total": total,
                # Guarded rather than assumed: an empty collection is a legitimate
                # sweep, and dividing by its size is not.
                "percent": (done / total * 100) if total else 100.0,
                "pairs": len(pairs),
                "errors": len(errors),
            }
        )

    report_progress(0)

    for index, question in enumerate(stored, start=1):
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
            report_progress(index)
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

        report_progress(index)

    pairs.sort(key=lambda item: item["rerank_score"], reverse=True)
    return pairs, errors


def report(
    *,
    settings: Settings | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Duplicate candidates, scored, deleting nothing.

    Pairs at or above the threshold are `flagged` — the ones worth acting on — and the
    rest are reported below it so the threshold itself stays visible as a judgement
    rather than a fact. Every pair names the question this program would drop and the
    one it would keep, so acting on the list is one click rather than a comparison the
    reader has to redo.
    """
    settings = settings or get_settings()
    pairs, errors = find_pairs(settings=settings, progress=progress)

    flagged, below = [], []
    for pair in pairs:
        if pair["rerank_score"] >= settings.question_rerank_delete_threshold:
            flagged.append(pair)
        else:
            below.append(pair)

    logger.info(
        "Duplicate sweep: %d pair(s) compared, %d flagged, %d below threshold, %d error(s)",
        len(pairs),
        len(flagged),
        len(below),
        len(errors),
    )
    return {
        "source": "question-duplicate-sweep",
        "compared": len(pairs),
        "threshold": settings.question_rerank_delete_threshold,
        "flagged": flagged,
        "below_threshold": below,
        "errors": errors,
    }
