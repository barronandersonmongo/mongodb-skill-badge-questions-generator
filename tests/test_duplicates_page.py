"""Tests for the duplicates screen at /duplicates — the sweep and its report.

The report used to sit on the questions screen. It is a long list about pairs of
questions, every entry of which links to a question — and while it lived there,
those links led back to the page the report was on. A sweep is also not that
screen's work: it is something done occasionally to the whole collection, not
part of writing or reviewing.

Assertions are on markup, never on a bare word: the template ships JavaScript
containing the same labels it renders.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import questions as api_module

PAGE = "/duplicates"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def isolated_storage(fake_collection, fake_questions):
    """Both collections these screens read, in memory.

    The screen loads the badge catalog and the categories in use for the sweep's scope
    pickers; without these the request waits on a connection that cannot be made.
    """


@pytest.fixture(autouse=True)
def reset_run_state():
    api_module._run_state.update(
        running=False, kind=None, last_result=None, last_error=None, last_traceback=None
    )
    yield
    api_module._run_state.update(
        running=False, kind=None, last_result=None, last_error=None, last_traceback=None
    )


def test_sweep_errors_are_surfaced(client):
    """
    Intent: A sweep that could not rerank part of the collection has not cleared it. Silence
        would be read as "no duplicates found", which is the one conclusion that must never
        be assumed.
    Success: Reported errors appear on the page.
    Feature: Question duplicate sweep — partial failures are visible.
    """
    api_module._run_state["last_result"] = {
        "source": "question-duplicate-sweep",
        "compared": 0,
        "dry_run": False,
        "deleted": [],
        "possible_duplicates": [],
        "errors": ["rerank failed for q1: VOYAGE_API_KEY not set"],
    }
    body = client.get(PAGE).text
    assert 'data-sweep-errors="true"' in body
    assert "VOYAGE_API_KEY" in body

def test_the_screen_offers_one_duplicate_control(client):
    """
    Intent: Replaces a test requiring both a sweep button and a dry-run button. Two controls
        where one is the same thing but irreversible made the operator choose a mode before
        seeing the collection, and since reporting is strictly more informative nobody should
        ever have pressed the other first.
    Success: The screen offers a single find-duplicates control and no dry-run button.
    Feature: Question duplicate sweep — one control, reachable from the main screen.
    """
    body = client.get(PAGE).text
    assert 'id="sweep-btn"' in body
    assert 'id="sweep-dry-run-btn"' not in body


def test_flagged_pairs_are_listed_for_review_with_their_scores(client):
    """
    Intent: Replaces a test reporting what a sweep had already deleted. The operator is now
        being asked to decide, so both questions in a pair and the score behind the flag have
        to be on screen — a list that only named the loser would be a decision taken on their
        behalf.
    Success: Each flagged pair shows the question to delete, the one to keep, and the score.
    Feature: Question duplicate sweep — the report is reviewable on screen.
    """
    api_module._run_state["last_result"] = {
        "source": "question-duplicate-sweep",
        "compared": 2,
        "threshold": 0.85,
        "flagged": [
            {"keep": "k1", "keep_stem": "The one kept?", "drop": "d1",
             "drop_stem": "The one to go?", "rerank_score": 0.98},
        ],
        "below_threshold": [
            {"keep": "k2", "keep_stem": "A survivor?", "drop": "d2",
             "drop_stem": "A suspect?", "rerank_score": 0.61},
        ],
        "errors": [],
    }
    body = client.get(PAGE).text
    assert 'data-sweep-flagged="true"' in body
    assert "The one to go?" in body and "The one kept?" in body
    assert "0.980" in body
    assert 'data-sweep-below="true"' in body and "0.610" in body

def test_a_flagged_pair_can_be_left_alone(client):
    """
    Intent: The threshold is a measured judgement, not a fact, so some flagged pairs will be
        two genuinely different questions on one topic. Deleting the whole flagged set as a
        block would make the threshold decide again, which is what moving deletion out of the
        sweep was meant to stop.
    Success: Each flagged pair carries its own tickbox, pre-ticked, keyed to the question that
        would be deleted.
    Feature: Question duplicate sweep — pairs are chosen individually.
    """
    api_module._run_state["last_result"] = {
        "source": "question-duplicate-sweep",
        "compared": 1,
        "threshold": 0.85,
        "flagged": [
            {"keep": "k1", "keep_stem": "Kept?", "drop": "d1",
             "drop_stem": "To go?", "rerank_score": 0.98},
        ],
        "below_threshold": [],
        "errors": [],
    }
    body = client.get(PAGE).text
    assert 'data-dupe-id="d1"' in body
    assert "checked" in body
    assert 'data-delete-dupes="true"' in body

def test_the_report_says_nothing_was_deleted(client):
    """
    Intent: The sweep used to delete, so an operator who has used it before will assume it
        still does. Saying plainly that nothing was removed is what stops them believing the
        collection has already been cleaned.
    Success: The sweep result states that nothing was deleted.
    Feature: Question duplicate sweep — the report says it deleted nothing.
    """
    api_module._run_state["last_result"] = {
        "source": "question-duplicate-sweep",
        "compared": 3,
        "threshold": 0.85,
        "flagged": [],
        "below_threshold": [],
        "errors": [],
    }
    body = client.get(PAGE).text
    assert "Nothing was deleted" in body

def sweep_result() -> dict:
    """A finished sweep with one flagged pair and one below the threshold."""
    return {
        "source": "question-duplicate-sweep",
        "compared": 2,
        "threshold": 0.85,
        "flagged": [
            {"keep": "k" * 32, "keep_stem": "The one kept?", "drop": "d" * 32,
             "drop_stem": "The one to go?", "rerank_score": 0.98},
        ],
        "below_threshold": [
            {"keep": "y" * 32, "keep_stem": "A survivor?", "drop": "z" * 32,
             "drop_stem": "A suspect?", "rerank_score": 0.61},
        ],
        "errors": [],
    }

def test_each_question_in_a_pair_is_named_by_its_identifier(client):
    """
    Intent: The report asks an operator to delete one of two questions on the strength of two
        stems. Stems are the part most alike in a duplicate pair — that is why they were
        flagged — so naming which question is which is what makes the decision auditable, and
        what lets it be discussed with anyone else.
    Success: Both questions in a flagged pair, and both in a pair below the threshold, are
        shown with their identifier.
    Feature: Question duplicate sweep — pairs name the questions they are about.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    for question_id in ("k" * 32, "d" * 32, "y" * 32, "z" * 32):
        assert 'data-question-id="' + question_id + '"' in body

