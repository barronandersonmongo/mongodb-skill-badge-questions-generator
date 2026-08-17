"""Tests for app/services/mongodb_page.py — the learn.mongodb.com title.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app.config import Settings
from app.services import badge_discovery, mongodb_page
from app.services.mongodb_page import MongoDBTitle
from tests.fakes import FakeAnthropic, FakeParsedResponse

URL = "https://learn.mongodb.com/courses/memory-for-ai-applications"


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


def test_the_indexed_title_is_returned_with_the_url_it_came_from(fake_client, settings):
    """
    Intent: MongoDB names these badges differently from Credly and from the artwork, and
        its course pages render in the browser so the title cannot be fetched — the
        indexed title is the only automated source, and the URL it came from is kept so a
        reviewer can check it.
    Success: The title and the matched URL are returned.
    Feature: Badge titles — learn.mongodb.com title.
    """
    fake_client(
        MongoDBTitle(found=True, title="Memory for AI Applications Skill Badge", matched_url=URL)
    )
    assert mongodb_page.fetch_indexed_title(URL, settings=settings) == (
        "Memory for AI Applications Skill Badge",
        URL,
    )


def test_the_lookup_is_restricted_to_the_mongodb_learning_site(fake_client, settings):
    """
    Intent: The title must come from MongoDB's own site. An unrestricted search would
        happily return a title from Credly or a third-party blog, which is exactly the
        confusion this field exists to resolve.
    Success: The search tool is offered with learn.mongodb.com as the only allowed domain,
        and the badge's URL is the query.
    Feature: Badge titles — learn.mongodb.com title.
    """
    client = fake_client(MongoDBTitle(found=True, title="A Title", matched_url=URL))
    mongodb_page.fetch_indexed_title(URL, settings=settings)

    call = client.messages.parse_calls[0]
    assert call["tools"][0]["allowed_domains"] == ["learn.mongodb.com"]
    assert URL in call["messages"][0]["content"]


def test_no_matching_result_returns_nothing_rather_than_a_near_miss(fake_client, settings):
    """
    Intent: Search will always return something plausible — a different course, a learning
        path, a lesson within a course. Recording one of those as MongoDB's title for this
        badge is worse than recording nothing.
    Success: None is returned when no result matched the URL.
    Feature: Badge titles — no near-miss titles.
    """
    fake_client(MongoDBTitle(found=False, title="", matched_url=""))
    assert mongodb_page.fetch_indexed_title(URL, settings=settings) is None


def test_a_blank_title_is_treated_as_not_found(fake_client, settings):
    """
    Intent: A result reported as found but carrying no title would otherwise store an empty
        string, which reads on the page as though MongoDB publishes no name for the badge.
    Success: None is returned for a blank title.
    Feature: Badge titles — no near-miss titles.
    """
    fake_client(MongoDBTitle(found=True, title="   ", matched_url=URL))
    assert mongodb_page.fetch_indexed_title(URL, settings=settings) is None


def test_the_brief_requires_matching_on_url_not_wording(settings):
    """
    Intent: Badge names differ across sources, so matching on wording is what would pick up
        the wrong page. The brief must anchor the match to the URL.
    Success: The brief requires matching the URL and forbids reporting the closest title.
    Feature: Badge titles — learn.mongodb.com title.
    """
    brief = mongodb_page.TITLE_SYSTEM
    assert "The URL decides which result is the right one" in brief
    assert "closest title" in brief


def test_a_failed_lookup_is_reported(fake_client, settings):
    """
    Intent: If the lookup returns nothing parseable, the sync must record a failure rather
        than silently leaving the badge without a MongoDB title as though none exists.
    Success: RuntimeError naming the URL is raised.
    Feature: Badge titles — lookup failure handling.
    """
    fake_client(FakeParsedResponse(None, stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="no structured output"):
        mongodb_page.fetch_indexed_title(URL, settings=settings)


def test_missing_credentials_are_actionable(monkeypatch, settings):
    """
    Intent: This is another Claude call, so it can be where a missing key surfaces; it must
        name the variable to set rather than repeating the SDK's internal message.
    Success: RuntimeError naming ANTHROPIC_API_KEY is raised.
    Feature: Badge titles — missing credential diagnostics.
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
        mongodb_page.fetch_indexed_title(URL, settings=settings)


