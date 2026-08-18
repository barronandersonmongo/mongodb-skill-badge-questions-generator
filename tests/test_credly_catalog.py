"""Tests for app/services/credly_catalog.py — the authoritative badge set.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import httpx
import pytest

from app.config import Settings
from app.services import credly_catalog

TEMPLATE = {
    "name": "Building AI-Powered Search with MongoDB Vector Search",
    "vanity_slug": "building-ai-powered-search-with-mongodb-vector-sear.1",
    "description": "Validates knowledge of indexing, embeddings and retrieval.",
    "skills": [
        {"name": "AI", "vanity_slug": "ai"},
        {"name": "Document Retrieval", "vanity_slug": "document-retrieval"},
    ],
    "url": "https://www.credly.com/org/mongodb/badge/building-ai-powered-search.1",
    "earn_this_badge_url": "https://learn.mongodb.com/courses/vector-search-fundamentals",
}


@pytest.fixture
def settings() -> Settings:
    return Settings(mongodb_uri="mongodb://test")


def test_a_collection_entry_becomes_a_badge_record(settings):
    """
    Intent: The published collection carries the official title, description, skill
        tags and links, so a badge must be built from those rather than researched —
        this is what stops titles being paraphrased and counts drifting between runs.
    Success: Title, description and skill tags map across, with both the badge page
        and its course page kept as evidence.
    Feature: Badge synchronisation — authoritative badge set.
    """
    badge = credly_catalog.to_badges({"data": [TEMPLATE]})[0]

    assert badge.name == "Building AI-Powered Search with MongoDB Vector Search"
    assert badge.description.startswith("Validates knowledge")
    assert badge.categories == ["AI", "Document Retrieval"]
    assert badge.source_urls == [TEMPLATE["url"], TEMPLATE["earn_this_badge_url"]]


def test_the_disambiguating_suffix_is_stripped_from_the_slug(settings):
    """
    Intent: Credly appends ".1" to some vanity slugs. Keeping it would make the stored
        identity depend on a Credly bookkeeping detail, and a change to it would look
        like a different badge.
    Success: The trailing numeric suffix is removed from the slug.
    Feature: Badge synchronisation — stable badge identity.
    """
    badge = credly_catalog.to_badges({"data": [TEMPLATE]})[0]
    assert badge.slug == "building-ai-powered-search-with-mongodb-vector-sear"


def test_a_slug_that_merely_ends_in_a_word_is_left_alone(settings):
    """
    Intent: Only a numeric suffix is Credly bookkeeping. Trimming after any dot would
        corrupt legitimate slugs.
    Success: A slug whose last dot-segment is not a number is preserved.
    Feature: Badge synchronisation — stable badge identity.
    """
    entry = {**TEMPLATE, "vanity_slug": "search.with.mongodb"}
    assert credly_catalog.to_badges({"data": [entry]})[0].slug == "search.with.mongodb"


def test_catalog_badges_are_high_confidence(settings):
    """
    Intent: These badges come from the published collection, not from research, so
        there is nothing tentative about them — a reviewer should not be asked to
        second-guess the source of truth.
    Success: Confidence is "high".
    Feature: Badge synchronisation — authoritative badge set.
    """
    assert credly_catalog.to_badges({"data": [TEMPLATE]})[0].confidence == "high"


@pytest.mark.parametrize("missing", ["name", "vanity_slug"])
def test_entries_without_an_identity_are_skipped(settings, missing):
    """
    Intent: A template with no title or no slug cannot be stored under a meaningful
        identity, and storing it blank would create a row nobody can act on.
    Success: Such an entry is skipped rather than stored.
    Feature: Badge synchronisation — authoritative badge set.
    """
    entry = {**TEMPLATE, missing: ""}
    assert credly_catalog.to_badges({"data": [entry]}) == []


def test_fetch_reads_the_configured_collection(monkeypatch, settings):
    """
    Intent: The collection URL is the authority for which badges exist, so the fetch
        must go to the configured URL and ask for JSON — a stale or wrong URL should
        be a config change, not a code change.
    Success: The configured URL is requested with an Accept: application/json header.
    Feature: Badge synchronisation — authoritative badge set.
    """
    seen = {}

    def fake_get(url, **kwargs):
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return httpx.Response(
            200, json={"data": [TEMPLATE]}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(credly_catalog.httpx, "get", fake_get)
    badges = credly_catalog.fetch_catalog(settings=settings)

    assert seen["url"] == settings.credly_collection_url
    assert seen["headers"]["Accept"] == "application/json"
    assert len(badges) == 1


def test_an_http_failure_is_raised_not_swallowed(monkeypatch, settings):
    """
    Intent: If the collection cannot be read, the run must fail loudly. Treating a 500
        or a redirect to a login page as "no badges" would look like the catalog had
        emptied, and could retire every badge in a future sync.
    Success: The HTTP error propagates.
    Feature: Badge synchronisation — failure handling.
    """
    monkeypatch.setattr(
        credly_catalog.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            500, request=httpx.Request("GET", url), text="boom"
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        credly_catalog.fetch_catalog(settings=settings)


def test_an_empty_collection_is_treated_as_an_error(monkeypatch, settings):
    """
    Intent: An empty or reshaped payload means the source moved or is gated, not that
        MongoDB retired every badge. Returning an empty list would let a later step
        act on that as though it were true.
    Success: RuntimeError naming the collection URL is raised.
    Feature: Badge synchronisation — failure handling.
    """
    monkeypatch.setattr(
        credly_catalog.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200, json={}, request=httpx.Request("GET", url)
        ),
    )
    with pytest.raises(RuntimeError, match="credly.com"):
        credly_catalog.fetch_catalog(settings=settings)
