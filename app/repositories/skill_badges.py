"""Persistence for the skill_badges collection.

Upserts are keyed on `slug`, optionally redirected by a match mapping so a badge
that was re-discovered under a different slug updates the existing record rather
than inserting a second copy of the same badge.

Two things are human decisions and are never overwritten by a discovery run:
`status` (set on insert only) and a hand-corrected `name` (protected by
`name_locked`).
"""

import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from bson.binary import Binary
from pymongo import ASCENDING, UpdateOne
from pymongo.collection import Collection

from app.config import get_settings
from app.db import get_database
from app.models.skill_badge import DiscoveredBadge

# Fields a discovery run refreshes unconditionally. `name` and `source_urls` are
# handled separately because a human may have curated them; `status` is
# insert-only.
REFRESHED_FIELDS = (
    "description",
    "categories",
    "confidence",
    "credly_url",
    "mongodb_url",
    "image_url",
    "text_title",
)


def collection() -> Collection:
    return get_database()[get_settings().skill_badges_collection]


def ensure_indexes() -> None:
    coll = collection()
    coll.create_index([("slug", ASCENDING)], unique=True, name="slug_unique")
    coll.create_index([("categories", ASCENDING)], name="categories")
    coll.create_index([("status", ASCENDING)], name="status")
    coll.create_index([("aliases", ASCENDING)], name="aliases")