def test_other_lookup_failures_propagate(monkeypatch, settings):
    """
    Intent: A real API error must not be relabelled as a credential problem.
    Success: The original exception propagates.
    Feature: Badge titles — lookup failure handling.
    """
    class Raising:
        def parse(self, **kwargs):
            raise ConnectionError("connection reset by peer")

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(ConnectionError):
        mongodb_page.fetch_indexed_title(URL, settings=settings)


@pytest.mark.parametrize(
    "generic",
    [
        "MongoDB Courses and Trainings | MongoDB University",
        "MongoDB Courses and Trainings",
        "mongodb university",
    ],
)
def test_the_generic_site_title_is_not_stored_as_a_badge_name(fake_client, settings, generic):
    """
    Intent: The search index falls back to the site's own document title for pages it could
        not render, and it did so for 6 of 34 badges on a real run. Storing that shows every
        reviewer the same meaningless string and hides the fact that the title is unknown.
    Success: None is returned when the result is the generic site title.
    Feature: Badge titles — rejecting the generic site title.
    """
    fake_client(MongoDBTitle(found=True, title=generic, matched_url=URL))
    assert mongodb_page.fetch_indexed_title(URL, settings=settings) is None


def test_a_real_badge_title_containing_the_word_mongodb_is_kept(fake_client, settings):
    """
    Intent: Many real badge titles begin with "MongoDB", so the generic-title check must not
        be so broad that it discards them.
    Success: A genuine MongoDB-prefixed badge title is returned.
    Feature: Badge titles — rejecting the generic site title.
    """
    fake_client(
        MongoDBTitle(found=True, title="MongoDB CRUD Operations Skill Badge", matched_url=URL)
    )
    assert mongodb_page.fetch_indexed_title(URL, settings=settings) == (
        "MongoDB CRUD Operations Skill Badge",
        URL,
    )


def test_the_badge_name_is_used_to_find_the_page(fake_client, settings):
    """
    Intent: Searching for the URL alone ranks poorly — a real run failed to find 8 of 34
        titles that do exist, and searching by badge name found them. The name must reach
        the query while the URL still decides which result counts.
    Success: Both the URL and the known name appear in the request.
    Feature: Badge titles — finding the learn.mongodb.com page.
    """
    client = fake_client(
        MongoDBTitle(found=True, title="Memory for AI Applications Skill Badge", matched_url=URL)
    )
    mongodb_page.fetch_indexed_title(URL, "Memory for AI Applications", settings=settings)

    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert URL in prompt
    assert "Memory for AI Applications" in prompt


def test_a_title_from_a_different_page_is_rejected(fake_client, settings):
    """
    Intent: Searching by name makes a well-ranked but different course the likeliest wrong
        answer — a fundamentals page instead of an advanced one, or a learning path. The
        result's URL must carry the same page slug or the title is not this badge's.
    Success: None is returned when the matched URL is a different page.
    Feature: Badge titles — no near-miss titles.
    """
    fake_client(
        MongoDBTitle(
            found=True,
            title="Vector Search Fundamentals Skill Badge",
            matched_url="https://learn.mongodb.com/courses/vector-search-fundamentals",
        )
    )
    assert mongodb_page.fetch_indexed_title(URL, "Memory for AI", settings=settings) is None


def test_a_trailing_slash_does_not_reject_the_right_page(fake_client, settings):
    """
    Intent: Search results cite the same page with and without a trailing slash. Treating
        that as a different page would discard correct titles.
    Success: The title is accepted when the URLs differ only by a trailing slash.
    Feature: Badge titles — finding the learn.mongodb.com page.
    """
    fake_client(MongoDBTitle(found=True, title="A Skill Badge", matched_url=URL + "/"))
    assert mongodb_page.fetch_indexed_title(URL, settings=settings) == (
        "A Skill Badge",
        URL + "/",
    )
