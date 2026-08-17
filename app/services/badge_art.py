"""Fetch badge artwork and keep it in the badge document.

The artwork is the most reliable way for a person to recognise a badge — more so
than any of the titles, which disagree across sources. Credly serves it, but a
hotlinked image breaks when Credly changes its asset URLs, so the bytes are
stored in MongoDB with the badge and served from this app.
"""

import httpx

ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "image/svg+xml", "image/webp")
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def fetch_image(url: str) -> tuple[bytes, str]:
    """Download badge artwork. Returns the bytes and their content type."""
    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise RuntimeError(
            f"{url} returned {content_type!r}, which is not badge artwork. Refusing to "
            "store it."
        )
    if len(response.content) > MAX_IMAGE_BYTES:
        raise RuntimeError(
            f"{url} returned {len(response.content)} bytes, over the "
            f"{MAX_IMAGE_BYTES} limit for artwork stored in a document."
        )
    return response.content, content_type
