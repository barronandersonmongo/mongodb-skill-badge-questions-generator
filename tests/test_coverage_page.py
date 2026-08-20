"""Tests for the coverage screen at /coverage.

It used to be a dialog on the questions screen. It answers "what should I run
next", which is asked before looking at questions rather than while looking at
them, and its rows lead into the list.

Assertions are on markup, never on a bare word: the screens ship JavaScript
containing the same labels they render.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

PAGE = "/coverage"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def isolated_storage(fake_collection, fake_questions):
    """The questions screen is rendered here too, and it reads from storage."""


def test_the_screen_offers_the_coverage_panel(client, fake_collection, fake_questions):
    """
    Intent: Coverage is proportional to how much documentation a badge has, so some badges
        come out thin. That is only a workflow rather than a defect if the author can see
        which ones — otherwise the imbalance is invisible until someone builds a quiz.
    Success: The screen offers a coverage panel.
    Feature: Question coverage — reachable from the authoring screen.
    """
    # The panel became a screen, so what the authoring screen offers is the route to it.
    # The requirement recorded above is unchanged: which badges are thin has to be
    # visible from where questions are written.
    assert 'href="/coverage"' in client.get("/").text
    assert 'data-coverage-body="true"' in client.get(PAGE).text


def test_the_rows_are_fetched_after_the_page_paints(client):
    """
    Intent: Resolving every badge's page set is dozens of vector searches — seconds to tens of
        seconds. Rendered with the page it would hold the whole screen blank for all of them,
        which reads as broken rather than as slow. This is the one list in the program not
        rendered server-side, and it is a deliberate exception.
    Success: The screen renders a container and a script that fills it, and says what the
        wait is for.
    Feature: Coverage screen — the slow part does not block the page.
    """
    body = client.get(PAGE).text
    assert 'data-coverage-body="true"' in body
    assert 'const API = "/api/questions"' in body
    assert 'fetch(API + "/coverage")' in body
    assert "takes a few seconds" in body


def test_a_badge_row_leads_to_its_questions(client):
    """
    Intent: "This badge is thin" is only actionable next to what it already has — the next
        thing anyone does with a thin badge is look at its questions, and copying a slug into
        a filter by hand is the step in the way.
    Success: Each row links to the questions list filtered to that badge.
    Feature: Coverage screen — rows lead into the list.
    """
    body = client.get(PAGE).text
    assert '"/?skill_badge=" + encodeURIComponent(row.skill_badge)' in body
