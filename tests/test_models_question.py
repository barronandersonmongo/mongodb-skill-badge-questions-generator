"""Tests for app/models/question.py — the quiz question schemas.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from pydantic import ValidationError

from app.models.question import GeneratedQuestion, QuestionOption


def option(text: str, correct: bool = False) -> QuestionOption:
    return QuestionOption(text=text, is_correct=correct, rationale="because")


def question(**overrides) -> GeneratedQuestion:
    return GeneratedQuestion(
        **{
            "stem": "Which stage filters documents?",
            "options": [option("$match", True), option("$project"), option("$sort"), option("$limit")],
            "explanation": "$match filters.",
            "difficulty": "foundational",
            **overrides,
        }
    )


def test_a_question_carries_badges_and_categories_as_arrays():
    """
    Intent: One question may serve several badges and exercise several topics, and
        the whole collection is filtered on those two fields. Storing either as a
        single value would make a shared question findable under only one of them.
    Success: skill_badges and categories are lists and accept several values each.
    Feature: Question data model — questions belong to many badges and categories.
    """
    result = question(skill_badges=["atlas-search", "aggregation"], categories=["search", "indexing"])
    assert result.skill_badges == ["atlas-search", "aggregation"]
    assert result.categories == ["search", "indexing"]


def test_badges_and_categories_default_to_empty_rather_than_missing():
    """
    Intent: An extraction that omits these fields must still produce a storable
        question; a missing array would make the field absent in Mongo and break a
        filter that assumes a list.
    Success: A question built without them has empty lists, not None.
    Feature: Question data model — filterable fields always exist.
    """
    result = question()
    assert result.categories == [] and result.skill_badges == []
    assert result.source_urls == []


def test_an_option_records_why_it_is_right_or_wrong():
    """
    Intent: The point of the tool is question quality, which a reviewer can only
        judge if each distractor states the misconception it catches. A bare option
        list gives a reviewer nothing to review.
    Success: Every option carries a rationale alongside its text and correctness.
    Feature: Question authoring — per-option rationale for review.
    """
    result = question()
    assert all(o.rationale for o in result.options)
    assert [o.is_correct for o in result.options] == [True, False, False, False]


def test_difficulty_is_restricted_to_the_three_recognised_levels():
    """
    Intent: Difficulty is a filterable, comparable field. Free text would let
        "easy", "beginner" and "foundational" accumulate as separate values that no
        query can reconcile.
    Success: An unrecognised difficulty is rejected at validation.
    Feature: Question data model — controlled difficulty vocabulary.
    """
    with pytest.raises(ValidationError):
        question(difficulty="easy")


def test_a_malformed_option_list_is_accepted_by_the_schema():
    """
    Intent: Format rules are enforced after extraction, not during it. If the schema
        rejected a five-option question, one bad question would fail the parse and
        lose every good question generated in the same run.
    Success: A question with the wrong number of options and no correct answer still
        validates as a GeneratedQuestion.
    Feature: Question generation — a bad question must not discard a good batch.
    """
    result = question(options=[option("a"), option("b")])
    assert len(result.options) == 2