# --- the screen exists so the report is not on the questions screen ---


def test_the_questions_screen_does_not_show_a_sweep_report(client):
    """
    Intent: Every entry in the report links to a question, and while the report sat on the
        questions screen those links led back to the page the report was on — a link that
        appears to do nothing. The report also outweighed the list it was sitting above.
    Success: A finished sweep's report does not render on the questions screen.
    Feature: Duplicates screen — the report is not on the questions screen.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get("/").text
    assert 'data-sweep-flagged="true"' not in body
    assert 'data-sweep-below="true"' not in body


def test_the_questions_screen_leads_here(client):
    """
    Intent: Moving the report must not hide it. Finding duplicates is something an author
        thinks of while looking at questions, so the route to it starts there.
    Success: The questions screen links to the duplicates screen.
    Feature: Duplicates screen — reachable from the questions screen.
    """
    body = client.get("/").text
    assert 'href="/duplicates"' in body


def test_a_generation_run_result_is_not_shown_here(client):
    """
    Intent: A generation run and a sweep leave their results in the same slot. A run's
        result rendered on this screen would be an unrelated report under a heading about
        duplicates.
    Success: A finished generation run leaves this screen showing no report.
    Feature: Duplicates screen — only a sweep's result belongs here.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk", "inserted": 4, "pages_done": 2,
    }
    body = client.get(PAGE).text
    assert 'data-last-result="true"' not in body
    assert 'data-empty="true"' in body


def test_the_screen_says_when_no_sweep_has_been_run(client):
    """
    Intent: An empty screen reads as "no duplicates found", which is the one conclusion that
        must never be assumed — nothing has been compared yet.
    Success: With no result, the screen says no sweep has been run.
    Feature: Duplicates screen — an unrun sweep is distinguished from a clean result.
    """
    body = client.get(PAGE).text
    assert 'data-empty="true"' in body


def test_a_generation_run_in_progress_is_named_as_such(client):
    """
    Intent: The two jobs share one run slot, so a sweep cannot start while a run is going.
        Reporting that run in this screen's words — or as a sweep — would say the sweep had
        started when it had not.
    Success: This screen distinguishes a generation run from a sweep in the state it polls.
    Feature: Duplicates screen — a generation run is not mistaken for a sweep.
    """
    body = client.get(PAGE).text
    assert 'state.kind !== "duplicate-sweep"' in body


# --- reading the two questions of a pair ---


