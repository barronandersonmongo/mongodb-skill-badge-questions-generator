"""Tests for app/services/question_generation.py — the two Claude passes.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from dataclasses import replace

import pytest

from app.models.question import (
    GeneratedQuestion,
    GeneratedQuestions,
    QuestionBadgeAttributions,
    QuestionBadges,
    QuestionOption,
)
from app.services import badge_discovery, question_generation
from tests.fakes import FakeAnthropic, FakeBlock, FakeMessage, FakeParsedResponse

BADGE = {
    "slug": "atlas-search",
    "name": "Atlas Search",
    "description": "Covers Atlas Search indexes and queries.",
    "categories": ["search"],
    "source_urls": ["https://learn.mongodb.com/atlas-search"],
    "mongodb_url": "https://learn.mongodb.com/badge/atlas-search",
}


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


ONE_QUESTION = GeneratedQuestions(questions=[make()])

# A run makes two parse() calls against different schemas — extraction, then badge
# attribution — so a full-run test scripts an answer for each.
NO_EXTRA_BADGES = QuestionBadgeAttributions(attributions=[])


def full_run(questions=ONE_QUESTION, attributions=NO_EXTRA_BADGES) -> dict:
    return {
        GeneratedQuestions: questions,
        QuestionBadgeAttributions: attributions,
    }


@pytest.fixture
def fake_client(monkeypatch):
    def install(stream_messages=None, parsed=None, parsed_by_format=None):
        client = FakeAnthropic(
            stream_messages=stream_messages,
            parsed=parsed,
            parsed_by_format=parsed_by_format,
        )
        monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)
        return client

    return install


# --- the authoring pass ---


def test_the_selected_badges_scope_the_request(fake_client, settings):
    """
    Intent: A badge is what makes a question in scope; the model cannot stay inside a
        syllabus it was not told about. The badge's title, coverage and topic areas
        must all reach the prompt.
    Success: The authoring prompt names the badge, its description and its categories.
    Feature: Question generation — questions are scoped by skill badge.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions([BADGE], 3, settings=settings)
    prompt = client.messages.stream_calls[0]["messages"][0]["content"]
    assert "Atlas Search" in prompt
    assert "atlas-search" in prompt
    assert "Atlas Search indexes and queries" in prompt
    assert "search" in prompt


def test_the_badges_reference_links_are_offered_as_sources(fake_client, settings):
    """
    Intent: Curated reference links are the badge's own vetted material. Withholding
        them would leave the model to find sources itself, which is how off-syllabus
        and outdated questions get written.
    Success: The badge's curated link and its badge page reach the prompt.
    Feature: Question generation — grounded in the badge's curated sources.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions([BADGE], 1, settings=settings)
    prompt = client.messages.stream_calls[0]["messages"][0]["content"]
    assert "https://learn.mongodb.com/atlas-search" in prompt
    assert "https://learn.mongodb.com/badge/atlas-search" in prompt


def test_the_requested_number_of_questions_reaches_the_prompt(fake_client, settings):
    """
    Intent: The author chooses how many questions a run produces; a prompt that
        ignored the count would make the control decorative.
    Success: The count appears in the authoring prompt.
    Feature: Question generation — the author sets the batch size.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions([BADGE], 7, settings=settings)
    assert "7" in client.messages.stream_calls[0]["messages"][0]["content"]


def test_pasted_source_material_is_preferred_over_what_claude_finds(fake_client, settings):
    """
    Intent: Internal training material is the best source available, and this process
        cannot fetch it — the author pastes it in. It must reach the prompt marked as
        authoritative, or the model will treat it as one source among many.
    Success: The material appears in the prompt, told to be preferred.
    Feature: Question generation — author-supplied source material.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions(
        [BADGE], 1, source_material="Lesson 4: search indexes are async.", settings=settings
    )
    prompt = client.messages.stream_calls[0]["messages"][0]["content"]
    assert "Lesson 4: search indexes are async." in prompt
    assert "Prefer it" in prompt


def test_extra_instructions_reach_the_prompt(fake_client, settings):
    """
    Intent: An author steering a run ("advanced only", "focus on $lookup") must
        actually influence it, otherwise the field misleads.
    Success: The instruction text appears in the authoring prompt.
    Feature: Question generation — per-run author instructions.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions(
        [BADGE], 1, extra_instructions="advanced only", settings=settings
    )
    assert "advanced only" in client.messages.stream_calls[0]["messages"][0]["content"]


def test_authoring_can_search_and_fetch_the_web(fake_client, settings):
    """
    Intent: Question quality depends on real documentation, not the model's memory of
        it. The authoring turn must be given the server-side search and fetch tools,
        configured for this environment.
    Success: Both tools are passed, using the configured tool versions and model.
    Feature: Question generation — grounded in current MongoDB documentation.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions([BADGE], 1, settings=settings)
    call = client.messages.stream_calls[0]
    assert [t["type"] for t in call["tools"]] == [
        settings.web_search_tool,
        settings.web_fetch_tool,
    ]
    assert call["model"] == settings.model


def test_authoring_streams_rather_than_blocking(fake_client, settings):
    """
    Intent: An authoring turn with web search runs for minutes. A non-streaming
        request would risk a read timeout and lose the whole run.
    Success: The authoring pass calls stream(), never parse().
    Feature: Question generation — long-running turn survives.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions([BADGE], 1, settings=settings)
    assert len(client.messages.stream_calls) == 1
    assert client.messages.parse_calls == []


def test_authoring_resumes_a_paused_turn_and_keeps_both_halves(fake_client, settings):
    """
    Intent: Server-side web search ends a turn with stop_reason "pause_turn". Treating
        that as the end would silently return a half-written batch.
    Success: The turn is re-sent and the returned draft contains text from both parts.
    Feature: Question generation — paused turns are resumed.
    """
    client = fake_client(
        stream_messages=[
            FakeMessage("first half", stop_reason="pause_turn"),
            FakeMessage("second half"),
        ]
    )
    draft = question_generation.author_questions([BADGE], 1, settings=settings)
    assert "first half" in draft and "second half" in draft
    assert len(client.messages.stream_calls) == 2


def test_authoring_gives_up_after_repeated_pauses(fake_client, settings):
    """
    Intent: A turn that never stops pausing would loop forever, burning tokens with
        nothing to show. The resume budget must be finite and the failure explicit.
    Success: A run that always pauses raises rather than looping.
    Feature: Question generation — bounded resume budget.
    """
    fake_client(stream_messages=[FakeMessage("x", stop_reason="pause_turn")] * 6)
    with pytest.raises(RuntimeError, match="still paused"):
        question_generation.author_questions([BADGE], 1, settings=settings)


def test_a_refusal_is_raised_rather_than_returned_as_an_empty_draft(fake_client, settings):
    """
    Intent: If Claude declines, an empty draft would extract to zero questions and be
        reported as "the run produced nothing", hiding the real reason.
    Success: A refusal raises an error naming the refusal.
    Feature: Question generation — refusals are surfaced, not swallowed.
    """
    fake_client(
        stream_messages=[FakeMessage("", stop_reason="refusal", stop_details="policy")]
    )
    with pytest.raises(RuntimeError, match="declined"):
        question_generation.author_questions([BADGE], 1, settings=settings)


def test_authoring_ignores_non_text_blocks(fake_client, settings):
    """
    Intent: A turn using tools returns tool-use and search-result blocks with no text
        attribute. Reading text off every block would crash the run.
    Success: The draft contains only the text blocks.
    Feature: Question generation — tool-using turns are handled.
    """
    fake_client(
        stream_messages=[
            FakeMessage(
                content=[FakeBlock("server_tool_use"), FakeBlock("text", "the draft")]
            )
        ]
    )
    assert question_generation.author_questions([BADGE], 1, settings=settings) == "the draft"


# --- the extraction pass ---


def test_extraction_returns_validated_questions(fake_client, settings):
    """
    Intent: Prose is not storable. The draft must come back as validated records, or
        malformed output would reach MongoDB and the export.
    Success: extract_questions returns the parsed question objects.
    Feature: Question generation — schema-validated extraction.
    """
    fake_client(parsed=ONE_QUESTION)
    result = question_generation.extract_questions("1. Which stage…", settings=settings)
    assert [q.stem for q in result] == ["Which stage filters documents?"]


