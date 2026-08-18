"""Find and resolve duplicate badges.

Duplicates arise because the same badge is titled differently on Credly, on
learn.mongodb.com and on its own artwork, and because earlier research runs
invented their own slugs. Slug comparison cannot catch that, so candidates are
found by searching descriptions semantically (Atlas Vector Search) and then
judged by Claude on what the badges actually cover.

Confident duplicates can be merged automatically; anything less is left for a
human, because a merge destroys one of the two records.
"""

from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.repositories import skill_badges

JUDGE_SYSTEM = """\
You decide whether two MongoDB skill badge records describe the SAME badge.

Judge on what the badge covers. The same badge is often titled differently on \
Credly, on learn.mongodb.com, and on its own artwork, so differing titles and \
slugs are expected and are not evidence of difference.

Two records are DIFFERENT badges when they cover different material, even if \
their titles are similar. In particular these pairs are always different: a \
fundamentals badge and an advanced badge on one topic; an Atlas variant and a \
self-managed variant; authentication/authorization and networking security for \
the same platform.

Two records that cite the same canonical page — the same Credly badge page or the \
same learn.mongodb.com page — are the same badge.

Answer "duplicate" only when you are confident. Saying they differ is safe — a \
person will review it. Merging two distinct badges destroys one of them."""


class DuplicateVerdict(BaseModel):
    duplicate: bool = Field(description="True only if these are the same badge")
    confident: bool = Field(
        description="True only if the descriptions make the answer unambiguous"
    )
    reason: str = Field(description="One sentence citing the overlap or the difference")


def judge_pair(left: dict, right: dict, *, settings: Settings | None = None) -> DuplicateVerdict:
    """Ask Claude whether two stored badge records are the same badge."""
    settings = settings or get_settings()
    from app.services.badge_discovery import _client, _translate_auth_error

    def describe(badge: dict) -> str:
        return (
            f"slug: {badge.get('slug')}\n"
            f"title: {badge.get('name')}\n"
            f"artwork title: {badge.get('image_title')}\n"
            f"description: {badge.get('description')}\n"
            f"categories: {badge.get('categories')}\n"
            f"credly page: {badge.get('credly_url')}\n"
            f"learn.mongodb.com page: {badge.get('mongodb_url')}"
        )

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=4000,
            system=JUDGE_SYSTEM,
            output_format=DuplicateVerdict,
            messages=[
                {
                    "role": "user",
                    "content": f"Record A:\n{describe(left)}\n\nRecord B:\n{describe(right)}",
                }
            ],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise

    if response.parsed_output is None:
        raise RuntimeError(
            f"Duplicate judgement produced no structured output (stop_reason="
            f"{response.stop_reason})."
        )
    return response.parsed_output


def _normalise_url(url: str | None) -> str | None:
    return url.strip().rstrip("/").lower() or None if url else None


def duplicates_by_url(badges: list[dict]) -> list[tuple[dict, dict, str]]:
    """Pairs sharing a canonical URL, which is proof rather than a hint.

    Two records citing the same Credly badge page or the same learn.mongodb.com
    page are the same badge, so these need no model call at all.
    """
    pairs: list[tuple[dict, dict, str]] = []
    for field, label in (("credly_url", "Credly page"), ("mongodb_url", "learn.mongodb.com page")):
        grouped: dict[str, list[dict]] = {}
        for badge in badges:
            key = _normalise_url(badge.get(field))
            if key:
                grouped.setdefault(key, []).append(badge)
        for url, group in grouped.items():
            for other in group[1:]:
                pairs.append((group[0], other, f"Both cite the same {label}: {url}"))
    return pairs


def find_duplicates(
    *, top_k: int = 5, settings: Settings | None = None
) -> list[dict]:
    """Compare every badge against its nearest neighbours by description.

    Atlas embeds the descriptions and the query itself (autoEmbed), so a scan costs
    no embedding calls here, and only neighbours close enough to be plausible are
    put to the model — a full pairwise comparison would be hundreds of calls.

    Returns one entry per confirmed duplicate pair, ordered with the record to
    keep first. Pairs are deduplicated, so A/B and B/A are reported once.
    """
    settings = settings or get_settings()
    badges = skill_badges.list_badges()
    by_slug = {b["slug"]: b for b in badges}

    seen_pairs: set[tuple[str, str]] = set()
    found: list[dict] = []

    # A shared canonical URL settles identity on its own.
    for left, right, reason in duplicates_by_url(badges):
        pair = tuple(sorted((left["slug"], right["slug"])))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        keep, drop = _choose_survivor(left, right)
        found.append(
            {
                "keep": keep["slug"],
                "drop": drop["slug"],
                "keep_name": keep.get("name"),
                "drop_name": drop.get("name"),
                "score": None,
                "confident": True,
                "reason": reason,
            }
        )

    for badge in badges:
        description = badge.get("description") or ""
        if not description:
            continue
        neighbours = skill_badges.similar_by_description(
            description,
            settings.vector_index_name,
            limit=top_k,
            exclude_slug=badge["slug"],
        )
        for neighbour in neighbours:
            other = by_slug.get(neighbour["slug"])
            if not other:
                continue
            score = neighbour.get("score") or 0.0
            if score < settings.duplicate_score_threshold:
                continue
            pair = tuple(sorted((badge["slug"], other["slug"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            verdict = judge_pair(badge, other, settings=settings)
            if not verdict.duplicate:
                continue
            keep, drop = _choose_survivor(badge, other)
            found.append(
                {
                    "keep": keep["slug"],
                    "drop": drop["slug"],
                    "keep_name": keep.get("name"),
                    "drop_name": drop.get("name"),
                    "score": neighbour.get("score"),
                    "confident": verdict.confident,
                    "reason": verdict.reason,
                }
            )
    return found


def _choose_survivor(left: dict, right: dict) -> tuple[dict, dict]:
    """Prefer the record carrying review work and canonical identity.

    Curated fields first (a reviewer's title or links must not be the thing that
    gets deleted), then the record with a Credly URL and artwork title, then the
    approved one, then the older one.
    """

    def rank(badge: dict) -> tuple:
        return (
            bool(badge.get("name_locked")),
            bool(badge.get("sources_locked")),
            bool(badge.get("credly_url")),
            bool(badge.get("image_title")),
            badge.get("status") == "approved",
        )

    if rank(left) >= rank(right):
        return left, right
    return right, left


def merge_confident_duplicates(
    *, top_k: int = 5, settings: Settings | None = None
) -> dict:
    """Find duplicates, merge the confident ones, and report the rest for review."""
    candidates = find_duplicates(top_k=top_k, settings=settings)
    merged, needs_review = [], []
    for candidate in candidates:
        if candidate["confident"] and skill_badges.merge_badges(
            candidate["drop"], candidate["keep"]
        ):
            merged.append(candidate)
        else:
            needs_review.append(candidate)
    return {"merged": merged, "needs_review": needs_review}
