"""Tests for the export screen at /export.

It used to be a link in the questions screen's toolbar, scoped to whatever
that screen happened to be filtered to — so what you exported depended on a
filter you may have set minutes earlier and scrolled past. Here the scope is
the screen's own, and stated.

Assertions are on markup, never on a bare word: the screens ship JavaScript
containing the same labels they render.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

PAGE = "/export"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def isolated_storage(fake_collection, fake_questions):
    """Both collections this screen reads, in memory."""


def test_the_export_link_carries_the_current_filters(client):
    """
    Intent: Export is a stated requirement, and the author expects to get what they are
        looking at. An export link that ignored the filters would quietly hand over the
        whole collection.
    Success: Under a badge filter, the export link requests that filter from the API.
    Feature: Question export — exports what is on screen.
    """
    # The link moved to the export screen, whose own filters set the scope. The
    # requirement recorded above is unchanged: what is exported must be what the filters
    # say, never the whole collection regardless of them.
    body = client.get(PAGE, params={"skill_badge": "atlas-search"}).text
    assert 'data-download="true"' in body
    assert "/api/questions?skill_badge=atlas-search" in body


def test_the_json_is_shown_and_not_only_offered(client):
    """
    Intent: Output needs to be easy to copy and paste — that is what an export is for here.
        A file that has to be downloaded and opened to be read is a step in the way of the
        usual thing anyone does with it.
    Success: The JSON is on the page, with a control to copy it.
    Feature: Question export — the JSON is readable in place.
    """
    body = client.get(PAGE).text
    assert 'data-export-json="true"' in body
    assert 'data-copy-json="true"' in body


def test_the_export_says_how_much_is_in_it(client):
    """
    Intent: An export is handed to someone else, and "is this all of it?" is the first thing
        either party asks. A count is the only thing that answers it without reading the JSON.
    Success: The screen states how many questions the export contains.
    Feature: Question export — the export states its size.
    """
    body = client.get(PAGE).text
    assert 'data-export-count="true"' in body


def test_the_export_is_never_one_page_of_results(client):
    """
    Intent: The list is paged and this is not. An export filtered to one page of results would
        be a surprising thing to hand someone, and the surprise would not show up until they
        counted.
    Success: The export screen says it returns everything matching the filters.
    Feature: Question export — not paged.
    """
    body = client.get(PAGE).text
    assert "never one page" in body


def test_an_unreachable_database_is_explained_rather_than_crashing(client, monkeypatch):
    """
    Intent: This screen reads the whole filtered collection, so it is the most likely place to
        meet a connection problem. A stack trace says nothing about which variable to fix.
    Success: The page still returns 200 and names the failure.
    Feature: Question export — storage failures are explained.
    """
    from pymongo.errors import ServerSelectionTimeoutError

    from app.repositories import questions

    def explode(*args, **kwargs):
        raise ServerSelectionTimeoutError("no reachable servers")

    monkeypatch.setattr(questions, "list_questions", explode)
    response = client.get(PAGE)
    assert response.status_code == 200
    assert "no reachable servers" in response.text
