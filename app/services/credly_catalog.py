"""Read the badge set straight from the published Credly collection.

The collection endpoint returns the full list of MongoDB skill badge templates as
JSON — official titles, descriptions, skill tags, and the course each badge is
earned from. That makes the badge *set* a deterministic fetch rather than a
judgement call, which is what earlier LLM-only runs kept getting wrong (counts
drifting between runs, retired badges from blog posts, paraphrased titles).

Claude is still used to reconcile this list with records already stored under
different slugs; it is no longer used to decide which badges exist.
"""

import httpx

from app.config import Settings, get_settings
from app.models.skill_badge import DiscoveredBadge


def _slug(template: dict) -> str:
    # Credly appends a disambiguating suffix (".1") to some vanity slugs.
    slug = (template.get("vanity_slug") or "").strip()
    return slug.rsplit(".", 1)[0] if slug and slug.rsplit(".", 1)[-1].isdigit() else slug


def _categories(template: dict) -> list[str]:
    return [
        skill["name"]
        for skill in template.get("skills") or []
        if isinstance(skill, dict) and skill.get("name")
    ]


def _source_urls(template: dict) -> list[str]:
    urls = [template.get("url"), template.get("earn_this_badge_url")]
    return [u for u in urls if u]


def to_badges(payload: dict) -> list[DiscoveredBadge]:
    """Convert the collection payload into badge records.

    Confidence is `high` for every badge: these come from the published
    collection itself, not from research, so there is nothing to be tentative
    about. Templates without a usable slug are skipped rather than stored under an
    empty identity.
    """
    badges = []
    for template in payload.get("data") or []:
        slug = _slug(template)
        name = (template.get("name") or "").strip()
        if not slug or not name:
            continue
        badges.append(
            DiscoveredBadge(
                slug=slug,
                name=name,
                text_title=name,
                description=(template.get("description") or "").strip(),
                categories=_categories(template),
                source_urls=_source_urls(template),
                confidence="high",
                credly_url=template.get("url"),
                mongodb_url=template.get("earn_this_badge_url"),
                image_url=template.get("image_url"),
            )
        )
    return badges


def fetch_catalog(*, settings: Settings | None = None) -> list[DiscoveredBadge]:
    """Fetch the published collection. Raises on a non-2xx or unreadable response."""
    settings = settings or get_settings()
    response = httpx.get(
        settings.credly_collection_url,
        headers={"Accept": "application/json"},
        timeout=30.0,
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()

    badges = to_badges(payload)
    if not badges:
        raise RuntimeError(
            f"The badge collection at {settings.credly_collection_url} returned no "
            "usable badges. Check the URL and whether the collection is still public."
        )
    return badges