def test_extraction_passes_the_schema_and_the_draft(fake_client, settings):
    """
    Intent: Extraction must be a faithful conversion of the draft against the question
        schema — not a second, unanchored authoring pass.
    Success: The draft text and the GeneratedQuestions schema both reach the call.
    Feature: Question generation — extraction is anchored to the draft.
    """
    client = fake_client(parsed=ONE_QUESTION)
    question_generation.extract_questions("the draft text", settings=settings)
    call = client.messages.parse_calls[0]
    assert call["output_format"] is GeneratedQuestions
    assert "the draft text" in call["messages"][0]["content"]


def test_extraction_does_not_search_the_web_again(fake_client, settings):
    """
    Intent: Extraction is a formatting step. Giving it tools would let it research and
        rewrite, so the stored questions would no longer be the reviewed draft.
    Success: No tools are passed to the extraction call.
    Feature: Question generation — extraction reformats, it does not re-author.
    """
    client = fake_client(parsed=ONE_QUESTION)
    question_generation.extract_questions("draft", settings=settings)
    assert "tools" not in client.messages.parse_calls[0]


def test_extraction_failure_is_reported_rather_than_treated_as_no_questions(
    fake_client, settings
):
    """
    Intent: A truncated or refused extraction returns no structured output. Reading
        that as "zero questions" would silently discard a completed authoring turn.
    Success: A missing parsed output raises an error naming the stop reason.
    Feature: Question generation — truncated extraction is an error.
    """
    fake_client(parsed=FakeParsedResponse(None, stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="max_tokens"):
        question_generation.extract_questions("draft", settings=settings)


# --- format validation ---


def test_a_well_formed_question_is_accepted():
    """
    Intent: The validator is the gate every question passes; if it rejected a correct
        question nothing could ever be stored.
    Success: A four-option, single-answer question reports no problem.
    Feature: Question validation — the four-option format.
    """
    assert question_generation.format_problem(make()) is None


@pytest.mark.parametrize(
    "count", [2, 3, 5], ids=["two-options", "three-options", "five-options"]
)
def test_a_question_without_exactly_four_options_is_rejected(count):
    """
    Intent: The quiz format is fixed at four options, so a question with any other
        number cannot be published however good its subject matter. Storing it would
        push the discovery of the problem onto a reviewer.
    Success: An option count other than four is reported as a problem.
    Feature: Question validation — exactly four options.
    """
    options = [option("first", True)] + [option(f"wrong {i}") for i in range(count - 1)]
    problem = question_generation.format_problem(make(options=options))
    assert problem is not None and "expected 4" in problem


@pytest.mark.parametrize("correct", [0, 2], ids=["none-correct", "two-correct"])
def test_a_question_must_have_exactly_one_correct_answer(correct):
    """
    Intent: These are single-answer questions. Two correct options make the question
        unfair and none makes it unanswerable — both are unpublishable, and neither is
        obvious from a listing.
    Success: Anything other than exactly one correct option is reported as a problem.
    Feature: Question validation — exactly one correct answer.
    """
    options = [option(f"opt {i}", i < correct) for i in range(4)]
    problem = question_generation.format_problem(make(options=options))
    assert problem is not None and "correct" in problem


def test_repeated_options_are_rejected():
    """
    Intent: Two identical options give the candidate a free elimination and reveal
        careless generation. Case and surrounding space do not make them different.
    Success: A question with a duplicated option is reported as a problem.
    Feature: Question validation — distinct options.
    """
    options = [option("$match", True), option(" $MATCH "), option("$sort"), option("$limit")]
    problem = question_generation.format_problem(make(options=options))
    assert problem is not None and "same" in problem


def test_an_empty_option_is_rejected():
    """
    Intent: A blank option is not an answer a candidate can choose; it is a generation
        failure that would render as an empty bullet.
    Success: A question with a blank option is reported as a problem.
    Feature: Question validation — every option says something.
    """
    options = [option("$match", True), option("   "), option("$sort"), option("$limit")]
    problem = question_generation.format_problem(make(options=options))
    assert problem is not None and "no text" in problem


def test_an_empty_stem_is_rejected():
    """
    Intent: A question with no stem asks nothing, so its options cannot be judged. It
        must never reach review.
    Success: A blank stem is reported as a problem.
    Feature: Question validation — the stem asks something.
    """
    problem = question_generation.format_problem(make(stem="   "))
    assert problem is not None and "stem" in problem


def test_a_bad_question_does_not_discard_the_good_ones_in_its_batch():
    """
    Intent: Generation is expensive and non-deterministic. Failing a whole run because
        one question came back malformed would throw away work that was fine.
    Success: The good question is kept and the bad one is returned separately with a
        reason.
    Feature: Question generation — partial batches are kept.
    """
    bad = make("Broken?", options=[option("a", True), option("b")])
    kept, rejected = question_generation.split_well_formed([make(), bad])
    assert [q.stem for q in kept] == ["Which stage filters documents?"]
    assert rejected[0]["stem"] == "Broken?"
    assert "expected 4" in rejected[0]["problem"]


# --- the whole run ---


def test_a_run_stores_the_questions_it_generated(fake_client, fake_collection, fake_questions, settings):
    """
    Intent: The run is the feature: authoring, extraction, validation and storage must
        be wired together, or the screen reports a successful run with nothing saved.
    Success: generate_questions stores the question and reports what it stored.
    Feature: Question generation — end-to-end run.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(stream_messages=[FakeMessage("draft")], parsed_by_format=full_run())
    result = question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert result["inserted"] == 1
    assert fake_questions.docs[0]["stem"] == "Which stage filters documents?"
    assert result["generated"] == 1 and result["requested"] == 1


def test_a_run_reports_the_questions_it_discarded(fake_client, fake_collection, fake_questions, settings):
    """
    Intent: A run that quietly stored one of five questions would look like a model
        with little to say. The author must be told how many were discarded and why,
        so a prompt problem is visible.
    Success: The malformed question is absent from storage and present in the run's
        rejected list with its reason.
    Feature: Question generation — discards are reported, not hidden.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    bad = make("Broken?", options=[option("a", True), option("b")])
    fake_client(
        stream_messages=[FakeMessage("draft")],
        parsed_by_format=full_run(GeneratedQuestions(questions=[make(), bad])),
    )
    result = question_generation.generate_questions(["atlas-search"], 2, settings=settings)
    assert result["inserted"] == 1
    assert [r["stem"] for r in result["rejected"]] == ["Broken?"]
    assert len(fake_questions.docs) == 1


def test_the_draft_is_kept_so_a_run_can_be_audited(fake_client, fake_collection, fake_questions, settings):
    """
    Intent: When a run produces weak questions the draft is the only evidence of what
        the model actually wrote before extraction reshaped it. Discarding it makes a
        bad run impossible to diagnose.
    Success: The run summary carries the draft text.
    Feature: Question generation — auditable runs.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(
        stream_messages=[FakeMessage("the raw draft")], parsed_by_format=full_run()
    )
    result = question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert result["draft"] == "the raw draft"


def test_generating_for_no_badge_is_refused(fake_collection, fake_questions, settings):
    """
    Intent: Badges are what keep questions in scope. A run with no badge would have no
        syllabus at all, so it must be refused before any tokens are spent.
    Success: An empty badge list raises before any model call.
    Feature: Question generation — a badge scope is required.
    """
    with pytest.raises(ValueError, match="at least one skill badge"):
        question_generation.generate_questions([], 3, settings=settings)


def test_generating_for_an_unknown_badge_is_refused(fake_collection, fake_questions, settings):
    """
    Intent: A badge slug that is not in the collection carries no description, links or
        topic areas, so the run would be unscoped while appearing scoped. Naming the
        bad slug is what makes the mistake fixable.
    Success: An unknown slug raises an error naming it, and nothing is stored.
    Feature: Question generation — badge scope must exist.
    """
    with pytest.raises(ValueError, match="made-up-badge"):
        question_generation.generate_questions(["made-up-badge"], 1, settings=settings)


def test_a_question_is_only_attributed_to_badges_that_exist(
    fake_client, fake_collection, fake_questions, settings
):
    """
    Intent: skill_badges is what the whole collection is filtered by. A badge slug the
        model invented — or mis-spelled — would make the question unfindable under that
        name while looking correctly tagged. Note this checks existence, not membership
        of the request: a question may legitimately belong to badges beyond the ones it
        was written for.
    Success: A slug matching no stored badge is dropped and the real one kept.
    Feature: Question generation — badge slugs must name a real badge.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(
        stream_messages=[FakeMessage("draft")],
        parsed_by_format=full_run(
            GeneratedQuestions(
                questions=[make(skill_badges=["atlas-search", "invented-badge"])]
            )
        ),
    )
    question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert fake_questions.docs[0]["skill_badges"] == ["atlas-search"]


def test_a_question_tagged_with_no_badge_falls_back_to_the_request(
    fake_client, fake_collection, fake_questions, settings
):
    """
    Intent: A question the model forgot to tag would be stored with an empty badge
        list and never appear under any badge filter — invisible work. The badges the
        run was scoped to are the correct answer.
    Success: An untagged question is stored against the requested badge.
    Feature: Question generation — every question is findable by badge.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(
        stream_messages=[FakeMessage("draft")],
        parsed_by_format=full_run(GeneratedQuestions(questions=[make(skill_badges=[])])),
    )
    question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert fake_questions.docs[0]["skill_badges"] == ["atlas-search"]


# --- failure handling ---


SDK_AUTH_ERROR = TypeError(
    "Could not resolve authentication method. Expected one of api_key, auth_token, "
    "or credentials to be set."
)


def _raising_client(monkeypatch, method: str, error: Exception) -> None:
    class Raising:
        def __getattr__(self, name):
            def call(**kwargs):
                raise error

            if name == method:
                return call
            raise AttributeError(name)

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)


def test_authoring_translates_missing_credentials_into_an_actionable_error(
    monkeypatch, settings
):
    """
    Intent: The SDK resolves credentials lazily and raises a bare TypeError naming its
        own constructor arguments, which tells an author nothing. An unset key is the
        most likely first-run failure, so the run must say which variable to set.
    Success: RuntimeError is raised naming ANTHROPIC_API_KEY, with the SDK's TypeError
        kept as the cause.
    Feature: Question generation — missing credential diagnostics.
    """
    _raising_client(monkeypatch, "stream", SDK_AUTH_ERROR)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY") as caught:
        question_generation.author_questions([BADGE], 1, settings=settings)
    assert isinstance(caught.value.__cause__, TypeError)


def test_extraction_translates_missing_credentials_into_an_actionable_error(
    monkeypatch, settings
):
    """
    Intent: Extraction is a second API call, so it can be the first place a credential
        problem surfaces. It must give the same actionable message rather than the
        SDK's.
    Success: RuntimeError is raised naming ANTHROPIC_API_KEY.
    Feature: Question generation — missing credential diagnostics.
    """
    _raising_client(monkeypatch, "parse", SDK_AUTH_ERROR)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        question_generation.extract_questions("draft", settings=settings)


def test_authoring_failures_other_than_credentials_still_propagate(monkeypatch, settings):
    """
    Intent: The credential translator sits in front of every authoring failure, so a
        real API error (rate limit, overload, network) must pass through unchanged
        rather than being relabelled as a missing key.
    Success: The original exception propagates from the authoring pass.
    Feature: Question generation — failure handling.
    """
    _raising_client(monkeypatch, "stream", ConnectionError("connection reset by peer"))
    with pytest.raises(ConnectionError, match="connection reset"):
        question_generation.author_questions([BADGE], 1, settings=settings)


def test_extraction_failures_other_than_credentials_still_propagate(monkeypatch, settings):
    """
    Intent: The same translator fronts extraction, and an authoring turn has already
        been paid for by that point — a relabelled error would send the author looking
        for a credential problem that is not there.
    Success: The original exception propagates from extract_questions.
    Feature: Question generation — failure handling.
    """
    _raising_client(monkeypatch, "parse", ConnectionError("connection reset by peer"))
    with pytest.raises(ConnectionError, match="connection reset"):
        question_generation.extract_questions("draft", settings=settings)


# --- badge attribution: a question belongs to every badge it tests ---

CATALOG = [
    BADGE,
    {
        "slug": "aggregation",
        "name": "Aggregation",
        "description": "Covers the aggregation pipeline.",
        "categories": ["aggregation"],
    },
    {
        "slug": "indexing",
        "name": "Indexing",
        "description": "Covers index design.",
        "categories": ["indexing"],
    },
]


def attributed(index: int, slugs: list[str], reason: str = "also tests pipelines"):
    return QuestionBadgeAttributions(
        attributions=[
            QuestionBadges(question_index=index, skill_badges=slugs, reason=reason)
        ]
    )


def test_a_question_gains_the_other_badges_it_tests(fake_client, settings):
    """
    Intent: Skills overlap, so a question written for one badge often tests others. Left
        tagged only with the badge it was requested for, it is invisible to an author
        working through any badge it also covers — who then has the same question
        written a second time.
    Success: A badge the attribution pass names is added to the question.
    Feature: Question attribution — questions belong to every badge they test.
    """
    question = make(skill_badges=["atlas-search"])
    fake_client(parsed=attributed(1, ["atlas-search", "aggregation"]))
    result = question_generation.attribute_badges(
        [question], CATALOG, ["atlas-search"], settings=settings
    )
    assert question.skill_badges == ["atlas-search", "aggregation"]
    assert result["cross_tagged"] == 1


def test_the_badges_a_question_was_written_for_are_never_dropped(fake_client, settings):
    """
    Intent: This pass exists to widen a question's reach, so it must never narrow it. An
        attribution that omitted the requested badge would remove the question from the
        very list the author generated it for.
    Success: The requested badge survives an attribution that does not mention it.
    Feature: Question attribution — attribution only ever adds.
    """
    question = make(skill_badges=["atlas-search"])
    fake_client(parsed=attributed(1, ["aggregation"]))
    question_generation.attribute_badges(
        [question], CATALOG, ["atlas-search"], settings=settings
    )
    assert question.skill_badges == ["atlas-search", "aggregation"]


def test_the_question_and_its_answers_are_both_reviewed(fake_client, settings):
    """
    Intent: What a question actually tests is often visible only in what separates the
        correct option from the wrong ones — a stem alone reads as more general than the
        question is. Sending only stems would produce attribution by keyword.
    Success: The stem, every option, the correct/wrong marking and the explanation all
        reach the attribution prompt.
    Feature: Question attribution — decided from the whole question.
    """
    client = fake_client(parsed=attributed(1, ["aggregation"]))
    question_generation.attribute_badges(
        [make()], CATALOG, ["atlas-search"], settings=settings
    )
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    for option in ("$match", "$project", "$sort", "$limit"):
        assert option in prompt
    assert "correct" in prompt and "wrong" in prompt
    assert "$match filters." in prompt


def test_every_badge_is_offered_as_a_candidate(fake_client, settings):
    """
    Intent: The pass can only tag a question with a badge it was told about, so the whole
        catalog must be offered — not just the badges the run was scoped to, which would
        make cross-badge attribution impossible by construction.
    Success: Every catalog slug appears in the attribution prompt.
    Feature: Question attribution — the whole badge catalog is considered.
    """
    client = fake_client(parsed=attributed(1, ["aggregation"]))
    question_generation.attribute_badges(
        [make()], CATALOG, ["atlas-search"], settings=settings
    )
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    for badge in CATALOG:
        assert badge["slug"] in prompt


def test_an_invented_badge_slug_is_ignored(fake_client, settings):
    """
    Intent: A slug that matches no stored badge makes a question unfindable under that
        name while looking correctly tagged — and this pass is the one place a model is
        asked to produce slugs freely.
    Success: A slug outside the catalog is not added.
    Feature: Question attribution — badge slugs must name a real badge.
    """
    question = make(skill_badges=["atlas-search"])
    fake_client(parsed=attributed(1, ["aggregation", "not-a-real-badge"]))
    question_generation.attribute_badges(
        [question], CATALOG, ["atlas-search"], settings=settings
    )
    assert question.skill_badges == ["atlas-search", "aggregation"]


def test_a_badge_is_not_recorded_twice(fake_client, settings):
    """
    Intent: An attribution naturally repeats the badges the question was written for. If
        those were appended blindly the stored array would contain duplicates, which
        would show as repeated tags on screen and repeat the question in an export
        filtered across badges.
    Success: The badge list has no duplicates.
    Feature: Question attribution — distinct badge list.
    """
    question = make(skill_badges=["atlas-search"])
    fake_client(parsed=attributed(1, ["atlas-search", "atlas-search", "aggregation"]))
    question_generation.attribute_badges(
        [question], CATALOG, ["atlas-search"], settings=settings
    )
    assert question.skill_badges == ["atlas-search", "aggregation"]


def test_an_attribution_for_a_question_that_was_not_sent_is_ignored(fake_client, settings):
    """
    Intent: Questions are addressed by position. An out-of-range index would either crash
        the run or, worse, retag a different question than the model meant.
    Success: An index beyond the batch leaves every question untouched.
    Feature: Question attribution — positional answers are bounds-checked.
    """
    question = make(skill_badges=["atlas-search"])
    fake_client(parsed=attributed(7, ["aggregation"]))
    result = question_generation.attribute_badges(
        [question], CATALOG, ["atlas-search"], settings=settings
    )
    assert question.skill_badges == ["atlas-search"]
    assert result["cross_tagged"] == 0


def test_each_question_is_attributed_independently(fake_client, settings):
    """
    Intent: A batch is catalogued in one call for cost, so the answers must be matched
        back to the right questions. Applying one question's badges to another would
        mis-file work in a way no reviewer could see.
    Success: Two questions receive only their own additional badges.
    Feature: Question attribution — answers map to the right question.
    """
    first, second = make("First?", skill_badges=["atlas-search"]), make(
        "Second?", skill_badges=["atlas-search"]
    )
    fake_client(
        parsed=QuestionBadgeAttributions(
            attributions=[
                QuestionBadges(question_index=1, skill_badges=["aggregation"], reason="a"),
                QuestionBadges(question_index=2, skill_badges=["indexing"], reason="b"),
            ]
        )
    )
    question_generation.attribute_badges(
        [first, second], CATALOG, ["atlas-search"], settings=settings
    )
    assert first.skill_badges == ["atlas-search", "aggregation"]
    assert second.skill_badges == ["atlas-search", "indexing"]


def test_the_reason_for_an_added_badge_is_reported(fake_client, settings):
    """
    Intent: Cross-tagging is a judgement an author may disagree with. Recording why a
        badge was added is what lets them audit an over-eager run rather than only
        seeing the result.
    Success: The run reports the added badges with the stated reason.
    Feature: Question attribution — auditable decisions.
    """
    fake_client(parsed=attributed(1, ["aggregation"], reason="tests pipeline stages"))
    result = question_generation.attribute_badges(
        [make(skill_badges=["atlas-search"])], CATALOG, ["atlas-search"], settings=settings
    )
    assert result["attribution_reasons"][0]["added"] == ["aggregation"]
    assert result["attribution_reasons"][0]["reason"] == "tests pipeline stages"


def test_attribution_costs_no_call_when_there_is_nothing_to_catalogue(fake_client, settings):
    """
    Intent: A run whose questions were all discarded, or a collection with no badges, has
        nothing to attribute. Calling the model anyway spends money to answer a question
        about an empty list.
    Success: No API call is made for an empty batch or an empty catalog.
    Feature: Question attribution — no needless API calls.
    """
    client = fake_client(parsed=NO_EXTRA_BADGES)
    assert question_generation.attribute_badges([], CATALOG, [], settings=settings)[
        "cross_tagged"
    ] == 0
    question_generation.attribute_badges([make()], [], ["atlas-search"], settings=settings)
    assert client.messages.parse_calls == []


def test_a_truncated_attribution_is_reported_rather_than_treated_as_no_overlap(
    fake_client, settings
):
    """
    Intent: A truncated or refused attribution returns no structured output. Reading that
        as "this question overlaps nothing" would silently under-tag a whole batch, which
        looks identical to a correct result.
    Success: A missing parsed output raises an error naming the stop reason.
    Feature: Question attribution — truncated output is an error.
    """
    fake_client(parsed=FakeParsedResponse(None, stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="max_tokens"):
        question_generation.attribute_badges(
            [make()], CATALOG, ["atlas-search"], settings=settings
        )


def test_a_failed_attribution_does_not_lose_the_generated_questions(
    fake_client, fake_collection, fake_questions, settings
):
    """
    Intent: By the time attribution runs, the authoring turn has been paid for and the
        questions are good. Discarding them because a follow-up cataloguing step hit a
        rate limit would throw away the expensive work — the questions must still be
        stored under the badges they were written for.
    Success: The run stores the question, reports zero cross-tagged, and names the
        attribution failure.
    Feature: Question generation — attribution failure never loses a batch.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(
        stream_messages=[FakeMessage("draft")],
        parsed_by_format={
            GeneratedQuestions: ONE_QUESTION,
            QuestionBadgeAttributions: FakeParsedResponse(None, stop_reason="max_tokens"),
        },
    )
    result = question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert result["inserted"] == 1
    assert fake_questions.docs[0]["skill_badges"] == ["atlas-search"]
    assert result["cross_tagged"] == 0
    assert "max_tokens" in result["attribution_error"]


def test_a_run_stores_the_extra_badges_a_question_earned(
    fake_client, fake_collection, fake_questions, settings
):
    """
    Intent: Attribution is only worth anything if the widened badge list is what gets
        stored — the array MongoDB is queried on. Deciding the extra badges and then
        saving the original list would be an invisible no-op.
    Success: The stored question carries both the requested and the attributed badge.
    Feature: Question generation — attributed badges are persisted.
    """
    fake_collection.docs.extend(
        [{**BADGE, "status": "approved"}, {**CATALOG[1], "status": "approved"}]
    )
    fake_client(
        stream_messages=[FakeMessage("draft")],
        parsed_by_format=full_run(
            attributions=attributed(1, ["atlas-search", "aggregation"])
        ),
    )
    result = question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert fake_questions.docs[0]["skill_badges"] == ["atlas-search", "aggregation"]
    assert result["cross_tagged"] == 1


def test_the_topic_areas_a_question_carries_reach_the_attribution_pass(fake_client, settings):
    """
    Intent: A question's own topic areas are the clearest signal of which badges it
        crosses into — a question tagged "indexing" is a candidate for the indexing badge
        whatever it was written for. Withholding them makes the pass work from prose
        alone.
    Success: The question's categories appear in the attribution prompt.
    Feature: Question attribution — topic areas inform the decision.
    """
    client = fake_client(parsed=attributed(1, ["aggregation"]))
    question_generation.attribute_badges(
        [make(categories=["aggregation", "indexing"])],
        CATALOG,
        ["atlas-search"],
        settings=settings,
    )
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "aggregation, indexing" in prompt


def test_attribution_translates_missing_credentials_into_an_actionable_error(
    monkeypatch, settings
):
    """
    Intent: Attribution is a third API call, so it can be where a credential problem first
        surfaces. It must give the same actionable message as the other two rather than
        the SDK's bare TypeError about its own constructor arguments.
    Success: RuntimeError is raised naming ANTHROPIC_API_KEY.
    Feature: Question attribution — missing credential diagnostics.
    """
    _raising_client(monkeypatch, "parse", SDK_AUTH_ERROR)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        question_generation.attribute_badges(
            [make()], CATALOG, ["atlas-search"], settings=settings
        )


def test_attribution_failures_other_than_credentials_still_propagate(monkeypatch, settings):
    """
    Intent: The credential translator fronts every attribution failure, so a real API
        error (rate limit, overload, network) must pass through unchanged for the caller
        to decide about — which is what lets a run keep its questions and report the
        reason rather than mislabelling it as a missing key.
    Success: The original exception propagates from attribute_badges.
    Feature: Question attribution — failure handling.
    """
    _raising_client(monkeypatch, "parse", ConnectionError("connection reset by peer"))
    with pytest.raises(ConnectionError, match="connection reset"):
        question_generation.attribute_badges(
            [make()], CATALOG, ["atlas-search"], settings=settings
        )


def test_generation_does_not_screen_for_duplicates(
    fake_client, fake_collection, fake_questions, settings
):
    """
    Intent: Screening every run cost a model call per candidate pair, and that cost fell on
        authoring — the one step a person waits for. Duplicates are now found by an ad-hoc
        sweep over the stored collection instead, so a generation run must not pay for
        screening at all. This replaces four earlier tests that required the opposite.
    Success: A run stores its questions and reports no screening outcome.
    Feature: Question generation — duplicates are not screened during a run.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(stream_messages=[FakeMessage("draft")], parsed_by_format=full_run())
    result = question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert result["inserted"] == 1
    assert "duplicates_dropped" not in result
    assert "duplicate_check_error" not in result


# --- authoring from the stored documentation corpus ---


def test_the_stored_documentation_is_given_to_the_author(fake_client, settings):
    """
    Intent: The corpus exists so a run reads its source material out of the database
        instead of fetching it mid-run: a run that searches the web spends most of its
        wall clock waiting, and two runs on the same badge see different source text.
        Material that is retrieved but not put in the prompt is material not used.
    Success: The retrieved page's text and its source URL both reach the authoring
        prompt.
    Feature: Question generation — authoring from the stored documentation corpus.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    pages = [{"url": "https://x/a.md", "title": "Indexes", "text": "Define an index."}]
    question_generation.author_questions(
        [BADGE], 1, corpus_pages=pages, settings=settings
    )
    prompt = client.messages.stream_calls[0]["messages"][0]["content"]
    assert "Define an index." in prompt
    assert "https://x/a.md" in prompt


def test_the_web_is_not_searched_when_the_corpus_supplied_material(fake_client, settings):
    """
    Intent: Left available alongside retrieved pages, the web tools get used anyway,
        which puts back the minutes of waiting and the run-to-run variation that
        reading from the corpus removes. Grounding in the corpus only holds if the
        web is not also on offer.
    Success: An authoring turn given corpus pages is passed no tools.
    Feature: Question generation — the corpus replaces web research.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    pages = [{"url": "https://x/a.md", "title": "Indexes", "text": "Define an index."}]
    question_generation.author_questions(
        [BADGE], 1, corpus_pages=pages, settings=settings
    )
    assert client.messages.stream_calls[0]["tools"] == []


def test_an_empty_corpus_falls_back_to_researching_the_web(fake_client, settings):
    """
    Intent: A badge whose documentation has not been crawled yet is a reason to
        research the slow way, not a reason to refuse to write questions. The
        fallback has to be the old behaviour, intact.
    Success: With no corpus pages, the authoring turn gets the web search and fetch
        tools and is told to search.
    Feature: Question generation — falling back to web research.
    """
    client = fake_client(stream_messages=[FakeMessage("draft")])
    question_generation.author_questions([BADGE], 1, corpus_pages=[], settings=settings)
    call = client.messages.stream_calls[0]
    assert [t["type"] for t in call["tools"]] == [
        settings.web_search_tool,
        settings.web_fetch_tool,
    ]
    assert "Search and fetch as needed" in call["messages"][0]["content"]


def test_a_run_reads_the_corpus_before_authoring(
    fake_client, fake_collection, fake_questions, fake_doc_pages, settings
):
    """
    Intent: Retrieval that is not wired into the run is retrieval that never happens —
        the screen would report a normal run while every question was still written
        from a web search.
    Success: An end-to-end run puts a stored documentation page into the authoring
        prompt.
    Feature: Question generation — end-to-end run reads the corpus.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    from app.repositories import doc_pages

    doc_pages.upsert_pages([{
        "url": "https://x/a.md",
        "source": "ix-1",
        "title": "Atlas Search indexes",
        "text": "Define an Atlas Search index.",
    }])
    client = fake_client(stream_messages=[FakeMessage("draft")], parsed_by_format=full_run())
    question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert "Define an Atlas Search index." in client.messages.stream_calls[0]["messages"][0]["content"]


def test_a_run_reports_which_pages_it_wrote_from(
    fake_client, fake_collection, fake_questions, fake_doc_pages, settings
):
    """
    Intent: A question is only worth as much as it is checkable. Without knowing which
        pages a run read, a reviewer cannot tell a question grounded in current
        documentation from one written out of the model's memory.
    Success: The run summary lists the source pages, and records that the web was not
        researched.
    Feature: Question generation — the source material of a run is reported.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    from app.repositories import doc_pages

    doc_pages.upsert_pages([{
        "url": "https://x/a.md",
        "source": "ix-1",
        "title": "Atlas Search indexes",
        "text": "Define an Atlas Search index.",
    }])
    fake_client(stream_messages=[FakeMessage("draft")], parsed_by_format=full_run())
    result = question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert result["source_pages"] == [{"url": "https://x/a.md", "title": "Atlas Search indexes"}]
    assert result["researched_the_web"] is False


def test_a_run_with_no_stored_documentation_says_it_researched_the_web(
    fake_client, fake_collection, fake_questions, fake_doc_pages, settings
):
    """
    Intent: A run that fell back to the web is slower and not repeatable, and the
        remedy — refresh the corpus — is only actionable if the fallback is visible
        rather than silent.
    Success: With an empty corpus the run reports no source pages and records that it
        researched the web.
    Feature: Question generation — the fallback to web research is reported.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(stream_messages=[FakeMessage("draft")], parsed_by_format=full_run())
    result = question_generation.generate_questions(["atlas-search"], 1, settings=settings)
    assert result["source_pages"] == []
    assert result["researched_the_web"] is True


# --- the badge-scoped page walk ---


@pytest.fixture
def walk_settings(settings):
    """Settings with the page-set relevance floor open.

    The floor is calibrated against the real Atlas index, where a page plainly about a
    badge scores 0.70-0.86. The in-memory stand-in approximates similarity with word
    overlap on short fixture text, which lands well below that — so a walk test using
    the production floor would resolve to no pages at all and test nothing. The floor
    itself is exercised in tests/test_doc_retrieval.py.
    """
    return replace(settings, doc_page_set_score_floor=0.0)


PAGE = {
    "url": "https://x/a.md",
    "title": "Atlas Search indexes",
    "source": "ix-1",
    "text": "An Atlas Search index defines how fields are analysed.",
}

CHUNK = {
    "chunk_id": "c1",
    "url": "https://x/a.md",
    "anchor": "atlas-search-indexes",
    "source": "ix-1",
    "page_title": "Atlas Search indexes",
    "heading": "Atlas Search indexes",
    "heading_path": ["Atlas Search"],
    "heading_level": 2,
    "ordinal": 0,
    "text": "An Atlas Search index defines how fields are analysed.",
    "embed_text": "Atlas Search indexes\n\nAn Atlas Search index defines how fields are analysed.",
    "chars": 53,
    "bytes": 53,
}


def seed_corpus(pages, settings):
    """Store pages and the chunks derived from them, as a refresh does.

    Chunked through the real splitter rather than with hand-written fixtures, so a walk
    test exercises the same shape production produces — a chunk whose metadata the test
    invented would let the walk pass while the refresh stored something else.
    """
    from app.repositories import doc_chunks, doc_pages
    from app.services import doc_chunking

    doc_pages.upsert_pages(pages)
    for page in pages:
        stored = doc_pages.page_by_url(page["url"])
        doc_chunks.replace_page_chunks(
            page["url"], doc_chunking.split_page(stored, settings=settings)
        )


def walk_run(questions=ONE_QUESTION) -> dict:
    """A page walk makes one parse() call per page, against the question schema."""
    return {GeneratedQuestions: questions}


def test_a_page_and_its_badge_reach_the_authoring_call(fake_client, walk_settings):
    """
    Intent: The page is the source and the badge is the scope. If either is missing from
        the prompt the model writes from memory or outside the syllabus, which is the
        whole thing the walk exists to prevent.
    Success: The page text, its source URL and the badge slug all reach the prompt.
    Feature: Question generation — one page authored at a time.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "An Atlas Search index defines how fields are analysed." in prompt
    assert "https://x/a.md" in prompt
    assert "atlas-search" in prompt


def test_the_badge_catalog_reaches_the_authoring_call(fake_client, walk_settings):
    """
    Intent: Badge attribution is folded into the same call rather than run as a separate
        pass — the model already holds the question, and re-sending every question to a
        second pass pays output tokens twice to decide something it could have decided
        while writing. That only works if the catalog is in front of it.
    Success: Every badge slug in the catalog is offered in the prompt.
    Feature: Question generation — attribution folded into page authoring.
    """
    other = {"slug": "indexing", "name": "Indexing", "description": "Index design."}
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE, other], settings=walk_settings)
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "indexing" in prompt and "atlas-search" in prompt


def test_page_authoring_is_one_pass_not_two(fake_client, walk_settings):
    """
    Intent: The badge-wide path drafts prose then extracts it, which is worth it for a
        research turn. Reading one page needs no tools and no research, so a second pass
        would only pay output tokens again to restate questions already written.
    Success: One page produces questions in a single call, with no web tools.
    Feature: Question generation — a single structured pass per page.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    assert len(client.messages.parse_calls) == 1
    assert client.messages.stream_calls == []
    assert "tools" not in client.messages.parse_calls[0]


def test_page_authoring_effort_is_tuned_separately(fake_client, walk_settings):
    """
    Intent: Output tokens — thinking most of all — dominate the cost of a walk, and
        reading one page to write three questions is a bounded task rather than the
        open-ended research the badge-wide path does. Inheriting that path's effort would
        be the single largest avoidable cost at thousands of questions.
    Success: The call uses the configured page-authoring effort.
    Feature: Question generation — effort tuned for page authoring.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    call = client.messages.parse_calls[0]
    assert call["output_config"]["effort"] == walk_settings.page_author_effort


def test_a_truncated_page_authoring_response_is_reported(fake_client, walk_settings):
    """
    Intent: A pass that returns no structured output has produced nothing. Treated as an
        empty list it would look like a page with no questions in it, and the walk would
        step over material that is actually fine.
    Success: Missing structured output raises rather than returning nothing.
    Feature: Question generation — a failed page pass is not silent.
    """
    client = fake_client()
    client.messages.parsed = FakeParsedResponse(None, stop_reason="max_tokens")
    client.messages.parsed_by_format = None
    with pytest.raises(RuntimeError, match="no structured output"):
        question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)


def test_every_question_cites_the_page_it_came_from(fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings):
    """
    Intent: The citation is load-bearing twice over: it is how a reviewer checks a
        question without re-reading the corpus, and it is what "pages already written
        from" is derived from — so a walk that lost it would repeat itself forever.
        Too important to leave to the model remembering to include it.
    Success: A stored question carries the URL of the page it was written from, even
        though the model returned none.
    Feature: Question generation — the source page is always recorded.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE], walk_settings)
    fake_client(parsed_by_format=walk_run(GeneratedQuestions(questions=[make(source_urls=[])])))
    question_generation.generate_for_badge("atlas-search", settings=walk_settings)
    assert fake_questions.docs[0]["source_urls"] == ["https://x/a.md"]