def similar_by_description(
    description: str, index_name: str, *, limit: int = 5, exclude_slug: str | None = None
) -> list[dict[str, Any]]:
    """Nearest badges by description meaning, most similar first.

    The index is configured with autoEmbed, so the query is the description text
    itself — Atlas embeds it with the same model it used for the stored documents.
    """
    pipeline: list[dict[str, Any]] = [
        {
            "$vectorSearch": {
                "index": index_name,
                "path": "description",
                "query": description,
                "numCandidates": 100,
                "limit": limit + (1 if exclude_slug else 0),
            }
        },
        {
            "$project": {
                "_id": False,
                "slug": True,
                "name": True,
                "description": True,
                "status": True,
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]
    results = list(collection().aggregate(pipeline))
    if exclude_slug:
        results = [r for r in results if r["slug"] != exclude_slug]
    return results[:limit]


def merge_badges(loser_slug: str, winner_slug: str) -> bool:
    """Fold one badge into another, keeping the winner's curated fields.

    The loser's slug and aliases move onto the winner so a later sync recognises
    either name, then the loser is removed. Curated titles and links on the winner
    are untouched: a merge must never lose review work.
    """
    coll = collection()
    if loser_slug == winner_slug:
        return False
    loser = coll.find_one({"slug": loser_slug}, {"_id": False})
    winner = coll.find_one({"slug": winner_slug}, {"_id": False})
    if not loser or not winner:
        return False

    aliases = {loser_slug, *(loser.get("aliases") or [])}
    update: dict[str, Any] = {"$addToSet": {"aliases": {"$each": sorted(aliases)}}}

    # Fill only gaps on the winner; never overwrite what a reviewer curated.
    fill = {}
    for field in ("credly_url", "mongodb_url", "image_url", "image_title", "description"):
        if not winner.get(field) and loser.get(field):
            fill[field] = loser[field]
    if not winner.get("sources_locked"):
        merged_sources = list(
            dict.fromkeys((winner.get("source_urls") or []) + (loser.get("source_urls") or []))
        )
        if merged_sources != (winner.get("source_urls") or []):
            fill["source_urls"] = merged_sources
    if fill:
        update["$set"] = fill

    coll.update_one({"slug": winner_slug}, update)
    coll.delete_one({"slug": loser_slug})
    return True


def upsert_badges(
    badges: list[DiscoveredBadge], matches: dict[str, str] | None = None
) -> dict[str, Any]:
    """Write discovered badges. Returns a summary for the admin UI.

    `matches` maps a discovered slug to the slug of the stored badge it is the
    same as; those writes are redirected onto the existing record and the
    discovered slug is remembered as an alias.
    """
    if not badges:
        return {
            "run_id": None,
            "matched": 0,
            "inserted": 0,
            "modified": 0,
            "slugs": [],
            "merged": {},
        }

    ensure_indexes()
    matches = {**alias_owners(), **(matches or {})}
    run_id = uuid4().hex
    now = datetime.now(timezone.utc)
    coll = collection()

    operations: list[UpdateOne] = []
    name_operations: list[UpdateOne] = []
    for badge in badges:
        target = matches.get(badge.slug, badge.slug)
        fields = {key: getattr(badge, key) for key in REFRESHED_FIELDS}
        update: dict[str, Any] = {
            "$set": {**fields, "discovered_at": now, "discovery_run_id": run_id},
            "$setOnInsert": {
                "status": "candidate",
                "created_at": now,
                "name": badge.name,
                "source_urls": badge.source_urls,
                "image_title": badge.image_title,
            },
        }
        if target != badge.slug:
            # Remember the slug this badge was discovered under so the next run
            # recognises it without asking the model again.
            update["$addToSet"] = {"aliases": badge.slug}
        operations.append(UpdateOne({"slug": target}, update, upsert=True))

        # Refresh the title and the reference links only where a human has not
        # curated them.
        name_operations.append(
            UpdateOne(
                {"slug": target, "name_locked": {"$ne": True}},
                {"$set": {"name": badge.name, "image_title": badge.image_title}},
            )
        )
        name_operations.append(
            UpdateOne(
                {"slug": target, "sources_locked": {"$ne": True}},
                {"$set": {"source_urls": badge.source_urls}},
            )
        )

    result = coll.bulk_write(operations, ordered=False)
    coll.bulk_write(name_operations, ordered=False)
    return {
        "run_id": run_id,
        "matched": result.matched_count,
        "inserted": len(result.upserted_ids),
        "modified": result.modified_count,
        "slugs": [matches.get(b.slug, b.slug) for b in badges],
        "merged": dict(matches),
    }


# The artwork bytes live on the document but are excluded from listings, which
# would otherwise carry megabytes of images into every page render.
LIST_PROJECTION = {"_id": False, "image_data": False}


def list_badges(status: str | None = None) -> list[dict[str, Any]]:
    query = {"status": status} if status else {}
    return list(collection().find(query, LIST_PROJECTION).sort("name", ASCENDING))


def alias_owners() -> dict[str, str]:
    """Map every remembered alias to the slug of the record that owns it.

    A slug recorded as an alias is already known, so matching skips it — which
    means the upsert must redirect it here instead, or it would insert a second
    record for a badge that was merged once already.
    """
    owners: dict[str, str] = {}
    for doc in collection().find({"aliases": {"$exists": True}}, {"_id": False, "slug": True, "aliases": True}):
        for alias in doc.get("aliases") or []:
            owners[alias] = doc["slug"]
    return owners


def known_slugs() -> set[str]:
    """Every slug the collection answers to, including remembered aliases."""
    known: set[str] = set()
    for doc in collection().find({}, {"_id": False, "slug": True, "aliases": True}):
        known.add(doc["slug"])
        known.update(doc.get("aliases") or [])
    return known


def set_status(slug: str, status: str) -> bool:
    result = collection().update_one({"slug": slug}, {"$set": {"status": status}})
    return result.matched_count == 1


def set_name(slug: str, name: str) -> bool:
    """Correct a badge's title. Locks it against being overwritten by a re-run."""
    result = collection().update_one(
        {"slug": slug}, {"$set": {"name": name, "name_locked": True}}
    )
    return result.matched_count == 1


def set_source_urls(slug: str, urls: list[str]) -> bool:
    """Replace a badge's reference links. Locks them against being overwritten.

    Curating links is a review decision — a reviewer removes a link that points at
    the wrong badge, or adds the catalog page a run failed to cite. A later run
    must not undo that, so the list is locked once edited.
    """
    result = collection().update_one(
        {"slug": slug}, {"$set": {"source_urls": urls, "sources_locked": True}}
    )
    return result.matched_count == 1


def delete_badge(slug: str) -> bool:
    """Permanently remove a retired badge.

    Restricted to badges already marked retired: retiring is the reversible
    decision, deleting is not, so a badge cannot be destroyed in one click from
    the review queue. Note a deleted badge is no longer known, so a later run
    that finds it again will re-introduce it as a new candidate.
    """
    result = collection().delete_one({"slug": slug, "status": "retired"})
    return result.deleted_count == 1


def set_image(slug: str, data: bytes, content_type: str, source_url: str) -> bool:
    """Store badge artwork in the badge document.

    The source URL is recorded so a re-sync only re-downloads when the artwork
    actually moved, and `image_stored` lets a listing know an image exists without
    carrying the bytes.
    """
    result = collection().update_one(
        {"slug": slug},
        {
            "$set": {
                "image_data": Binary(data),
                "image_content_type": content_type,
                "image_source_url": source_url,
                "image_bytes": len(data),
                "image_stored": True,
            }
        },
    )
    return result.matched_count == 1


def get_image(slug: str) -> tuple[bytes, str] | None:
    """Return stored artwork and its content type, or None if there is none."""
    doc = collection().find_one(
        {"slug": slug}, {"_id": False, "image_data": True, "image_content_type": True}
    )
    if not doc or not doc.get("image_data"):
        return None
    return bytes(doc["image_data"]), doc.get("image_content_type") or "image/png"


def badges_needing_art() -> list[dict[str, Any]]:
    """Badges whose stored artwork is missing or no longer matches their image URL."""
    return [
        doc
        for doc in collection().find(
            {"image_url": {"$exists": True}},
            {"_id": False, "slug": True, "image_url": True, "image_source_url": True},
        )
        if doc.get("image_url") and doc.get("image_source_url") != doc.get("image_url")
    ]


def slug_from_name(name: str) -> str:
    """Derive a badge slug from its title: lowercase, hyphen-separated.

    Punctuation is dropped rather than kept, so a title like "Data Resilience:
    Atlas" gives a URL-safe identifier. The slug is the badge's identity, so it
    should not carry characters that need escaping wherever it appears.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return cleaned.strip("-")


def desired_slug(doc: dict[str, Any]) -> str:
    """The slug a badge should have: derived from the title on its artwork.

    The artwork is the authority for a badge's name, so it is also the authority
    for its identity. A badge with no readable artwork falls back to the title in
    use, which is the best available name.
    """
    return slug_from_name(doc.get("image_title") or doc.get("name") or "")


def normalise_slugs() -> dict[str, Any]:
    """Move every badge to the slug derived from its artwork title.

    Renames are reported rather than forced: two badges whose artwork titles reduce
    to one slug would otherwise silently collapse into a single record.
    """
    renamed, unchanged, conflicts = [], 0, []
    for doc in list_badges():
        wanted = desired_slug(doc)
        if not wanted:
            continue
        outcome = rename_slug(doc["slug"], wanted)
        if outcome == "renamed":
            renamed.append({"from": doc["slug"], "to": wanted})
        elif outcome == "unchanged":
            unchanged += 1
        else:
            conflicts.append({"slug": doc["slug"], "wanted": wanted})
    return {"renamed": renamed, "unchanged": unchanged, "slug_conflicts": conflicts}


def rename_slug(old_slug: str, new_slug: str) -> str:
    """Move a badge to a new slug, remembering the old one as an alias.

    Keeping the old slug as an alias is what stops the next sync treating the badge
    as new and re-inserting it. Returns "renamed", "unchanged", or "conflict" when
    the target slug is taken by a different badge.
    """
    if old_slug == new_slug:
        return "unchanged"
    coll = collection()
    if coll.find_one({"slug": new_slug}):
        return "conflict"
    result = coll.update_one(
        {"slug": old_slug},
        {"$set": {"slug": new_slug}, "$addToSet": {"aliases": old_slug}},
    )
    return "renamed" if result.matched_count == 1 else "conflict"


def set_credly_title(slug: str, title: str, source_url: str) -> bool:
    """Record the title shown on the badge's own Credly page.

    The source URL is stored alongside so a re-verification only refetches when the
    Credly page itself changed.
    """
    result = collection().update_one(
        {"slug": slug},
        {
            "$set": {
                "credly_title": title,
                "credly_title_source_url": source_url,
                "credly_title_checked_at": datetime.now(timezone.utc),
            }
        },
    )
    return result.matched_count == 1


def badges_needing_credly_title() -> list[dict[str, Any]]:
    """Badges whose Credly page title is missing or was read from a different URL."""
    return [
        doc
        for doc in collection().find(
            {"credly_url": {"$exists": True}},
            {
                "_id": False,
                "slug": True,
                "credly_url": True,
                "credly_title": True,
                "credly_title_source_url": True,
            },
        )
        if doc.get("credly_url")
        and (
            not doc.get("credly_title")
            or doc.get("credly_title_source_url") != doc.get("credly_url")
        )
    ]


def set_mongodb_title(slug: str, title: str, source_url: str) -> bool:
    """Record the title learn.mongodb.com gives this badge."""
    result = collection().update_one(
        {"slug": slug},
        {
            "$set": {
                "mongodb_title": title,
                "mongodb_title_source_url": source_url,
                "mongodb_title_checked_at": datetime.now(timezone.utc),
            }
        },
    )
    return result.matched_count == 1


def badges_needing_mongodb_title() -> list[dict[str, Any]]:
    """Badges whose MongoDB title is missing or was read for a different URL."""
    return [
        doc
        for doc in collection().find(
            {"mongodb_url": {"$exists": True}},
            {
                "_id": False,
                "slug": True,
                "name": True,
                "mongodb_url": True,
                "mongodb_title": True,
                "mongodb_title_source_url": True,
            },
        )
        if doc.get("mongodb_url")
        and (
            not doc.get("mongodb_title")
            or doc.get("mongodb_title_source_url") != doc.get("mongodb_url")
        )
    ]
