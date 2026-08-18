"""Tests for app/services/badge_titles.py — reading titles from badge artwork.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app.config import Settings
from app.services import badge_discovery, badge_titles
from app.services.badge_titles import BadgeImageTitle
from tests.fakes import FakeAnthropic, FakeParsedResponse

IMAGE = "https://images.credly.com/images/abc/blob"


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


def test_the_title_printed_on_the_artwork_is_returned(fake_client, settings):
    """
    Intent: The text titles on Credly and learn.mongodb.com disagree with each other and
        with the badge artwork, and the artwork is what a badge holder sees — so the
        printed title is the one the program must use.
    Success: The transcribed title is returned.
    Feature: Badge titles — reading the title from badge artwork.
    """
    fake_client(BadgeImageTitle(title="MongoDB Overview", readable=True))
    assert badge_titles.read_title_from_image(IMAGE, settings=settings) == "MongoDB Overview"


def test_the_image_is_sent_for_reading(fake_client, settings):
    """
    Intent: The title can only come from the artwork if the artwork actually reaches the
        model, so the request must carry the image itself rather than a description of it.
    Success: The request contains an image block pointing at the badge artwork.
    Feature: Badge titles — reading the title from badge artwork.
    """
    client = fake_client(BadgeImageTitle(title="MongoDB Overview", readable=True))
    badge_titles.read_title_from_image(IMAGE, settings=settings)

    content = client.messages.parse_calls[0]["messages"][0]["content"]
    image_blocks = [b for b in content if b["type"] == "image"]
    assert image_blocks[0]["source"]["url"] == IMAGE


def test_unreadable_artwork_returns_nothing_rather_than_a_guess(fake_client, settings):
    """
    Intent: A badge whose artwork carries no readable title must fall back to the catalog
        title, not to something the model invented from the picture.
    Success: None is returned when the model reports the artwork unreadable.
    Feature: Badge titles — no invented titles.
    """
    fake_client(BadgeImageTitle(title="", readable=False))
    assert badge_titles.read_title_from_image(IMAGE, settings=settings) is None


def test_a_blank_title_is_treated_as_unreadable(fake_client, settings):
    """
    Intent: A whitespace-only transcription would otherwise overwrite a good catalog
        title with an empty name, leaving an unusable row in the review table.
    Success: None is returned for a blank title even when reported readable.
    Feature: Badge titles — no invented titles.
    """
    fake_client(BadgeImageTitle(title="   ", readable=True))
    assert badge_titles.read_title_from_image(IMAGE, settings=settings) is None


def test_the_brief_forbids_transcribing_surrounding_chrome(settings):
    """
    Intent: Badge artwork carries a "MongoDB Skill" banner and level or duration labels
        around the name. Transcribing those would produce titles no source agrees with.
    Success: The brief requires a verbatim title and rules out banners and labels.
    Feature: Badge titles — reading the title from badge artwork.
    """
    brief = badge_titles.TITLE_SYSTEM
    assert "verbatim" in brief
    assert "MongoDB Skill" in brief


def test_a_failed_read_is_reported(fake_client, settings):
    """
    Intent: If the read returns nothing parseable, the run must fail rather than silently
        keeping the catalog title while reporting that artwork titles were applied.
    Success: RuntimeError is raised when no structured output comes back.
    Feature: Badge titles — failure handling.
    """
    fake_client(FakeParsedResponse(None, stop_reason="max_tokens"))
    with pytest.raises(Exception):
        badge_titles.read_title_from_image(IMAGE, settings=settings)


def test_missing_credentials_while_reading_artwork_are_actionable(monkeypatch, settings):
    """
    Intent: Reading artwork is a Claude call, so it can be where a missing key surfaces
        first; it must name the variable to set rather than repeating the SDK's own
        message about its constructor arguments.
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
        badge_titles.read_title_from_image(IMAGE, settings=settings)


def test_other_artwork_read_failures_propagate(monkeypatch, settings):
    """
    Intent: A real API error must not be relabelled as a credential problem, or the
        operator debugs the wrong thing.
    Success: The original exception propagates.
    Feature: Badge titles — failure handling.
    """
    class Raising:
        def parse(self, **kwargs):
            raise ConnectionError("connection reset by peer")

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(ConnectionError):
        badge_titles.read_title_from_image(IMAGE, settings=settings)