def test_every_pair_offers_a_comparison(client):
    """
    Intent: Replaces the identifier links that opened each question elsewhere. Judging whether
        two questions are the same means reading both — and a link asked the reader to hold one
        in their head while looking at the other in a different tab, which is exactly the work
        they were trying to do.
    Success: Every pair, flagged or below the threshold, offers a control that names both of
        its questions.
    Feature: Duplicates screen — a pair can be compared in place.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    assert body.count('data-compare="true"') == 2
    assert 'data-compare-drop="' + "d" * 32 + '"' in body
    assert 'data-compare-keep="' + "k" * 32 + '"' in body


def test_the_comparison_shows_both_questions_at_once(client):
    """
    Intent: The stems are the part a duplicate pair has most in common — that is why it was
        flagged — so a comparison that showed one question at a time would be comparing the
        two things least likely to differ. Both have to be on screen together.
    Success: The comparison renders two sides in one view.
    Feature: Duplicates screen — both questions are shown together.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    assert 'data-compare-grid="true"' in body
    assert 'data-compare-body="drop"' in body
    assert 'data-compare-body="keep"' in body


def test_the_comparison_says_which_side_would_be_deleted(client):
    """
    Intent: The two sides are identical in form, and the whole decision is which of them goes.
        Getting them the wrong way round deletes the question that was meant to be kept, so
        the distinction cannot rest on position alone.
    Success: Each side is labelled with what would happen to it.
    Feature: Duplicates screen — the two sides of a comparison are named.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    assert "Would be deleted" in body
    assert "Would be kept" in body


def test_a_questions_own_words_cannot_become_markup(client):
    """
    Intent: A stem, an option and a rationale are all model-written text, drawn from
        documentation this program fetched. Putting any of it into the page as HTML would let
        fetched content decide what the page is.
    Success: The comparison is built through the DOM, never by assigning innerHTML.
    Feature: Duplicates screen — question text is inserted as text.
    """
    body = client.get(PAGE).text
    script = body[body.index("function renderQuestion"):body.index("</script>")]
    assert "innerHTML" not in script


def test_the_comparison_is_wider_than_a_standard_dialog(client):
    """
    Intent: Two questions side by side is the one thing here that genuinely wants the room. At
        Bootstrap's widest dialog each column was narrower than the list the questions came
        from, so the options wrapped more in the comparison than on the screen the reader had
        just left — which makes two questions look less alike than they are.
    Success: The comparison dialog is given a width of its own, capped against the viewport so
        it cannot exceed a smaller window.
    Feature: Duplicates screen — the comparison has room for two questions.
    """
    assert "compare-dialog" in client.get(PAGE).text
    css = client.get("/static/theme.css").text
    rule = css[css.index(".compare-dialog {"):]
    rule = rule[: rule.index("}")]
    assert "1368px" in rule
    assert "vw" in rule


def test_a_compared_question_is_laid_out_like_one_on_the_questions_screen(client):
    """
    Intent: A reader arrives at a comparison from the questions list, where a question opens
        with its identifier, generated date and source on a grey ground. Laying the same
        question out differently here makes them find those facts twice — and the comparison
        is already asking them to hold two questions in mind at once.
    Success: The comparison builds each question's facts as the same block the questions
        screen uses, with the same labels in the same order.
    Feature: Duplicates screen — a compared question looks like a listed one.
    """
    body = client.get(PAGE).text
    script = body[body.index("function factsBlock"):body.index("function renderQuestion")]
    assert 'element("dl", "question-head")' in script
    assert script.index("Question ID") < script.index("Generated") < script.index("Source")


def test_the_facts_come_before_the_question_in_a_comparison(client):
    """
    Intent: The facts are how a question is referred to, dated and checked, so they are read
        before it — the same order as the questions screen, and the reason they moved there in
        the first place.
    Success: The facts block is appended before the stem.
    Feature: Duplicates screen — facts precede the question.
    """
    body = client.get(PAGE).text
    render = body[body.index("function renderQuestion"):body.index("async function fillCompareSide")]
    assert "factsBlock(question), element(\"div\", \"compare-stem\"" in render


def test_neither_column_can_be_widened_by_its_content(client):
    """
    Intent: One long option or one unbroken URL was floored at its own minimum width, which
        widened its column past the dialog and made the comparison scroll sideways — hiding
        half of the thing being compared.
    Success: The columns are bounded below zero and long unbroken text is allowed to break.
    Feature: Duplicates screen — the comparison fits the dialog.
    """
    css = client.get("/static/theme.css").text
    grid = css[css.index(".compare-grid {"):]
    assert "minmax(0, 1fr) minmax(0, 1fr)" in grid[: grid.index("}")]
    side = css[css.index(".compare-side {"):]
    assert "overflow-wrap" in side[: side.index("}")]


# --- choosing the threshold ---


def test_the_threshold_can_be_moved_over_the_report(client):
    """
    Intent: The threshold is a measured judgement, not a fact — 0.85 sits inside a gap
        observed on one collection. A genuine duplicate scoring below it is exactly the thing
        an operator needs to reach, and re-running a sweep to see it would be minutes of round
        trips to learn something already in hand.
    Success: The report carries a slider, starting at the configured threshold.
    Feature: Duplicates screen — the threshold is adjustable.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    assert 'data-threshold="true"' in body
    assert 'type="range"' in body
    assert 'value="0.85"' in body


