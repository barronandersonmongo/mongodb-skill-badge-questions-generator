"""Quiz question shapes.

`GeneratedQuestion` is what Claude returns; `QuestionDoc` is what we store.

The option list is deliberately *not* constrained to four options with exactly
one correct answer at the schema level. A generation that breaks the format is a
result worth showing the author — `question_generation.split_well_formed` sorts
those out and reports why — whereas a schema violation would fail the whole batch
and lose the questions that were fine.

`categories` and `skill_badges` are arrays: one question may serve several
badges, and is filtered on either.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DIFFICULTIES = ("foundational", "intermediate", "advanced")

# Labels inside the combined text. They are part of the embedded string on
# purpose: an unlabelled concatenation reads as one run-on sentence, and the
# labels give the embedding model the same cue a reader gets about which part is
# the question and which is the reasoning behind its answer.
STEM_LABEL = "Question"
EXPLANATION_LABEL = "Explanation"


def combined_text(stem: str, explanation: str | None) -> str:
    """The stem and explanation as one labelled block, for embedding.

    This is the field an Atlas Vector Search index with autoEmbed points at, so
    Atlas embeds it — this program stores no vectors, exactly as with badge
    descriptions.

    An absent explanation drops its section rather than leaving a dangling label,
    which would otherwise be embedded as meaningful text.
    """
    parts = [f"{STEM_LABEL}: {(stem or '').strip()}"]
    if explanation and explanation.strip():
        parts.append(f"{EXPLANATION_LABEL}: {explanation.strip()}")
    return "\n".join(parts)


class QuestionOption(BaseModel):
    text: str = Field(description="The answer option as a candidate would read it")
    is_correct: bool = Field(description="True for the single correct option")
    rationale: str = Field(
        description="Why this option is correct, or the misconception it represents"
    )


class GeneratedQuestion(BaseModel):
    stem: str = Field(description="The question itself, answerable without the options")
    options: list[QuestionOption] = Field(
        description="Four options, exactly one of them correct"
    )
    explanation: str = Field(
        description="Short explanation of the correct answer for a reviewer"
    )
    difficulty: Literal["foundational", "intermediate", "advanced"] = Field(
        description="How demanding the question is for someone earning the badge"
    )
    categories: list[str] = Field(
        default_factory=list,
        description="Topic areas the question exercises, e.g. ['aggregation']",
    )
    skill_badges: list[str] = Field(
        default_factory=list,
        description="Slugs of the skill badges this question belongs to",
    )
    source_urls: list[str] = Field(
        default_factory=list,
        description="URLs of the material this question was written from",
    )


class GeneratedQuestions(BaseModel):
    questions: list[GeneratedQuestion]


class QuestionBadges(BaseModel):
    """Which badges one question belongs to, decided by reviewing the question."""

    question_index: int = Field(
        description="1-based position of the question in the list that was reviewed"
    )
    skill_badges: list[str] = Field(
        description="Slugs of every badge whose subject matter this question tests"
    )
    reason: str = Field(
        default="",
        description="Why the additional badges apply, for the run's audit trail",
    )


class QuestionBadgeAttributions(BaseModel):
    attributions: list[QuestionBadges]


class QuestionDoc(GeneratedQuestion):
    question_id: str
    created_at: datetime
    generation_run_id: str
    # Written on insert by the repository; the path a vector index points at.
    embedding_text: str = ""
