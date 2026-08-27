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

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

PAGE = "/coverage"
THEME = Path(__file__).parent.parent / "app" / "static" / "theme.css"


def _theme() -> str:
    """The stylesheet as text. The screens are styled from it and nowhere else, so a
    rule the heat map depends on is asserted where it actually lives."""
    return THEME.read_text()


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


def test_the_screen_offers_a_heatmap_above_the_table(client):
    """
    Intent: The table answers "which badge is thinnest" one row at a time, which is the
        question asked while reading down it. The shape of the whole bank — a few badges
        holding most of it, a tail holding almost none — is not in any single row, and
        reading it off a column of numbers is work the screen can do instead.
    Success: The screen renders a heat map container, and it precedes the table's.
    Feature: Coverage screen — the bank's shape above its numbers.
    """
    body = client.get(PAGE).text
    assert 'data-coverage-heatmap="true"' in body
    assert body.index('data-coverage-heatmap="true"') < body.index('data-coverage-body="true"')


def test_a_circle_is_sized_by_the_questions_it_holds(client):
    """
    Intent: The map only says anything if size means one thing, and the thing to compare is
        how much of the bank each badge holds. Sizing the diameter by the count would show a
        badge with four times the questions as sixteen times the badge, which overstates the
        very imbalance the map exists to report.
    Success: The diameter is the square root of the badge's share of the fullest badge,
        between a floor and a ceiling, and is set on the circle as a custom property.
    Feature: Coverage heat map — area carries the count.
    """
    body = client.get(PAGE).text
    assert "Math.sqrt(total / largest)" in body
    assert "CIRCLE_MIN + (CIRCLE_MAX - CIRCLE_MIN)" in body
    assert 'circle.style.setProperty("--size"' in body
    assert ".heat-circle" in _theme()


def test_a_circle_leads_to_that_badges_questions(client):
    """
    Intent: A circle that reports an imbalance and cannot be acted on leaves the author back
        at the table to find the same badge again. The next thing anyone does with a badge
        the map singles out is read what it already holds.
    Success: Each circle is an anchor to the questions list filtered to that badge, so it can
        be opened in a new tab and reached from the keyboard rather than being a shape with a
        click handler.
    Feature: Coverage heat map — circles lead into the list.
    """
    body = client.get(PAGE).text
    assert 'document.createElement("a")' in body
    assert 'circle.href = "/?skill_badge=" + encodeURIComponent(row.skill_badge)' in body


def test_a_badge_with_no_questions_is_still_visible(client):
    """
    Intent: An empty badge is the most useful thing the map can report — it is the one that
        should be run next. Sized honestly it would be a dot, and filled honestly it would be
        the palest circle on the screen, so the reading the map is most worth having is the
        one it would lose.
    Success: Circles have a floor size, and a badge holding nothing is marked so the
        stylesheet can draw it as an outline rather than as a very small full circle.
    Feature: Coverage heat map — an empty badge is not a dot.
    """
    body = client.get(PAGE).text
    assert "const CIRCLE_MIN" in body
    assert 'circle.dataset.empty = "true"' in body
    assert '.heat-circle[data-empty="true"]' in _theme()


def test_the_heatmap_is_not_shown_when_there_is_nothing_to_draw(client):
    """
    Intent: An empty panel above an explanation reads as a panel that failed to load. With no
        badges at all, and when the fetch fails, the table's own message is the whole story
        and a blank circle field above it contradicts it.
    Success: The heat map's panel is hidden when the fetch fails and when no badges come back.
    Feature: Coverage heat map — nothing to draw is not an empty box.
    """
    body = client.get(PAGE).text
    assert body.count('heatmap.closest(".panel").hidden = true') == 2


def test_the_heatmap_is_styled_only_from_the_stylesheet(client):
    """
    Intent: Every screen here is built from the shared macros and styled from theme.css alone.
        A chart drawn by a script is the easiest place for a one-off palette and a hand-picked
        gap to get in, and one screen with its own look is how the screens drifted apart
        before the theme existed.
    Success: The circles carry classes the stylesheet owns, and the only properties the script
        sets on them are the two that are data — the diameter and the fill's strength.
    Feature: Coverage heat map — the shared look, not a chart's own.
    """
    body = client.get(PAGE).text
    theme = _theme()
    for rule in (".badge-heatmap", ".heat-circle", ".heat-count", ".heat-name"):
        assert rule in theme
    assert sorted(re.findall(r'setProperty\("(--[a-z]+)"', body)) == ["--heat", "--size"]