def test_a_walk_stores_questions_page_by_page(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: A walk runs for many minutes. Storing only at the end would mean a failure on
        page eighteen discarded the questions from the first seventeen — an hour of work
        and spend lost to one bad page.
    Success: Questions from each page are stored, and the summary counts the pages walked.
    Feature: Question generation — a walk stores as it goes.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus(
        [PAGE, {**PAGE, "url": "https://x/b.md", "title": "Atlas Search analysers"}],
        walk_settings,
    )
    fake_client(parsed_by_format=walk_run())
    result = question_generation.generate_for_badge("atlas-search", settings=walk_settings)
    assert result["pages_done"] == 2
    assert result["inserted"] == 2
    assert len(fake_questions.docs) == 2


def test_a_walk_is_bounded_by_its_page_cap(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: The cap is how an author trades questions against how long they will wait,
        and it is the only bound on a run's spend. Ignored, a badge with 300 pages would
        run for hours on a request the author thought was small.
    Success: A walk reads no more pages than the cap allows.
    Feature: Question generation — a bounded walk.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([{**PAGE, "url": f"https://x/{n}.md"} for n in range(6)], walk_settings)
    fake_client(parsed_by_format=walk_run())
    result = question_generation.generate_for_badge(
        "atlas-search", max_pages=2, settings=walk_settings
    )
    assert result["pages_done"] == 2


def test_one_bad_page_does_not_end_the_walk(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: A refusal or a truncated response on one page says nothing about the next,
        and a badge's walk is worth far more than any single page in it. Raising would
        throw away everything after the first bad page.
    Success: A failing page is recorded with its reason and the walk continues.
    Feature: Question generation — a walk steps over a failing page.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    fake_client(parsed_by_format=walk_run())
    calls = {"n": 0}
    original = question_generation.questions_from_chunk

    def flaky(chunk, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("refused")
        return original(chunk, *args, **kwargs)

    question_generation.questions_from_chunk = flaky
    try:
        result = question_generation.generate_for_badge("atlas-search", settings=walk_settings)
    finally:
        question_generation.questions_from_chunk = original
    assert result["failure_count"] == 1
    assert "refused" in result["failures"][0]["error"]
    assert result["inserted"] == 1


def test_a_walk_reports_progress_page_by_page(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: A walk of 25 pages takes many minutes. Without per-page progress the screen
        can only show a spinner, and an author cannot tell a slow run from a stuck one.
    Success: A progress callback is given the pages done, the total and the running
        question count.
    Feature: Question generation — a walk reports its progress.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    fake_client(parsed_by_format=walk_run())
    seen = []
    question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, progress=seen.append
    )
    # Reported more than once per page — before the set is resolved, as each page is
    # named, and as each finishes — so this checks the sequence advances and ends
    # complete rather than pinning the number of snapshots.
    assert [s["pages_done"] for s in seen] == sorted(s["pages_done"] for s in seen)
    assert seen[0]["pages_done"] == 0
    assert seen[-1]["pages_done"] == 2
    assert seen[-1]["pages_total"] == 2
    assert seen[-1]["inserted"] == 2
    assert seen[-1]["phase"] == "done"


def test_a_walk_can_be_stopped(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: A run of 200 pages is a long commitment and an author who started the wrong
        one should not have to wait it out or restart the server. Stopping must keep what
        has been written rather than discarding it.
    Success: A walk asked to stop reports it stopped early and keeps its questions.
    Feature: Question generation — a walk can be stopped without losing work.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    fake_client(parsed_by_format=walk_run())
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    result = question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, stop=stop
    )
    assert result["stopped_early"] is True
    assert result["inserted"] == 1


def test_a_badge_with_no_documentation_falls_back_to_research(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: A badge whose material was never crawled is a reason to research the slow
        way, not a reason to refuse. The walk cannot walk an empty set, so the old
        single-prompt path is what answers instead — and the author has to be told,
        because that run is slower and not repeatable.
    Success: With no pages, the run researches instead and says so.
    Feature: Question generation — an uncrawled badge still produces questions.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(stream_messages=[FakeMessage("draft")], parsed_by_format=full_run())
    result = question_generation.generate_for_badge("atlas-search", settings=walk_settings)
    assert result["fell_back_to_research"] is True
    assert result["inserted"] == 1


def test_walking_an_unknown_badge_is_refused(fake_collection, fake_questions, walk_settings):
    """
    Intent: An unknown slug cannot be resolved to a page set, and questions filed under
        it would be findable from no badge at all. Better refused than stored somewhere
        nothing looks.
    Success: A slug matching no stored badge raises before anything is spent.
    Feature: Question generation — an unknown badge is refused.
    """
    with pytest.raises(ValueError, match="No skill badge"):
        question_generation.generate_for_badge("not-a-badge", settings=walk_settings)


# --- what a walk costs, and stopping it ---


def test_a_walk_reports_what_it_has_spent(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: Cost is the reason to stop a walk, so the walk has to account for it as it
        goes rather than only at the end. Reported from the token counts each response
        carries, so the figure is what was spent rather than a guess at it.
    Success: The run summary carries token counts, a call count and a dollar figure.
    Feature: Question generation — a walk accounts for its own cost.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    client = fake_client(parsed_by_format=walk_run())
    client.messages.usage = {"input_tokens": 1000, "output_tokens": 500}
    result = question_generation.generate_for_badge("atlas-search", settings=walk_settings)
    assert result["cost"]["calls"] == 2
    assert result["cost"]["input_tokens"] == 2000
    assert result["cost"]["dollars"] > 0


def test_progress_carries_the_cost_so_far_and_the_projection(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: The stop decision has to be made while there is still something to stop, so
        spend and the projected total both belong in the progress snapshot rather than the
        final summary.
    Success: A progress snapshot mid-walk reports dollars spent and a projected total.
    Feature: Question generation — cost reported during the walk.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    client = fake_client(parsed_by_format=walk_run())
    client.messages.usage = {"input_tokens": 1000, "output_tokens": 500}
    seen = []
    question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, progress=seen.append
    )
    mid = [s for s in seen if s["pages_done"] == 1][-1]
    assert mid["cost"]["dollars"] > 0
    assert mid["cost"]["projected_dollars"] > mid["cost"]["dollars"]


def test_a_walk_names_the_page_it_is_reading(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: "Page 7 of 25" says how far along a run is; it does not say whether it is
        working on something sensible. Naming the page is what lets an author notice a walk
        spending its budget on material that does not belong to the badge.
    Success: A progress snapshot names the page currently being read.
    Feature: Question generation — the page being read is reported.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE], walk_settings)
    fake_client(parsed_by_format=walk_run())
    seen = []
    question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, progress=seen.append
    )
    named = [s["current_page"] for s in seen if s["current_page"]]
    assert named and named[0]["url"] == "https://x/a.md"
    # Cleared at the end: a finished run is not still reading anything.
    assert seen[-1]["current_page"] is None


def test_a_walk_reports_its_phase(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: Resolving a badge to its page set happens before the total is known, so a
        progress bar has nothing honest to show during it. A phase lets the screen say what
        is happening instead of inventing a percentage.
    Success: A walk reports resolving before writing, and done at the end.
    Feature: Question generation — the phase of a walk is reported.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE], walk_settings)
    fake_client(parsed_by_format=walk_run())
    seen = []
    question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, progress=seen.append
    )
    phases = [s["phase"] for s in seen]
    assert phases[0] == "resolving"
    assert "writing" in phases
    assert phases[-1] == "done"


def test_a_stopped_walk_reports_the_stopped_phase(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: A stopped walk and a completed one both end with a run summary. Reported as
        done, a stopped walk would look like a badge that had run out of material, and the
        pages still waiting would be invisible.
    Success: A walk that was stopped ends in the stopped phase rather than done.
    Feature: Question generation — a stopped walk is distinguishable from a finished one.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    fake_client(parsed_by_format=walk_run())
    result = question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, stop=lambda: True
    )
    assert result["phase"] == "stopped"
    assert result["stopped_early"] is True


# --- how the questions read ---


def test_the_author_is_told_who_the_reader_is(fake_client, walk_settings):
    """
    Intent: Questions that read as machine-written are not usable as they stand — the
        audience is working software developers, and a question phrased like a technical
        writer's abstract signals that nobody who does the job wrote it. The audience has
        to be stated, not implied.
    Success: The page-authoring prompt names software developers and engineers as the
        reader.
    Feature: Question quality — written for a developer audience.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    system = client.messages.parse_calls[0]["system"]
    assert "software developer" in system
    assert "engineer" in system


def test_the_author_is_given_the_words_not_to_use(fake_client, walk_settings):
    """
    Intent: "Write naturally" does not change anything on its own; the machine-written
        register comes from a specific and recognisable vocabulary. Naming the words is
        what makes the instruction actionable rather than aspirational.
    Success: The prompt bans the vocabulary that marks the register, and the stock stems
        that go with it.
    Feature: Question quality — the machine-written register is named and excluded.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    system = client.messages.parse_calls[0]["system"]
    for word in ("leverage", "utilize", "seamless", "crucial", "delve"):
        assert word in system
    assert "Which of the following best describes" in system


def test_the_author_is_told_to_be_specific(fake_client, walk_settings):
    """
    Intent: The surest sign a question was not written by someone who does the work is
        that it names nothing — "the appropriate configuration" instead of the actual flag.
        Specificity is both what makes a question read as human and what makes it test
        anything.
    Success: The prompt requires real stage names, commands, flags and errors, and a
        second-person situation.
    Feature: Question quality — concrete situations over abstract ones.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    system = client.messages.parse_calls[0]["system"]
    assert "second person" in system
    assert "Name real things" in system
    assert "the appropriate configuration" in system


def test_the_rationales_are_held_to_the_same_voice(fake_client, walk_settings):
    """
    Intent: The rationale is what a reviewer reads when deciding whether a question is
        sound, and what an author reads when deciding whether a distractor is fair. Left
        out of the style rules it reverts to textbook prose, and the question reads as
        machine-written even when the stem does not.
    Success: The prompt applies the same voice to option rationales.
    Feature: Question quality — rationales in the same voice as the question.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    system = client.messages.parse_calls[0]["system"]
    assert "rationale" in system and "same voice" in system


# --- the kinds of question asked ---


def test_the_author_is_told_not_to_make_everything_a_scenario(fake_client, walk_settings):
    """
    Intent: Observed on real output: every question opened by painting a situation. A page
        of scenarios is exhausting to read and tests one narrow skill, and a page that
        simply states how something works does not need a story wrapped round it. The
        instruction has to say so, because scenario-writing is the model's default.
    Success: The prompt tells the author to vary the form and not to force a scenario.
    Feature: Question quality — a mix of question forms.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    system = client.messages.parse_calls[0]["system"]
    assert "Do not write every question as a scenario" in system
    assert "Do not force a scenario" in system


def test_the_author_is_given_the_forms_to_choose_between(fake_client, walk_settings):
    """
    Intent: "Vary the form" is not actionable without naming the alternatives — the model
        needs to know that factual, procedural, best-practice, diagnostic and comparative
        questions are all wanted, or it will vary only the wording of a scenario.
    Success: The prompt names each form and says what it is right for.
    Feature: Question quality — the question forms are named.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    system = client.messages.parse_calls[0]["system"]
    for form in ("Situational", "Factual", "Procedural", "Best practice", "Diagnostic", "Comparative"):
        assert form in system


def test_best_practices_are_asked_directly(fake_client, walk_settings):
    """
    Intent: Best practice is exactly where a scenario adds least — a practitioner
        recognises "which index order should you use" faster stated plainly than buried in
        a story about a slow query. Left unsaid, these get dressed up like everything else.
    Success: The prompt requires best-practice questions to be asked directly.
    Feature: Question quality — best practices stated plainly.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    system = client.messages.parse_calls[0]["system"]
    assert "Ask these directly" in system
    assert "a direct question is not a lesser question" in system


def test_the_form_is_chosen_by_the_material(fake_client, walk_settings):
    """
    Intent: A fixed quota of forms per page would be as mechanical as all-scenarios — a page
        describing a sequence should yield procedural questions, and one defining behaviour
        should yield factual ones. The material has to drive the choice.
    Success: The prompt tells the author to let the page decide the form.
    Feature: Question quality — the form follows the material.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    assert "Let the material choose the form" in client.messages.parse_calls[0]["system"]


# --- throughput and unit cost during a walk ---


def test_a_walk_reports_questions_per_minute(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: Pages per minute describes the machinery; questions per minute describes the
        output, and it is what an author plans a session against. Reported only at the end it
        arrives too late to decide whether to let a run continue.
    Success: The finished run and its progress snapshots both carry a questions-per-minute
        figure.
    Feature: Question generation — throughput reported in questions per minute.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    fake_client(parsed_by_format=walk_run())
    seen = []
    result = question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, progress=seen.append
    )
    assert result["questions_per_minute"] is not None
    assert any(s.get("questions_per_minute") is not None for s in seen)


def test_a_walk_reports_what_each_question_is_costing(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: Spend so far does not say whether a run is going well — a big number may be fine
        if it is producing a lot. Cost per question is the figure that makes stopping an
        informed decision, and during a run the running average is also the best projection
        of what the rest will cost.
    Success: Progress and the finished run both report dollars per question.
    Feature: Question generation — cost per question while the walk runs.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE, {**PAGE, "url": "https://x/b.md"}], walk_settings)
    client = fake_client(parsed_by_format=walk_run())
    client.messages.usage = {"input_tokens": 1000, "output_tokens": 500}
    seen = []
    result = question_generation.generate_for_badge(
        "atlas-search", settings=walk_settings, progress=seen.append
    )
    assert result["cost"]["dollars_per_question"] > 0
    mid = [s for s in seen if s["pages_done"] == 1][-1]
    assert mid["cost"]["dollars_per_question"] > 0


# --- asking for a skill level ---


def test_a_requested_skill_level_reaches_the_prompt(fake_client, walk_settings):
    """
    Intent: The badge decides the subject matter; the skill level decides who the question is
        for, and a quiz aimed at people who own the deployment is a different artefact from
        one aimed at people who installed it last week. Dropped between the form and the
        prompt, the choice would silently do nothing.
    Success: The requested level's guidance reaches the authoring prompt.
    Feature: Question generation — questions pitched at a chosen skill level.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(
        PAGE, BADGE, [BADGE], difficulty="advanced", settings=walk_settings
    )
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "ADVANCED" in prompt
    assert "owns the deployment" in prompt


def test_each_level_says_what_it_means(fake_client, walk_settings):
    """
    Intent: "Advanced" on its own is read as harder wording rather than harder judgement,
        which produces obscure trivia — a version number nobody remembers — instead of
        questions a senior engineer finds worth answering. Each level has to describe the
        reader and the kind of thinking wanted.
    Success: Every level's guidance describes who it is for, and advanced rules trivia out.
    Feature: Question generation — the skill levels are defined, not just named.
    """
    guidance = question_generation.DIFFICULTY_GUIDANCE
    assert set(guidance) == {"foundational", "intermediate", "advanced"}
    assert "few weeks" in guidance["foundational"]
    assert "production" in guidance["intermediate"]
    assert "not obscurer trivia" in guidance["advanced"]


def test_no_chosen_level_spreads_the_questions(fake_client, walk_settings):
    """
    Intent: Left silent the model pitches a whole page at one level of its own choosing, which
        is the same problem as forcing every question into a scenario. "Mixed" has to be an
        instruction to spread them, not the absence of one.
    Success: With no level requested the prompt asks for a spread across the three levels.
    Feature: Question generation — a mixed run spreads across levels.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "spread the questions across" in prompt


def test_the_level_asked_for_is_recorded_with_the_run(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings
):
    """
    Intent: The level is one of the choices that explains a run's output, so it belongs with
        the record — comparing two runs on a badge is meaningless if you cannot see that one
        asked for foundational and the other for advanced.
    Success: The run summary reports the level that was requested.
    Feature: Run history — the requested skill level is recorded.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE], walk_settings)
    fake_client(parsed_by_format=walk_run())
    result = question_generation.generate_for_badge(
        "atlas-search", difficulty="intermediate", settings=walk_settings
    )
    assert result["requested"]["difficulty"] == "intermediate"


# --- a huge page must not cost a fortune ---


def test_a_huge_page_is_cut_short_before_it_is_sent(fake_client, walk_settings):
    """
    Intent: Measured on a real run on 2026-08-19: the corpus holds documentation pages up to
        1.7 MB — driver tutorials repeating every example in a dozen languages — and one sent
        whole was 505,435 input tokens, $2.58 for three questions, about a hundred times the
        expected cost per question. The per-page cap existed for the single-prompt path and was
        never applied to the walk.
    Success: Only the first `doc_context_page_chars` of a page reach the prompt.
    Feature: Question generation — a page's contribution to a prompt is bounded.
    """
    # A filler that cannot occur anywhere else in the prompt, so the count is the
    # page's contribution and nothing else's — "x" also appears in the fixture URL.
    huge = {**PAGE, "text": "Q" * 500_000}
    capped = replace(walk_settings, doc_context_page_chars=1000)
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(huge, BADGE, [BADGE], settings=capped)
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "Q" * 1000 in prompt
    assert "Q" * 1001 not in prompt


def test_a_cut_page_tells_the_author_it_was_cut(fake_client, walk_settings):
    """
    Intent: A model shown a page truncated mid-sentence can reasonably conclude the feature has
        no more to it, and write a question asserting something the full page contradicts. It
        has to be told, and told not to guess at the rest.
    Success: A truncated page is marked as cut short, with an instruction not to assume the
        remainder.
    Feature: Question generation — truncation is disclosed to the author model.
    """
    huge = {**PAGE, "text": "Q" * 500_000}
    capped = replace(walk_settings, doc_context_page_chars=1000)
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(huge, BADGE, [BADGE], settings=capped)
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "cut short" in prompt
    assert "do not assume what the rest says" in prompt


def test_a_page_within_the_cap_is_sent_whole(fake_client, walk_settings):
    """
    Intent: The cap is a guard against outliers, not a summariser. Most pages are a few
        thousand characters, and quietly trimming them would lose material for no benefit —
        and would make the truncation notice a lie on nearly every page.
    Success: A page shorter than the cap arrives complete and unmarked.
    Feature: Question generation — an ordinary page is not truncated.
    """
    client = fake_client(parsed_by_format=walk_run())
    question_generation.questions_from_page(PAGE, BADGE, [BADGE], settings=walk_settings)
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert PAGE["text"] in prompt
    assert "cut short" not in prompt




# --- where the correct answer sits ---


def test_the_correct_answer_does_not_always_come_first(walk_settings):
    """
    Intent: Measured on the first 125 questions this program produced: the correct answer was
        option A in every single one. A candidate who always answers A scores 100%, so the
        entire bank is worthless as a quiz. The cause is structural — a model filling four
        options into a schema writes the right one first — so it is fixed here rather than
        asked for in the prompt.
    Success: Across many questions the correct answer lands in every position.
    Feature: Question quality — the correct answer's position is randomised.
    """
    import random

    questions = [make() for _ in range(200)]
    question_generation.randomise_option_order(questions, random.Random(7))
    positions = {
        next(i for i, o in enumerate(q.options) if o.is_correct) for q in questions
    }
    assert positions == {0, 1, 2, 3}


def test_shuffling_keeps_each_option_with_its_own_rationale(walk_settings):
    """
    Intent: Each option carries the misconception it catches. If the shuffle moved text and
        rationale independently, every distractor would be explained by the wrong reasoning —
        a subtler failure than the one being fixed, and harder to notice.
    Success: After shuffling, every option still has the rationale and correctness it started
        with.
    Feature: Question quality — shuffling moves whole options.
    """
    import random

    question = make()
    before = {(o.text, o.rationale, o.is_correct) for o in question.options}
    question_generation.randomise_option_order([question], random.Random(3))
    assert {(o.text, o.rationale, o.is_correct) for o in question.options} == before


def test_shuffling_keeps_exactly_one_correct_answer(walk_settings):
    """
    Intent: The format check runs after the shuffle, so a shuffle that lost or duplicated the
        correct flag would turn good questions into rejected ones — and a run would report
        questions discarded for a reason that had nothing to do with the model.
    Success: Every shuffled question still has exactly one correct option, and four options.
    Feature: Question quality — shuffling preserves the format.
    """
    import random

    questions = [make() for _ in range(50)]
    question_generation.randomise_option_order(questions, random.Random(11))
    assert all(len(q.options) == 4 for q in questions)
    assert all(sum(1 for o in q.options if o.is_correct) == 1 for q in questions)


def test_a_walk_stores_questions_with_shuffled_options(
    fake_client, fake_collection, fake_questions, fake_doc_pages, fake_doc_chunks, walk_settings
):
    """
    Intent: A shuffle that exists but is not wired into the walk fixes nothing — which is
        exactly how the first 125 questions were stored with the answer always first.
    Success: The walk applies the shuffle before storing.
    Feature: Question generation — stored questions have randomised option order.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE], walk_settings)
    fake_client(parsed_by_format=walk_run(GeneratedQuestions(questions=[make() for _ in range(40)])))
    question_generation.generate_for_badge("atlas-search", settings=walk_settings)
    positions = {
        next(i for i, o in enumerate(doc["options"]) if o["is_correct"])
        for doc in fake_questions.docs
    }
    assert len(positions) > 1


