"""Tests for app/services/credly_page.py — reading titles from Credly pages.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import httpx
import pytest

from app.services import credly_page

URL = "https://www.credly.com/org/mongodb/badge/building-ai-agents-with-mongodb"


def test_the_page_title_is_read_from_the_markup():
    """
    Intent: The badge's own page names it differently from the collection API and from its
        artwork, and that page is what a credential link shows a viewer — so its title must
        be captured verbatim from the page itself.
    Success: The og:title value is returned.
    Feature: Badge titles — Credly page title.
    """
    page = '<html><head><meta property="og:title" content="Building AI Agents with MongoDB">'
    assert credly_page.extract_title(page) == "Building AI Agents with MongoDB"


def test_the_heading_is_used_when_there_is_no_og_title():
    """
    Intent: Page markup changes over time. A missing og:title must fall back to the visible
        heading rather than losing the title entirely.
    Success: The h1 text is returned, stripped of nested markup.
    Feature: Badge titles — Credly page title.
    """
    page = "<html><body><h1><span>CRUD Operations in MongoDB</span></h1></body></html>"
    assert credly_page.extract_title(page) == "CRUD Operations in MongoDB"


def test_the_credly_brand_suffix_is_removed():
    """
    Intent: The document title carries Credly's own branding, which is not part of the badge
        name and would show up in the review table as noise.
    Success: The trailing brand suffix is stripped.
    Feature: Badge titles — Credly page title.
    """
    page = "<html><head><title>Vector Search Performance - Credly</title></head>"
    assert credly_page.extract_title(page) == "Vector Search Performance"


def test_html_entities_are_decoded():
    """
    Intent: Titles contain ampersands, which pages encode. Storing the encoded form would
        display "&amp;" to a reviewer and would not match the title anywhere else.
    Success: The entity is decoded.
    Feature: Badge titles — Credly page title.
    """
    page = '<meta property="og:title" content="Performance Tools &amp; Techniques">'
    assert credly_page.extract_title(page) == "Performance Tools & Techniques"


def test_a_page_without_a_title_is_reported(monkeypatch):
    """
    Intent: A retired or gated badge page carries no title. That must be reported so the
        sync can list it as unverified, rather than storing an empty title.
    Success: RuntimeError naming the URL is raised.
    Feature: Badge titles — Credly page verification failures.
    """
    monkeypatch.setattr(
        credly_page.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200, text="<html><body>no title here</body></html>",
            request=httpx.Request("GET", url),
        ),
    )
    with pytest.raises(RuntimeError, match="No badge title found"):
        credly_page.fetch_page_title(URL)


def test_an_http_failure_propagates(monkeypatch):
    """
    Intent: An unreachable page must surface so the badge is reported as unverified rather
        than silently keeping a stale title.
    Success: The HTTP error propagates.
    Feature: Badge titles — Credly page verification failures.
    """
    monkeypatch.setattr(
        credly_page.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            404, text="gone", request=httpx.Request("GET", url)
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        credly_page.fetch_page_title(URL)


def test_the_fetched_page_title_is_returned(monkeypatch):
    """
    Intent: The verification step stores whatever the live page says, so the fetch must
        return the page's own title rather than anything derived locally.
    Success: The title from the fetched page is returned.
    Feature: Badge titles — Credly page title.
    """
    monkeypatch.setattr(
        credly_page.httpx,
        "get",
        lambda url, **kwargs: httpx.Response(
            200,
            text='<meta property="og:title" content="Search with MongoDB">',
            request=httpx.Request("GET", url),
        ),
    )
    assert credly_page.fetch_page_title(URL) == "Search with MongoDB"
