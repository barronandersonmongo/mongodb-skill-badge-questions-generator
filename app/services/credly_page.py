"""Read a badge's title from its own Credly page.

The collection API, the badge artwork and the Credly page can all name a badge
differently, and the page is what a badge holder is shown when they follow a
credential link. It is captured separately so a reviewer can see all three
without leaving the admin screen.

The title is taken from the page's own markup rather than from a model: it is
present verbatim in `og:title`, so there is nothing to infer.
"""

import html
import re

import httpx

OG_TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TAGS = re.compile(r"<[^>]+>")


def _clean(value: str) -> str:
    text = html.unescape(TAGS.sub("", value)).strip()
    # Credly suffixes the document title with its own brand.
    return re.sub(r"\s*[-|]\s*Credly\s*$", "", text).strip()


def extract_title(page: str) -> str | None:
    """Pull the badge title out of Credly page markup."""
    for pattern in (OG_TITLE, H1, TITLE_TAG):
        match = pattern.search(page)
        if match:
            title = _clean(match.group(1))
            if title:
                return title
    return None


def fetch_page_title(url: str) -> str:
    """Fetch a Credly badge page and return the title it displays."""
    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()

    title = extract_title(response.text)
    if not title:
        raise RuntimeError(
            f"No badge title found on {url}. The page markup may have changed, or the "
            "badge may no longer be published."
        )
    return title
