"""Tests for app/services/question_generation.py — the two Claude passes.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app.models.question import GeneratedQuestion, GeneratedQuestions, QuestionOption
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


@pytest.fixture
def fake_client(monkeypatch):
    def install(stream_messages=None, parsed=None):
        client = FakeAnthropic(stream_messages=stream_messages, parsed=parsed)
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
    fake_client(stream_messages=[FakeMessage("draft")], parsed=ONE_QUESTION)
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
        parsed=GeneratedQuestions(questions=[make(), bad]),
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
    fake_client(stream_messages=[FakeMessage("the raw draft")], parsed=ONE_QUESTION)
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


def test_a_question_is_only_attributed_to_badges_that_were_asked_for(
    fake_client, fake_collection, fake_questions, settings
):
    """
    Intent: skill_badges is what the whole collection is filtered by. A badge slug the
        model invented — or mis-spelled — would make the question unfindable under any
        real badge while looking correctly tagged.
    Success: A badge outside the request is dropped and the requested one kept.
    Feature: Question generation — badge attribution stays inside the request.
    """
    fake_collection.docs.append({**BADGE, "status": "approved"})
    fake_client(
        stream_messages=[FakeMessage("draft")],
        parsed=GeneratedQuestions(
            questions=[make(skill_badges=["atlas-search", "invented-badge"])]
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
        parsed=GeneratedQuestions(questions=[make(skill_badges=[])]),
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
