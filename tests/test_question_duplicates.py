"""Tests for app/services/question_duplicates.py — screening new questions.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app.config import Settings
from app.models.question import GeneratedQuestion, QuestionOption
from app.services import badge_discovery, question_duplicates
from tests.fakes import FakeAnthropic, FakeParsedResponse

Verdict = question_duplicates.QuestionDuplicateVerdict


def option(text: str, correct: bool = False) -> QuestionOption:
    return QuestionOption(text=text, is_correct=correct, rationale="because")


def make(stem: str = "Which stage filters documents?", **overrides) -> GeneratedQuestion:
    return GeneratedQuestion(
        **{
            "stem": stem,
            "options": [option("$match", True), option("$project"), option("$sort"), option("$limit")],
            "explanation": "$match filters.",
            "difficulty": "foundational",
            "skill_badges": ["atlas-search"],
            **overrides,
        }
    )


STORED = {
    "question_id": "stored1",
    "stem": "Which aggregation stage removes documents from the pipeline?",
    "explanation": "$match filters.",
    "options": [
        {"text": "$match", "is_correct": True},
        {"text": "$unwind", "is_correct": False},
    ],
    "score": 0.93,
}


@pytest.fixture
def fake_client(monkeypatch):
    def install(parsed=None):
        client = FakeAnthropic(parsed=parsed)
        monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)
        return client

    return install


@pytest.fixture
def neighbours(monkeypatch):
    """Script what the vector index returns for each candidate."""
    def install(results: list[dict]):
        calls: list[tuple] = []

        def search(text, index_name, **kwargs):
            calls.append((text, index_name, kwargs))
            return results

        monkeypatch.setattr(
            question_duplicates.questions_repo, "similar_by_embedding_text", search
        )
        return calls

    return install


def test_a_confident_duplicate_is_discarded(fake_client, neighbours, settings):
    """
    Intent: A quiz that asks the same thing twice is the failure this whole screening
        exists to prevent, and the cheap moment to catch it is before the question is
        stored — afterwards someone has to notice it during review.
    Success: A question Claude confidently calls a duplicate is not kept, and is reported
        with what it duplicates.
    Feature: Question duplicates — confident duplicates are dropped before storage.
    """
    neighbours([STORED])
    fake_client(Verdict(duplicate=True, confident=True, reason="both test $match"))
    result = question_duplicates.screen_questions([make()], settings=settings)
    assert result["keep"] == []
    assert result["duplicates_dropped"][0]["duplicate_of"] == "stored1"
    assert result["duplicates_dropped"][0]["reason"] == "both test $match"


def test_an_unconfident_duplicate_is_kept_and_flagged(fake_client, neighbours, settings):
    """
    Intent: Discarding a question the model merely suspected loses work nobody reviewed.
        Showing an author both questions and letting them choose is the safe direction of
        error.
    Success: A duplicate verdict without confidence keeps the question and reports it for
        review.
    Feature: Question duplicates — only confident duplicates are discarded.
    """
    neighbours([STORED])
    fake_client(Verdict(duplicate=True, confident=False, reason="similar but arguable"))
    result = question_duplicates.screen_questions([make()], settings=settings)
    assert len(result["keep"]) == 1
    assert result["duplicates_dropped"] == []
    assert result["possible_duplicates"][0]["duplicate_of"] == "stored1"


def test_a_question_judged_different_is_kept_without_comment(fake_client, neighbours, settings):
    """
    Intent: Questions on one topic are naturally close in vector space. If proximity alone
        flagged them, every run would report noise and authors would stop reading the
        report.
    Success: A "not a duplicate" verdict keeps the question and reports nothing.
    Feature: Question duplicates — proximity alone is not a duplicate.
    """
    neighbours([STORED])
    fake_client(Verdict(duplicate=False, confident=True, reason="different knowledge"))
    result = question_duplicates.screen_questions([make()], settings=settings)
    assert len(result["keep"]) == 1
    assert result["duplicates_dropped"] == [] and result["possible_duplicates"] == []


def test_a_distant_neighbour_is_never_put_to_the_model(fake_client, neighbours, settings):
    """
    Intent: Every judgement costs a model call. Anything below the score floor is not a
        plausible duplicate, so paying to ask about it is waste — the floor exists to trim
        cost, not to decide.
    Success: A neighbour scoring below the threshold produces no API call.
    Feature: Question duplicates — the score floor trims cost.
    """
    neighbours([{**STORED, "score": 0.10}])
    client = fake_client(Verdict(duplicate=True, confident=True, reason="unused"))
    result = question_duplicates.screen_questions([make()], settings=settings)
    assert len(result["keep"]) == 1
    assert client.messages.parse_calls == []


def test_the_score_floor_is_configurable(fake_client, neighbours):
    """
    Intent: The right floor is empirical and differs from the badge one, because questions
        are longer and more specific. It has to be tunable without editing code once real
        scores are observed.
    Success: Lowering the threshold puts a previously ignored neighbour to the model.
    Feature: Question duplicates — tunable score floor.
    """
    neighbours([{**STORED, "score": 0.50}])
    client = fake_client(Verdict(duplicate=True, confident=True, reason="same"))
    settings = Settings(mongodb_uri="mongodb://test", question_duplicate_score_threshold=0.4)
    result = question_duplicates.screen_questions([make()], settings=settings)
    assert result["keep"] == []
    assert len(client.messages.parse_calls) == 1


def test_the_configured_index_is_the_one_searched(fake_client, neighbours, settings):
    """
    Intent: The index name is external state created by hand in Atlas. If this program
        searched a different name the screening would fail — or worse, silently return
        nothing and report every question as unique.
    Success: The search uses the index name from settings.
    Feature: Question duplicates — searches the configured vector index.
    """
    calls = neighbours([])
    fake_client(Verdict(duplicate=False, confident=True, reason="n/a"))
    question_duplicates.screen_questions([make()], settings=settings)
    assert calls[0][1] == settings.questions_vector_index_name


def test_the_candidate_is_searched_by_its_own_embedding_text(fake_client, neighbours, settings):
    """
    Intent: The index embeds the labelled stem-and-explanation block, so the query has to
        be composed the same way. Searching with a bare stem would compare unlike text and
        weaken every score.
    Success: The query text is the candidate's combined embedding text.
    Feature: Question duplicates — query matches how documents were embedded.
    """
    calls = neighbours([])
    fake_client(Verdict(duplicate=False, confident=True, reason="n/a"))
    question_duplicates.screen_questions([make()], settings=settings)
    assert calls[0][0] == "Question: Which stage filters documents?\nExplanation: $match filters."


def test_the_judge_sees_the_options_of_both_questions(fake_client, neighbours, settings):
    """
    Intent: Two stems can be near-identical while their options test different
        distinctions, and two different stems can reduce to one fact. Judging on stems
        alone would both merge distinct questions and miss real duplicates.
    Success: Both questions' options, and which is correct, reach the prompt.
    Feature: Question duplicates — judged on the whole question.
    """
    neighbours([STORED])
    client = fake_client(Verdict(duplicate=False, confident=True, reason="differs"))
    question_duplicates.screen_questions([make()], settings=settings)
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "$project" in prompt and "$unwind" in prompt
    assert "CORRECT" in prompt


def test_screening_stops_at_the_first_confident_duplicate(fake_client, neighbours, settings):
    """
    Intent: Once a question is known to duplicate something, further comparisons cannot
        change the outcome and only cost money.
    Success: With several close neighbours, only one judgement is made.
    Feature: Question duplicates — no needless API calls.
    """
    neighbours([STORED, {**STORED, "question_id": "stored2"}])
    client = fake_client(Verdict(duplicate=True, confident=True, reason="same"))
    question_duplicates.screen_questions([make()], settings=settings)
    assert len(client.messages.parse_calls) == 1


def test_an_empty_collection_produces_no_judgements(fake_client, neighbours, settings):
    """
    Intent: The first run against an empty collection has nothing to duplicate. Calling the
        model to compare against nothing is pure cost.
    Success: With no neighbours, the question is kept and no call is made.
    Feature: Question duplicates — no needless API calls.
    """
    neighbours([])
    client = fake_client(Verdict(duplicate=False, confident=True, reason="n/a"))
    result = question_duplicates.screen_questions([make()], settings=settings)
    assert len(result["keep"]) == 1
    assert client.messages.parse_calls == []


def test_a_truncated_judgement_is_reported_rather_than_assumed_unique(
    fake_client, neighbours, settings
):
    """
    Intent: A truncated judgement returns no structured output. Reading that as "not a
        duplicate" would let duplicates through while appearing to have screened them,
        which is worse than not screening at all.
    Success: Missing structured output raises, naming the stop reason.
    Feature: Question duplicates — an unanswered judgement is an error.
    """
    neighbours([STORED])
    fake_client(FakeParsedResponse(None, stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="max_tokens"):
        question_duplicates.screen_questions([make()], settings=settings)


def test_each_question_in_a_batch_is_screened(fake_client, neighbours, settings):
    """
    Intent: A run produces several questions and any of them may duplicate something.
        Screening only the first would leave the rest unchecked while the run reported
        itself as screened.
    Success: Every question in the batch is searched.
    Feature: Question duplicates — the whole batch is screened.
    """
    calls = neighbours([])
    fake_client(Verdict(duplicate=False, confident=True, reason="n/a"))
    question_duplicates.screen_questions(
        [make("First?"), make("Second?"), make("Third?")], settings=settings
    )
    assert len(calls) == 3


def test_judging_translates_missing_credentials_into_an_actionable_error(
    monkeypatch, neighbours, settings
):
    """
    Intent: Screening is the fourth API call in a run, so it can be where a credential
        problem first surfaces. The SDK's bare TypeError names its own constructor
        arguments and tells an operator nothing about which variable to set.
    Success: RuntimeError is raised naming ANTHROPIC_API_KEY.
    Feature: Question duplicates — missing credential diagnostics.
    """
    from tests.test_question_generation import SDK_AUTH_ERROR, _raising_client

    neighbours([STORED])
    _raising_client(monkeypatch, "parse", SDK_AUTH_ERROR)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        question_duplicates.screen_questions([make()], settings=settings)


def test_judging_failures_other_than_credentials_still_propagate(
    monkeypatch, neighbours, settings
):
    """
    Intent: The credential translator fronts every judgement, so a real API error (rate
        limit, overload, network) must pass through unchanged for the run to report it and
        keep its questions — rather than being mislabelled as a missing key.
    Success: The original exception propagates from screen_questions.
    Feature: Question duplicates — failure handling.
    """
    from tests.test_question_generation import _raising_client

    neighbours([STORED])
    _raising_client(monkeypatch, "parse", ConnectionError("connection reset by peer"))
    with pytest.raises(ConnectionError, match="connection reset"):
        question_duplicates.screen_questions([make()], settings=settings)
