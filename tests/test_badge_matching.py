"""Tests for app/services/badge_matching.py — badge identity resolution.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app.config import Settings
from app.services import badge_discovery, badge_matching
from app.services.badge_matching import BadgeMatch, BadgeMatches
from tests.fakes import FakeAnthropic, FakeParsedResponse

DISCOVERED = [
    {
        "slug": "atlas-search-basics",
        "name": "Atlas Search Basics",
        "description": "Covers building Atlas Search indexes and running $search queries.",
        "categories": ["search"],
    }
]
EXISTING = [
    {
        "slug": "atlas-search-fundamentals",
        "name": "Atlas Search Fundamentals (corrected title)",
        "description": "Covers creating Atlas Search indexes and querying with $search.",
        "categories": ["search"],
    }
]


@pytest.fixture
def settings() -> Settings:
    return Settings(mongodb_uri="mongodb://test")


@pytest.fixture
def fake_client(monkeypatch):
    def install(parsed) -> FakeAnthropic:
        client = FakeAnthropic(parsed=parsed)
        monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)
        return client

    return install


def test_a_renamed_badge_is_recognised_as_the_existing_one(fake_client, settings):
    """
    Intent: A badge whose stored title a human corrected, or whose slug the research
        derived differently, must be recognised as the same badge — otherwise the
        next run silently re-introduces it as a second record under its old name.
    Success: The discovered slug maps to the stored badge's slug.
    Feature: Badge synchronisation — identity matching by description.
    """
    fake_client(
        BadgeMatches(
            matches=[
                BadgeMatch(
                    discovered_slug="atlas-search-basics",
                    existing_slug="atlas-search-fundamentals",
                    reason="Both cover creating Atlas Search indexes and $search queries.",
                )
            ]
        )
    )
    mapping = badge_matching.match_discovered_to_existing(
        DISCOVERED, EXISTING, settings=settings
    )
    assert mapping == {"atlas-search-basics": "atlas-search-fundamentals"}


def test_matching_compares_descriptions_not_just_titles(fake_client, settings):
    """
    Intent: Identity has to be judged on what a badge covers, so the descriptions of
        both sides must actually reach the model — matching on titles alone is what
        the feature exists to avoid.
    Success: The prompt contains both descriptions and the matching system prompt is
        used.
    Feature: Badge synchronisation — identity matching by description.
    """
    client = fake_client(BadgeMatches(matches=[]))
    badge_matching.match_discovered_to_existing(DISCOVERED, EXISTING, settings=settings)

    call = client.messages.parse_calls[0]
    prompt = call["messages"][0]["content"]
    assert DISCOVERED[0]["description"] in prompt
    assert EXISTING[0]["description"] in prompt
    assert call["system"] == badge_matching.MATCH_SYSTEM


def test_no_match_means_the_badge_is_inserted_as_new(fake_client, settings):
    """
    Intent: Declining to match must be safe and normal — an unmatched badge should be
        inserted for a human to review, never force-merged into something else.
    Success: An empty match list produces an empty mapping.
    Feature: Badge synchronisation — identity matching by description.
    """
    fake_client(BadgeMatches(matches=[]))
    assert (
        badge_matching.match_discovered_to_existing(
            DISCOVERED, EXISTING, settings=settings
        )
        == {}
    )


def test_matches_naming_unknown_badges_are_ignored(fake_client, settings):
    """
    Intent: A match is a redirect for a database write. A hallucinated or stale slug
        must never be able to point a write at a badge that was not part of the
        comparison, which would overwrite an unrelated record.
    Success: Pairs naming slugs outside the supplied inputs are dropped.
    Feature: Badge synchronisation — safe application of matches.
    """
    fake_client(
        BadgeMatches(
            matches=[
                BadgeMatch(
                    discovered_slug="atlas-search-basics",
                    existing_slug="a-badge-that-was-never-offered",
                    reason="invented",
                ),
                BadgeMatch(
                    discovered_slug="not-in-this-run",
                    existing_slug="atlas-search-fundamentals",
                    reason="invented",
                ),
            ]
        )
    )
    assert (
        badge_matching.match_discovered_to_existing(
            DISCOVERED, EXISTING, settings=settings
        )
        == {}
    )


def test_a_badge_is_never_matched_to_itself(fake_client, settings):
    """
    Intent: A self-referential pair would add a badge's own slug to its alias list as
        if it had been renamed, which is noise in the audit trail.
    Success: A pair whose two slugs are identical is dropped.
    Feature: Badge synchronisation — safe application of matches.
    """
    same = [{"slug": "atlas-search", "name": "A", "description": "d", "categories": []}]
    fake_client(
        BadgeMatches(
            matches=[
                BadgeMatch(
                    discovered_slug="atlas-search",
                    existing_slug="atlas-search",
                    reason="same slug",
                )
            ]
        )
    )
    assert badge_matching.match_discovered_to_existing(same, same, settings=settings) == {}


def test_an_empty_collection_costs_no_api_call(fake_client, settings):
    """
    Intent: On a first run there is nothing to match against, so the comparison must
        be skipped entirely rather than spending tokens asking about an empty list.
    Success: No API call is made and the mapping is empty.
    Feature: Badge synchronisation — avoiding pointless model calls.
    """
    client = fake_client(BadgeMatches(matches=[]))
    assert badge_matching.match_discovered_to_existing(DISCOVERED, [], settings=settings) == {}
    assert client.messages.parse_calls == []


def test_matching_failure_is_reported_rather_than_guessed(fake_client, settings):
    """
    Intent: If the model returns no structured answer, the run must fail loudly. An
        empty mapping would look like "nothing matched" and silently duplicate every
        renamed badge.
    Success: RuntimeError naming the missing structured output is raised.
    Feature: Badge synchronisation — failure handling.
    """
    fake_client(FakeParsedResponse(None, stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="no structured output"):
        badge_matching.match_discovered_to_existing(
            DISCOVERED, EXISTING, settings=settings
        )


def test_matching_reports_missing_credentials_actionably(monkeypatch, settings):
    """
    Intent: Matching is a second Claude call, so it can be where a credential problem
        first surfaces. It must give the same actionable message as discovery rather
        than the SDK's internal one.
    Success: RuntimeError naming ANTHROPIC_API_KEY is raised.
    Feature: Badge synchronisation — missing credential diagnostics.
    """
    class Raising:
        def parse(self, **kwargs):
            raise TypeError(
                "Could not resolve authentication method. Expected one of api_key, "
                "auth_token, or credentials to be set."
            )

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        badge_matching.match_discovered_to_existing(
            DISCOVERED, EXISTING, settings=settings
        )


def test_other_matching_failures_propagate_unchanged(monkeypatch, settings):
    """
    Intent: The credential translator sits in front of every matching failure, so a
        real API error must pass through rather than being relabelled and sending the
        operator after the wrong cause.
    Success: The original exception propagates.
    Feature: Badge synchronisation — failure handling.
    """
    class Raising:
        def parse(self, **kwargs):
            raise ConnectionError("connection reset by peer")

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(ConnectionError, match="connection reset"):
        badge_matching.match_discovered_to_existing(
            DISCOVERED, EXISTING, settings=settings
        )