def test_every_scored_pair_is_on_the_page_whatever_the_threshold(client):
    """
    Intent: Moving the threshold can only reach pairs that are already rendered. If the page
        carried the flagged ones alone, lowering it would reveal nothing and the control would
        be a lie.
    Success: Pairs from both sides of the configured threshold are rendered as rows, each
        carrying its score.
    Feature: Duplicates screen — the whole scored set is on the page.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    assert body.count('data-pair="true"') == 2
    assert 'data-score="0.980000"' in body
    assert 'data-score="0.610000"' in body


def test_a_pair_below_the_threshold_can_still_be_ticked(client):
    """
    Intent: The operator's judgement outranks the score — "I have read both of these and they
        are the same question" is a better reason to delete than any number. A row that could
        not be ticked would make the threshold the decider again, which is what moving
        deletion out of the sweep was meant to stop.
    Success: A pair below the threshold renders the same tickable row as one above it, unticked.
    Feature: Duplicates screen — anything can be chosen, whatever it scored.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    below = body[body.index('data-below-list="true"'):]
    assert 'class="form-check-input mt-1 flex-shrink-0 js-dupe"' in below
    assert 'data-dupe-id="' + "z" * 32 + '"' in below


def test_moving_the_threshold_runs_nothing(client):
    """
    Intent: The reranker has already scored every pair, so which side of a line one falls on is
        arithmetic. Re-running the sweep for it would be minutes of round trips, and a sweep
        that ran on every drag of a slider would be unusable.
    Success: The threshold control re-partitions the rows already on the page and issues no
        request.
    Feature: Duplicates screen — adjusting the threshold costs nothing.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    handler = body[body.index("function applyThreshold"):body.index("thresholdInput.addEventListener")]
    assert "fetch(" not in handler
    assert "flaggedList" in handler and "belowList" in handler


def test_the_configured_default_is_named_as_a_default(client):
    """
    Intent: A slider with no marked starting point invites the reader to treat wherever they
        left it as the truth. 0.85 is calibrated — it sits inside a measured gap between
        distinct questions and reworded copies — and that is worth saying next to a control
        that can leave it.
    Success: The screen states the configured threshold as the default.
    Feature: Duplicates screen — the calibrated threshold is still visible.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    assert 'data-threshold-note="true"' in body
    assert "configured default" in body


# --- choosing what to sweep ---


def test_the_screen_offers_the_three_scopes(client):
    """
    Intent: A sweep costs one round trip per question scanned. Without a way to narrow it, the
        only sweep available on a bank of thousands is the expensive one, so it gets run once
        and then never again — which is the same as not having it.
    Success: The screen offers badge, category and skill level as scopes.
    Feature: Duplicates screen — a sweep can be scoped.
    """
    body = client.get(PAGE).text
    assert 'data-scope="skill_badge"' in body
    assert 'data-scope="category"' in body
    assert 'data-scope="difficulty"' in body


def test_the_scope_is_read_when_the_sweep_starts(client):
    """
    Intent: The pickers can be changed after the page loads, and what the operator can see is
        what they mean. A scope captured at load would silently sweep something else.
    Success: The scope is collected inside the start handler and posted with the request.
    Feature: Duplicates screen — the scope is read at the moment of starting.
    """
    body = client.get(PAGE).text
    handler = body[body.index('document.getElementById("sweep-btn")'):]
    handler = handler[: handler.index("pollStatus();")]
    assert "[data-scope]" in handler
    assert "JSON.stringify(scope)" in handler


def test_a_scoped_report_says_so(client):
    """
    Intent: "No duplicates" means something very different about one badge than about the whole
        bank. A report that did not name its scope would be read as covering everything, and
        the reader would conclude the collection is clean.
    Success: The report states the scope it covered.
    Feature: Duplicates screen — the report names its scope.
    """
    result = sweep_result()
    result["scope"] = {"skill_badge": "atlas-search", "category": None,
                       "difficulty": "advanced"}
    api_module._run_state["last_result"] = result
    body = client.get(PAGE).text
    assert 'data-scope-summary="true"' in body
    assert "atlas-search" in body
    assert "advanced level" in body


def test_an_unscoped_report_says_that_too(client):
    """
    Intent: The absence of a scope is a fact about the report, not the absence of a fact. Left
        blank it reads as an unfinished sentence, and the reader is left to assume.
    Success: A report with no scope says it covered the whole collection.
    Feature: Duplicates screen — an unscoped report is explicit.
    """
    api_module._run_state["last_result"] = sweep_result()
    body = client.get(PAGE).text
    assert "across the whole collection" in body
