"""Tests for the run history screen at /runs.

The history used to be a dialog on the questions screen, fetched by JavaScript when
it opened. That made the one lasting record of what has been spent the least
reachable thing in the program: it could not be linked to, could not be read beside
the questions a run produced, and did not exist without a script having run.

Assertions are on markup, never on a bare word: the screens ship JavaScript
containing the same labels they render.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import runs as runs_repo

PAGE = "/runs"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def stub_history(monkeypatch):
    """Script the recorded runs and their totals."""
    state: dict = {"runs": [], "totals": {"runs": 0, "questions": 0, "pages": 0,
                                         "dollars": 0.0, "seconds": 0.0,
                                         "questions_per_minute": None,
                                         "dollars_per_question": None}}
    monkeypatch.setattr(runs_repo, "list_runs", lambda **kwargs: state["runs"])
    monkeypatch.setattr(runs_repo, "totals", lambda: state["totals"])
    return state


def a_run(**overrides) -> dict:
    return {
        "run_id": "r1",
        "finished_at": 1_755_600_000.0,
        "badge_name": "Atlas Search",
        "skill_badges": ["atlas-search"],
        "pages_done": 12,
        "pages_total": 25,
        "inserted": 34,
        "requested": {"questions_per_page": 3, "difficulty": "advanced"},
        "effort": "high",
        "questions_per_minute": 2.5,
        "elapsed_seconds": 815.0,
        "cost": {"dollars": 0.184, "dollars_per_question": 0.0054},
        "phase": "done",
        **overrides,
    }


def test_the_screen_offers_the_run_history(client, fake_collection, fake_questions):
    """
    Intent: Run history exists to make a prompt change assessable — what we did, what it
        cost, what it produced. Reachable only as an API call it would never be looked at,
        which is the same as not recording it.
    Success: The screen offers a run-history panel.
    Feature: Run history — reachable from the authoring screen.
    """
    # The panel became a screen of its own, so what the authoring screen offers is the
    # route to it. The requirement recorded above is unchanged — the history must be
    # reachable from there rather than only as an API call.
    body = client.get("/").text
    assert 'href="/runs"' in body
    assert client.get(PAGE).status_code == 200


def test_the_history_is_rendered_without_javascript(client, stub_history):
    """
    Intent: What it costs to keep the bank growing is a number someone may need to read, quote
        or paste into a message. As a dialog filled in by a script it existed only for a
        browser that had run it, and could not be linked to at all.
    Success: The runs are in the HTML the server returns.
    Feature: Run history — server-rendered like every other list.
    """
    stub_history["runs"] = [a_run()]
    body = client.get(PAGE).text
    assert 'data-run-table="true"' in body
    assert 'data-run-id="r1"' in body
    assert "Atlas Search" in body


def test_the_cumulative_spend_is_shown_first(client, stub_history):
    """
    Intent: Per-run cost is small enough to ignore individually and large enough to matter in
        aggregate — exactly the shape of cost that goes unnoticed until someone asks what this
        has cost. The total is the number the screen exists to answer.
    Success: The totals render above the runs, with the spend among them.
    Feature: Run history — cumulative totals.
    """
    stub_history["runs"] = [a_run()]
    stub_history["totals"] = {"runs": 4, "questions": 120, "pages": 60,
                             "dollars": 1.2345, "seconds": 3600.0,
                             "questions_per_minute": 2.0,
                             "dollars_per_question": 0.0103}
    body = client.get(PAGE).text
    assert 'data-run-totals="true"' in body
    assert body.index('data-run-totals="true"') < body.index('data-run-table="true"')
    assert "$1.23" in body


def test_a_failed_run_is_marked_as_failed(client, stub_history):
    """
    Intent: A failed run is the one row worth finding at a glance — it is why the questions
        someone expected are not there. Reported as its last phase it would read as having
        finished.
    Success: A run carrying an error shows as failed rather than as its phase.
    Feature: Run history — failures are visible in the list.
    """
    stub_history["runs"] = [a_run(error="rate limited", phase="writing")]
    body = client.get(PAGE).text
    outcome = body[body.index('data-outcome="true"'):]
    assert "failed" in outcome[: outcome.index("</td>")]
    assert "rate limited" in outcome[: outcome.index("</td>")]


def test_an_older_run_missing_newer_fields_still_renders(client, stub_history):
    """
    Intent: Runs recorded before a figure was measured do not carry it, and the collection is
        never migrated — an older run is a record of what was known then. A screen that could
        not render one would lose the whole history to its oldest row.
    Success: A run with no cost, rate or duration renders, showing those as absent.
    Feature: Run history — older records render.
    """
    stub_history["runs"] = [{"run_id": "old", "inserted": 3, "pages_done": 1}]
    response = client.get(PAGE)
    assert response.status_code == 200
    assert 'data-run-id="old"' in response.text


def test_no_runs_yet_is_said_rather_than_shown_as_empty(client, stub_history):
    """
    Intent: An empty table reads as a screen that failed to load. Nothing has been run yet is
        a different fact, and the one that is true on a new deployment.
    Success: With no recorded runs, the screen says so and shows no table.
    Feature: Run history — an empty history is explained.
    """
    body = client.get(PAGE).text
    assert 'data-empty="true"' in body
    assert 'data-run-table="true"' not in body


def test_the_questions_screen_no_longer_carries_the_history_dialog(client, fake_collection, fake_questions):
    """
    Intent: Two routes to one screen, one of them a dialog that cannot be linked to, is a
        choice the reader should not have to make. The sidebar lists it on every screen.
    Success: The questions screen has no history dialog or button.
    Feature: Run history — one route, from the sidebar.
    """
    body = client.get("/").text
    assert 'id="history-modal"' not in body
    assert 'id="history-btn"' not in body


def test_a_recorded_duration_reads_in_the_same_format_as_a_running_one(
    client, stub_history
):
    """
    Intent: A finished run's time was written "13m 35s" while the live panel counted in
        00:13:35, so the same quantity had two shapes and comparing a run against the one in
        flight meant converting between them.
    Success: A run of 815 seconds renders as 00:13:35 — hours, minutes and seconds, padded.
    Feature: Run history — one duration format across the tool.
    """
    stub_history["runs"] = [a_run()]
    body = client.get(PAGE).text
    assert "00:13:35" in body
    assert "13m 35s" not in body


def test_the_history_uses_the_same_column_names_as_the_run_panel(client, stub_history):
    """
    Intent: History and the live panel report the same quantities, so a run being watched and
        the same run recorded have to be readable against each other. "Cost" here and "Spend"
        there, "Took" here and "Time elapsed" there, made that a translation exercise.
    Success: The totals and the table lead with the noun — questions created, chunks
        evaluated, spend, time — and no column is headed Cost, Took, Chunks or Questions
        alone.
    Feature: Run history — column names match the run panel.
    """
    stub_history["runs"] = [a_run()]
    stub_history["totals"] = {"runs": 1, "questions": 34, "pages": 12, "dollars": 0.18,
                              "seconds": 815.0, "questions_per_minute": 2.5,
                              "dollars_per_question": 0.0054}
    body = client.get(PAGE).text
    # The figures only: the sidebar links to a screen called Questions, which is a
    # destination rather than a measurement.
    figures = body[body.index('data-run-totals="true"'):body.index("</table>")]
    for label in ("Questions created", "Chunks evaluated", "Spend", "Time taken",
                  "Cost/question", "Questions/chunk"):
        assert ">" + label + "<" in figures
    for label in ("Cost", "Took", "Chunks", "Questions", "Spent"):
        assert ">" + label + "<" not in figures