def test_a_badge_whose_sections_cannot_be_resolved_is_reported_as_failed(
    fake_client, fake_collection, fake_questions, fake_doc_pages, walk_settings, monkeypatch
):
    """
    Intent: A walk could not tell "the search failed" from "there is nothing left", so a
        transient Atlas error made a badge report as having exhausted its documentation —
        advice to widen the corpus, given for a badge with hundreds of sections in it. It also
        skipped the badge silently inside a multi-badge run, so the questions the author
        expected were simply absent.
    Success: The walk reports the badge as failed, names the cause, and does not claim the
        badge is exhausted or fall back to researching the web.
    Feature: Question generation — an unresolvable badge is a failure, not exhaustion.
    """
    from app.services import doc_retrieval

    fake_collection.docs.append({**BADGE, "status": "approved"})
    seed_corpus([PAGE], walk_settings)
    fake_client(parsed_by_format=walk_run())

    def unavailable(*args, **kwargs):
        raise doc_retrieval.ChunkSetUnavailable("could not resolve: 503 from Atlas")

    monkeypatch.setattr(doc_retrieval, "chunk_set_for_badge", unavailable)
    summary = question_generation.generate_for_badge(
        "atlas-search", max_pages=5, questions_per_page=3, settings=walk_settings
    )
    assert summary["phase"] == "failed"
    assert summary["sections_unavailable"] is True
    assert "503" in summary["error"]
    assert summary["inserted"] == 0
    assert not summary.get("exhausted")
    assert not summary.get("fell_back_to_research")
