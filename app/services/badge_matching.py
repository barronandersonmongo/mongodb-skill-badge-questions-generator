"""Decide whether a newly discovered badge is one already in the collection.

Slugs and titles come from research and are not stable: MongoDB renames badges,
Claude derives slugs differently between runs, and a human may correct a wrong
title in the admin screen. Matching on slug alone would then re-introduce the
same badge under a second identity.

So anything whose slug is not already known is compared against the stored
badges by *description*, and Claude judges whether they describe the same badge.
Only confident matches are applied; everything else is left to insert as new and
be reviewed by a human.
"""

import anthropic
from pydantic import BaseModel, Field

from app.config import Settings, get_settings

MATCH_SYSTEM = """\
You are reconciling a freshly researched list of MongoDB skill badges against \
badges already stored in a database.

Decide which newly discovered badges are the SAME badge as one already stored, \
where the stored title may have been corrected by hand and the slugs may differ.

Judge primarily on what the badge covers — its description and topic areas — not \
on title or slug similarity. Two badges that cover the same material are the same \
badge even if titled differently. Two badges with similar titles that cover \
different material (for example a fundamentals badge and an advanced badge on the \
same topic, or the Atlas and self-managed variants of one subject) are DIFFERENT \
badges; do not merge them.

Report only pairs you are confident about. Omitting a pair is safe — it will be \
reviewed by a person. Merging two distinct badges is not: it destroys one of them."""


class BadgeMatch(BaseModel):
    discovered_slug: str = Field(description="Slug from the freshly discovered badge")
    existing_slug: str = Field(description="Slug of the stored badge it matches")
    reason: str = Field(
        description="One sentence on why these describe the same badge, citing the "
        "overlap in what they cover"
    )


class BadgeMatches(BaseModel):
    matches: list[BadgeMatch] = Field(
        description="Only pairs that are confidently the same badge. Empty is a "
        "valid answer."
    )


def _describe(badges: list[dict], keys: tuple[str, ...]) -> str:
    lines = []
    for badge in badges:
        parts = [f"{key}: {badge.get(key)!r}" for key in keys]
        lines.append("- " + "; ".join(parts))
    return "\n".join(lines)


def match_discovered_to_existing(
    discovered: list[dict],
    existing: list[dict],
    *,
    settings: Settings | None = None,
) -> dict[str, str]:
    """Map discovered slug -> existing slug for badges that are the same badge.

    Returns an empty mapping when there is nothing to compare, so a first run
    (or a run with no new slugs) costs no API call.
    """
    settings = settings or get_settings()
    if not discovered or not existing:
        return {}

    from app.services.badge_discovery import _client, _translate_auth_error

    prompt = (
        "Newly discovered badges:\n"
        + _describe(discovered, ("slug", "name", "description", "categories"))
        + "\n\nBadges already stored:\n"
        + _describe(existing, ("slug", "name", "description", "categories"))
        + "\n\nWhich discovered badges are the same badge as a stored one?"
    )

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=8000,
            system=MATCH_SYSTEM,
            output_format=BadgeMatches,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise

    if response.parsed_output is None:
        raise RuntimeError(
            f"Badge matching produced no structured output (stop_reason="
            f"{response.stop_reason})."
        )

    discovered_slugs = {b["slug"] for b in discovered}
    existing_slugs = {b["slug"] for b in existing}
    mapping: dict[str, str] = {}
    for match in response.parsed_output.matches:
        # Ignore anything that does not name badges we actually asked about; a
        # hallucinated slug must never redirect a write.
        if (
            match.discovered_slug in discovered_slugs
            and match.existing_slug in existing_slugs
            and match.discovered_slug != match.existing_slug
        ):
            mapping[match.discovered_slug] = match.existing_slug
    return mapping
