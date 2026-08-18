"""Find questions that duplicate ones already stored.

Two authors generating for the same badge — or one author running generation
twice — produce questions that test the same thing in different words. Text
comparison cannot catch that, so candidates are found by searching
`embedding_text` semantically (Atlas Vector Search, autoEmbed) and then judged by
Claude on what the questions actually test.

Screening happens before a question is stored, which is the cheap moment: once a
near-duplicate is in the collection an author has to notice it while reviewing,
and a quiz built from that collection can ask the same thing twice.

Confident duplicates are dropped. Anything less is stored and reported, because
discarding a question a model merely suspected is worse than showing an author two
questions and letting them choose.
"""

import logging

from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.models.question import GeneratedQuestion, combined_text
from app.repositories import questions as questions_repo

logger = logging.getLogger(__name__)

JUDGE_SYSTEM = """\
You decide whether two MongoDB quiz questions are DUPLICATES.

Two questions are duplicates when they test the same knowledge, so that a \
candidate who can answer one can answer the other for the same reason. Wording, \
scenario details, option order and the names used in an example do not matter — \
the same question dressed in a different story is still the same question.

Two questions are NOT duplicates when answering them correctly requires different \
knowledge, even if they share a topic, a feature name, or an almost identical \
scenario. In particular these are always different questions:

- the same feature approached from different decisions (when to use it vs. how it \
  behaves vs. why it fails);
- questions whose correct answers are different facts;
- a question about a concept and a question about applying that concept to a \
  specific situation.

Read the options, not just the stem. Two stems can be near-identical while the \
options test different distinctions — and two very different stems can reduce to \
the same single fact.

Answer "duplicate" only when you are confident. Saying they differ is safe: the \
question is kept and a person reviews it. A confident duplicate is discarded \
before anyone sees it, so a wrong "duplicate" silently loses work."""


class QuestionDuplicateVerdict(BaseModel):
    duplicate: bool = Field(description="True only if these test the same knowledge")
    confident: bool = Field(
        description="True only if the pair is unambiguous. Confident duplicates are "
        "discarded without review, so this must mean certain."
    )
    reason: str = Field(
        description="One sentence citing what both test, or the difference"
    )


def _describe(question: dict) -> str:
    """One question as the judge sees it: stem, every option, and the answer."""
    lines = [f"stem: {question.get('stem')}"]
    for option in question.get("options") or []:
        mark = "CORRECT" if option.get("is_correct") else "wrong"
        lines.append(f"  option ({mark}): {option.get('text')}")
    if question.get("explanation"):
        lines.append(f"explanation: {question['explanation']}")
    return "\n".join(lines)


def judge_pair(
    candidate: dict, stored: dict, *, settings: Settings | None = None
) -> QuestionDuplicateVerdict:
    """Ask Claude whether a new question duplicates a stored one."""
    from app.services.badge_discovery import _client, _translate_auth_error

    settings = settings or get_settings()
    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=4000,
            system=JUDGE_SYSTEM,
            output_format=QuestionDuplicateVerdict,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"New question:\n{_describe(candidate)}\n\n"
                        f"Already stored:\n{_describe(stored)}"
                    ),
                }
            ],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise

    if response.parsed_output is None:
        raise RuntimeError(
            f"Duplicate judgement produced no structured output (stop_reason="
            f"{response.stop_reason})."
        )
    return response.parsed_output


def screen_questions(
    candidates: list[GeneratedQuestion],
    *,
    top_k: int = 5,
    settings: Settings | None = None,
) -> dict:
    """Split newly generated questions into those to keep and those to discard.

    Only neighbours close enough to be plausible are put to the model — comparing
    every new question against every stored one would be hundreds of calls for a
    result the search already narrows.

    Returns the questions to store, plus what was dropped and what merely resembles
    something and is being kept for review.
    """
    settings = settings or get_settings()

    keep: list[GeneratedQuestion] = []
    dropped: list[dict] = []
    flagged: list[dict] = []

    for question in candidates:
        text = combined_text(question.stem, question.explanation)
        neighbours = questions_repo.similar_by_embedding_text(
            text, settings.questions_vector_index_name, limit=top_k
        )

        verdict_for_drop = None
        for neighbour in neighbours:
            score = neighbour.get("score") or 0.0
            if score < settings.question_duplicate_score_threshold:
                continue
            verdict = judge_pair(
                question.model_dump(), neighbour, settings=settings
            )
            if not verdict.duplicate:
                continue
            entry = {
                "stem": question.stem,
                "duplicate_of": neighbour.get("question_id"),
                "duplicate_of_stem": neighbour.get("stem"),
                "score": score,
                "reason": verdict.reason,
            }
            if verdict.confident:
                verdict_for_drop = entry
                break
            flagged.append(entry)

        if verdict_for_drop is None:
            keep.append(question)
        else:
            dropped.append(verdict_for_drop)

    if dropped or flagged:
        logger.info(
            "Duplicate screening: %d dropped, %d flagged for review, %d kept",
            len(dropped),
            len(flagged),
            len(keep),
        )
    return {
        "keep": keep,
        "duplicates_dropped": dropped,
        "possible_duplicates": flagged,
    }
