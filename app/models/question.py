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


class QuestionDoc(GeneratedQuestion):
    question_id: str
    created_at: datetime
    generation_run_id: str
    status: Literal["draft", "approved", "rejected"] = "draft"
