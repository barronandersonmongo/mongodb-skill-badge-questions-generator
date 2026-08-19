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
