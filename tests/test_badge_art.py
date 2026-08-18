"""Tests for app/services/badge_art.py — fetching badge artwork.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import httpx
import pytest

from app.services import badge_art

URL = "https://images.credly.com/images/abc/blob"


def test_artwork_is_returned_with_its_content_type(monkeypatch):
    """
    Intent: The artwork is stored in the badge document and served back by this app, so
        both the bytes and their type must be captured — serving a PNG as the wrong type
        leaves a broken image in the review table.
    Success: The bytes and normalised content type are returned.
    Feature: Badge artwork — fetching the image.
    """
    monkeypatch.setattr(
        badge_art.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            content=b"\x89PNG fake",
            headers={"content-type": "image/png; charset=binary"},
            request=httpx.Request("GET", url),
        ),
    )
    assert badge_art.fetch_image(URL) == (b"\x89PNG fake", "image/png")


def test_a_non_image_response_is_refused(monkeypatch):
    """
    Intent: A moved or gated asset URL commonly returns an HTML login or error page. Storing
        that as artwork would put a broken image in every row and waste document space.
    Success: RuntimeError is raised rather than storing the payload.
    Feature: Badge artwork — refusing non-images.
    """
    monkeypatch.setattr(
        badge_art.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            content=b"<html>login</html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", url),
        ),
    )
    with pytest.raises(RuntimeError, match="not badge artwork"):
        badge_art.fetch_image(URL)


def test_an_oversized_image_is_refused(monkeypatch):
    """
    Intent: The artwork lives inside the badge document, which has a hard size limit, so an
        unexpectedly large asset must be refused rather than risking a document that
        cannot be written or that bloats every read.
    Success: RuntimeError naming the limit is raised.
    Feature: Badge artwork — bounding document size.
    """
    monkeypatch.setattr(
        badge_art.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            content=b"x" * (badge_art.MAX_IMAGE_BYTES + 1),
            headers={"content-type": "image/png"},
            request=httpx.Request("GET", url),
        ),
    )
    with pytest.raises(RuntimeError, match="limit"):
        badge_art.fetch_image(URL)


def test_an_http_failure_propagates(monkeypatch):
    """
    Intent: An unreachable asset must surface so the sync can report which badges have no
        artwork, rather than silently leaving them blank.
    Success: The HTTP error propagates.
    Feature: Badge artwork — failure handling.
    """
    monkeypatch.setattr(
        badge_art.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            404, text="gone", request=httpx.Request("GET", url)
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        badge_art.fetch_image(URL)
