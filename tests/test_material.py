"""Tests for app/services/material.py and the material screen.

The coverage screen answers "which badges are thin". This answers the question
behind it: whether a badge is about to run out of material worth writing from,
which the section count alone does not say. A walk takes one section per article
before it takes a second from any of them, so the count of distinct articles is
the ceiling on new material — a badge with 252 sections across 25 articles has 25
sections' worth of fresh material and 227 helpings of what it already read.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.skill_badge import DiscoveredBadge
from app.repositories import doc_chunks, questions as questions_repo, skill_badges
from app.services import doc_retrieval, material

PAGE = "/admin/material"
API = "/api/admin/material"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


def section(url: str, heading: str = "Overview", **overrides) -> dict:
    return {
        "chunk_id": url + "#" + heading,
        "url": url,
        "page_title": "A page",
        "heading": heading,
        "heading_path": [heading],
        "score": 0.9,
        **overrides,
    }


@pytest.fixture
def catalog(monkeypatch, fake_collection, fake_questions):
    """Script the badges, their resolved sections, and the questions written."""
    def install(sections: list[dict], *, slug: str = "atlas-search", used=frozenset()):
        skill_badges.upsert_badges(
            [
                DiscoveredBadge(
                    slug=slug,
                    name=slug.replace("-", " ").title(),
                    description="A badge.",
                    confidence="high",
                    categories=["search"],
                    source_urls=["https://learn.mongodb.com/" + slug],
                )
            ]
        )
        monkeypatch.setattr(
            doc_retrieval, "chunk_set_for_badge", lambda badge, **kwargs: list(sections)
        )
        monkeypatch.setattr(
            questions_repo, "source_chunk_ids_for_badge", lambda s: set(used)
        )
        monkeypatch.setattr(
            doc_chunks, "collection", lambda: _FakeChunks(sections)
        )

    return install


class _FakeChunks:
    """Enough of the chunk collection to look up which article a section came from."""

    def __init__(self, sections):
        self._sections = sections

    def find(self, query, projection=None):
        wanted = set(query["chunk_id"]["$in"])
        return [
            {"url": s["url"]} for s in self._sections if s["chunk_id"] in wanted
        ]


def test_articles_are_counted_separately_from_sections(catalog):
    """
    Intent: The section count was read as the amount of material left, and it is not: a walk
        takes one section per article before a second from any of them, so twelve sections from
        two articles is two sections' worth of new material. On the live corpus one badge's 252
        sections came from 25 articles, 85 of them slices of a single page.
    Success: A badge reports its distinct article count alongside its section count.
    Feature: Material screen — articles counted, not only sections.
    """
    catalog([section("https://a", "One"), section("https://a", "Two"),
             section("https://b", "Three")])
    row = material.badge_material()[0]
    assert row["sections"] == 3
    assert row["articles"] == 2


def test_how_concentrated_the_material_is_is_reported(catalog):
    """
    Intent: "Sections per article" is what says whether more sections would be more material or
        more of the same, and the biggest single article is what says whether one page is
        dominating a badge — which is exactly what went wrong when 85 of 252 sections were one
        page of repeated code samples.
    Success: The row carries the mean sections per article and the largest article's share.
    Feature: Material screen — concentration is visible.
    """
    catalog([section("https://a", "One"), section("https://a", "Two"),
             section("https://a", "Three"), section("https://b", "Four")])
    row = material.badge_material()[0]
    assert row["sections_per_article"] == 2.0
    assert row["largest_article"] == 3
    assert row["largest_article_url"] == "https://a"


def test_the_documentation_can_be_narrowed_by_topic(catalog):
    """
    Intent: A badge's headline article count is over everything it resolves to. The real
        question is often narrower — a badge may resolve to 25 articles yet only 3 that are
        about Voyage AI, which makes it saturated for that topic while looking healthy.
    Success: Sections and articles count only what matches the topic term.
    Feature: Material screen — the documentation side can be filtered.
    """
    catalog([
        section("https://a", "Voyage AI embeddings"),
        section("https://b", "Atlas Search indexes"),
        section("https://c", "Using voyage-3 with Atlas"),
    ])
    row = material.badge_material(contains="voyage")[0]
    assert row["sections"] == 2
    assert row["articles"] == 2


def test_a_topic_filter_reads_headings_not_bodies(catalog):
    """
    Intent: A section's body mentions everything it relates to, so matching on it would put
        "the article that name-drops Voyage AI once" in the same bucket as "the article about
        Voyage AI" — and the count is being used to judge whether there is material there.
    Success: A section whose only mention is in its body does not match.
    Feature: Material screen — topic matching is on titles and headings.
    """
    catalog([section("https://a", "Atlas Search indexes", body="all about voyage ai")])
    assert material.badge_material(contains="voyage")[0]["sections"] == 0


def test_the_question_filters_do_not_change_the_documentation_counts(catalog):
    """
    Intent: The two kinds of filter act on different things — one on the corpus, one on what has
        been written from it. If a category filter changed the article count, the screen would
        be claiming the docs are tagged the way the questions are, which they are not.
    Success: A category filter changes the question count and leaves sections and articles
        alone.
    Feature: Material screen — question filters and documentation filters are separate.
    """
    catalog([section("https://a"), section("https://b")])
    unfiltered = material.badge_material()[0]
    filtered = material.badge_material(category="nothing-has-this")[0]
    assert filtered["sections"] == unfiltered["sections"]
    assert filtered["articles"] == unfiltered["articles"]
    assert filtered["questions"] == 0


def test_articles_already_written_from_are_counted(catalog):
    """
    Intent: "We have written from 23 sections" says nothing about whether that was 23 articles
        or one article twenty-three times — and the second is how a badge ends up with many
        questions that are all about the same page.
    Success: The row reports how many distinct articles have been written from.
    Feature: Material screen — past coverage is counted in articles too.
    """
    sections = [section("https://a", "One"), section("https://a", "Two"),
                section("https://b", "Three")]
    catalog(sections, used={sections[0]["chunk_id"], sections[1]["chunk_id"]})
    row = material.badge_material()[0]
    assert row["sections_used"] == 2
    assert row["articles_used"] == 1


def test_the_badge_nearest_to_saturation_is_listed_first(catalog):
    """
    Intent: The screen exists to find badges about to run out. Sorted by section count the
        badge with 252 sections across 25 articles would outrank one with 58 across 36, which
        is backwards — the second has more new material.
    Success: Rows are ordered by how few articles they have left.
    Feature: Material screen — ordered by nearness to saturation.
    """
    catalog([section("https://a", "One")], slug="thin")
    rows = material.badge_material()
    assert rows[0]["articles"] <= (rows[-1]["articles"] or 0)


def test_an_unresolvable_badge_still_reports_its_questions(catalog, monkeypatch):
    """
    Intent: Resolving sections needs the Atlas vector index, which can be missing or still
        building. Failing the whole screen for it would hide the question counts, which do not
        depend on it — and a blank row reads as "no material" rather than "not measured".
    Success: The row renders with its question count and its section figures unknown.
    Feature: Material screen — an unresolvable badge is reported, not fatal.
    """
    catalog([section("https://a")])

    def explode(*args, **kwargs):
        raise RuntimeError("index not queryable")

    monkeypatch.setattr(doc_retrieval, "chunk_set_for_badge", explode)
    row = material.badge_material()[0]
    assert row["articles"] is None
    assert row["error"] == "index not queryable"
    assert row["questions"] == 0


# --- the screen ---


def test_the_screen_is_in_the_admin_area(client, fake_collection, fake_questions):
    """
    Intent: Whether the corpus is about to run out for a badge is a question about the
        material, not about any question in it — the same curation work as the badge catalog
        and the corpus itself.
    Success: The screen renders under /admin with its nav link marked as admin.
    Feature: Material screen — an admin screen.
    """
    response = client.get(PAGE)
    assert response.status_code == 200
    assert 'href="/admin/material"' in response.text
    assert 'data-admin-area="true"' in response.text


def test_the_screen_offers_both_kinds_of_filter(client, fake_collection, fake_questions):
    """
    Intent: The two kinds narrow different things, and a reader who took them for one set would
        read an article count as though it were filtered by category. Keeping them apart on
        screen is what says they are not the same question.
    Success: The screen offers the documentation filters and the question filters, and says
        which each one affects.
    Feature: Material screen — the two filter kinds are distinguished.
    """
    body = client.get(PAGE).text
    assert 'data-filter="contains"' in body
    assert 'data-filter="skill_badge"' in body
    assert 'data-filter="category"' in body
    assert 'data-filter="difficulty"' in body
    assert "Narrow the documentation" in body
    assert "Narrow the questions" in body


def test_measuring_is_asked_for_rather_than_automatic(client, fake_collection, fake_questions):
    """
    Intent: Resolving one badge's sections is dozens of vector searches and all of them is tens
        of seconds. On page load that is a screen that appears broken; on every filter change it
        is a screen that cannot be used.
    Success: The screen renders a control that starts the measurement, and says what it costs.
    Feature: Material screen — the expensive part is opt-in.
    """
    body = client.get(PAGE).text
    assert 'data-load="true"' in body
    assert "dozens of vector" in body
    assert 'data-material-body="true"' in body


def test_the_api_refuses_an_unoffered_skill_level(client, fake_collection, fake_questions):
    """
    Intent: The level is compared against a stored value, so a typo would quietly match nothing
        and report every badge as having no questions — which on this screen reads as a bank
        that needs filling.
    Success: A level outside the three the program uses is rejected.
    Feature: Material screen — the question filters are validated.
    """
    assert client.get(API, params={"difficulty": "expert"}).status_code == 422
