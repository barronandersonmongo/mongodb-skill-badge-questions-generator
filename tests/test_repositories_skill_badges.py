"""Tests for app/repositories/skill_badges.py — badge persistence.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from datetime import datetime, timezone

import pytest

from app.models.skill_badge import DiscoveredBadge
from app.repositories import skill_badges


def badge(slug: str, **overrides) -> DiscoveredBadge:
    return DiscoveredBadge(
        **{
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "description": f"Covers {slug}.",
            "confidence": "high",
            **overrides,
        }
    )


# --- ensure_indexes ---


def test_ensure_indexes_creates_a_unique_slug_index(fake_collection):
    """
    Intent: slug is the badge's identity. A unique index is what makes repeated
        discovery runs idempotent instead of accumulating duplicate badges.
    Success: An index named slug_unique exists with unique=True.
    Feature: Badge persistence — idempotent upserts.
    """
    skill_badges.ensure_indexes()
    slug_index = next(i for i in fake_collection.indexes if i["name"] == "slug_unique")
    assert slug_index["unique"] is True


def test_ensure_indexes_supports_the_documented_query_fields(fake_collection):
    """
    Intent: The admin view filters badges by status and the question tooling
        queries by category, so both must be indexed rather than collection-scanned.
    Success: Indexes exist for slug, categories, and status.
    Feature: Badge persistence — queryability by category and status.
    """
    skill_badges.ensure_indexes()
    names = {i["name"] for i in fake_collection.indexes}
    assert {"slug_unique", "categories", "status"} <= names


# --- upsert_badges ---


def test_upsert_inserts_new_badges(fake_collection):
    """
    Intent: A first discovery run must persist every badge found and report an
        accurate summary the admin page can display.
    Success: Both badges are stored; the summary reports 2 inserted, 0 modified,
        and lists both slugs.
    Feature: Badge discovery — writing results to MongoDB.
    """
    summary = skill_badges.upsert_badges([badge("atlas-search"), badge("aggregation")])
    assert summary["inserted"] == 2
    assert summary["modified"] == 0
    assert set(summary["slugs"]) == {"atlas-search", "aggregation"}
    assert fake_collection.count_documents({}) == 2


def test_upsert_stamps_provenance_on_every_document(fake_collection):
    """
    Intent: Claude's output is research, not authority. Each stored badge must be
        traceable to the run and moment that produced it so a reviewer can audit it.
    Success: The stored doc carries the summary's run_id and a discovered_at
        timestamp at or after the start of the call.
    Feature: Badge lifecycle — auditable provenance.
    """
    before = datetime.now(timezone.utc)
    summary = skill_badges.upsert_badges([badge("atlas-search")])
    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert doc["discovery_run_id"] == summary["run_id"]
    assert doc["discovered_at"] >= before


def test_new_badges_land_as_candidates(fake_collection):
    """
    Intent: Nothing Claude discovers is trusted on arrival; a human promotes it.
        New badges must therefore enter review as candidates.
    Success: A newly inserted badge has status "candidate".
    Feature: Badge lifecycle — human approval gate.
    """
    skill_badges.upsert_badges([badge("atlas-search")])
    assert fake_collection.find_one({"slug": "atlas-search"})["status"] == "candidate"


def test_rerun_updates_fields_but_preserves_a_human_approval(fake_collection):
    """
    Intent: Re-running discovery must refresh badge facts without undoing review
        decisions — a re-run may never demote a badge a human already approved.
    Success: The description is updated, the summary reports a match rather than an
        insert, and status stays "approved".
    Feature: Badge lifecycle — human approval survives re-discovery.
    """
    skill_badges.upsert_badges([badge("atlas-search")])
    skill_badges.set_status("atlas-search", "approved")

    summary = skill_badges.upsert_badges(
        [badge("atlas-search", description="Updated description.")]
    )

    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert summary["inserted"] == 0
    assert summary["matched"] == 1
    assert doc["status"] == "approved"
    assert doc["description"] == "Updated description."


def test_rerun_does_not_retire_badges_absent_from_this_run(fake_collection):
    """
    Intent: A run that misses a badge is a research gap (a failed search, a moved
        page), not evidence MongoDB retired the badge. Absence must never delete or
        retire existing records.
    Success: The badge missing from the second run is still present and untouched.
    Feature: Badge lifecycle — no automatic retirement.
    """
    skill_badges.upsert_badges([badge("atlas-search"), badge("aggregation")])
    skill_badges.upsert_badges([badge("atlas-search")])

    assert fake_collection.count_documents({}) == 2
    assert fake_collection.find_one({"slug": "aggregation"})["status"] == "candidate"


def test_upsert_is_keyed_on_slug_not_name(fake_collection):
    """
    Intent: MongoDB renames badges (e.g. adding a year). Keying on slug means a
        rename updates the existing badge instead of creating a near-duplicate.
    Success: After a re-run with a changed display name, one document exists and
        carries the new name.
    Feature: Badge persistence — identity is the slug.
    """
    skill_badges.upsert_badges([badge("atlas-search", name="Atlas Search")])
    skill_badges.upsert_badges([badge("atlas-search", name="Atlas Search (2026)")])
    assert fake_collection.count_documents({}) == 1
    assert fake_collection.find_one({"slug": "atlas-search"})["name"] == (
        "Atlas Search (2026)"
    )


def test_upsert_of_empty_list_is_a_no_op(fake_collection):
    """
    Intent: A run that finds nothing must touch nothing — no writes, no index
        creation, no run_id — and report a zeroed summary the admin page can render.
    Success: An all-zero summary with run_id None, no documents, and no indexes.
    Feature: Badge discovery — safe handling of an empty result.
    """
    summary = skill_badges.upsert_badges([])
    assert summary["run_id"] is None
    assert (summary["matched"], summary["inserted"], summary["modified"]) == (0, 0, 0)
    assert summary["slugs"] == []
    assert fake_collection.count_documents({}) == 0
    assert fake_collection.indexes == []


def test_upsert_persists_the_fields_a_reviewer_judges_by(fake_collection):
    """
    Intent: A reviewer decides whether to trust a badge from its cited sources, its
        confidence, and the topic areas it covers, so all of those must survive the
        write rather than being dropped as extra fields.
    Success: categories, source_urls, and confidence all match what was discovered.
    Feature: Badge lifecycle — reviewable sourcing metadata.
    """
    skill_badges.upsert_badges(
        [
            badge(
                "atlas-search",
                categories=["search"],
                source_urls=["https://learn.mongodb.com/x"],
                confidence="low",
            )
        ]
    )
    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert doc["categories"] == ["search"]
    assert doc["source_urls"] == ["https://learn.mongodb.com/x"]
    assert doc["confidence"] == "low"


# --- list_badges ---


def test_list_badges_hides_the_mongo_id_and_sorts_by_name(fake_collection):
    """
    Intent: The listing feeds an HTTP response and an admin table, so it must be
        JSON-serializable (no ObjectId) and in a stable, human-scannable order.
    Success: Results are name-sorted and no document contains _id.
    Feature: Badge review — admin listing.
    """
    skill_badges.upsert_badges([badge("zed"), badge("alpha")])
    docs = skill_badges.list_badges()
    assert [d["slug"] for d in docs] == ["alpha", "zed"]
    assert all("_id" not in d for d in docs)


def test_list_badges_filters_by_status(fake_collection):
    """
    Intent: Reviewers work a queue — "what still needs approval" vs "what is
        approved" — so listing must filter by status.
    Success: Each status filter returns exactly the badges in that state.
    Feature: Badge review — filtering by status.
    """
    skill_badges.upsert_badges([badge("a"), badge("b")])
    skill_badges.set_status("a", "approved")

    assert [d["slug"] for d in skill_badges.list_badges("approved")] == ["a"]
    assert [d["slug"] for d in skill_badges.list_badges("candidate")] == ["b"]


# --- set_status ---


def test_set_status_reports_success_and_failure(fake_collection):
    """
    Intent: Promotion and retirement are the human review actions, and the caller
        must be able to tell a real update from a miss so the API can return 404.
    Success: True for an existing slug (with the status persisted), False for an
        unknown slug.
    Feature: Badge review — promote / retire.
    """
    skill_badges.upsert_badges([badge("atlas-search")])
    assert skill_badges.set_status("atlas-search", "retired") is True
    assert skill_badges.set_status("does-not-exist", "retired") is False
    assert fake_collection.find_one({"slug": "atlas-search"})["status"] == "retired"


def test_set_status_does_not_create_a_document(fake_collection):
    """
    Intent: A status change is a review action on a known badge. A typo'd slug must
        not conjure a badge that no run ever discovered.
    Success: Setting status on an unknown slug leaves the collection empty.
    Feature: Badge review — promote / retire.
    """
    skill_badges.set_status("ghost", "approved")
    assert fake_collection.count_documents({}) == 0


# --- collection wiring ---


def test_collection_name_comes_from_settings(monkeypatch):
    """
    Intent: The collection name must be read from settings, so the storage target
        is configurable in one place rather than hardcoded at each call site.
    Success: The repository asks the database for the "skill_badges" collection and
        returns that handle.
    Feature: Badge persistence — configurable storage target.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    captured = {}

    class FakeDatabase:
        def __getitem__(self, name):
            captured["name"] = name
            return "collection-handle"

    monkeypatch.setattr(skill_badges, "get_database", lambda: FakeDatabase())
    assert skill_badges.collection() == "collection-handle"
    assert captured["name"] == "skill_badges"


# --- title corrections and identity ---


def test_a_corrected_title_survives_a_later_discovery_run(fake_collection):
    """
    Intent: Some researched titles are wrong, so a human corrects them in the admin
        screen. A later run must not silently restore the wrong title, or the
        correction is pointless and the reviewer's work is lost on every re-pull.
    Success: After set_name, a re-run with the original researched title leaves the
        corrected name in place.
    Feature: Badge review — hand-corrected titles are protected.
    """
    skill_badges.upsert_badges([badge("atlas-search", name="Altas Search")])
    skill_badges.set_name("atlas-search", "Atlas Search Fundamentals")

    skill_badges.upsert_badges([badge("atlas-search", name="Altas Search")])

    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert doc["name"] == "Atlas Search Fundamentals"
    assert doc["name_locked"] is True


def test_an_uncorrected_title_is_refreshed_by_a_discovery_run(fake_collection):
    """
    Intent: Titles that no human has touched should track what MongoDB publishes, so
        a rename upstream flows through on the next run.
    Success: A re-run with a new title updates a badge whose name was never locked.
    Feature: Badge synchronisation — refreshing researched fields.
    """
    skill_badges.upsert_badges([badge("atlas-search", name="Atlas Search")])
    skill_badges.upsert_badges([badge("atlas-search", name="Atlas Search (2026)")])

    assert fake_collection.find_one({"slug": "atlas-search"})["name"] == (
        "Atlas Search (2026)"
    )


def test_set_name_reports_success_and_failure(fake_collection):
    """
    Intent: The rename endpoint must be able to tell a real correction from a stale
        slug so it can return 404 rather than silently doing nothing.
    Success: True for a stored badge, False for an unknown slug, and no document is
        created for the unknown one.
    Feature: Badge review — editing a badge title.
    """
    skill_badges.upsert_badges([badge("atlas-search")])
    assert skill_badges.set_name("atlas-search", "Corrected") is True
    assert skill_badges.set_name("nope", "Corrected") is False
    assert fake_collection.count_documents({}) == 1


def test_a_matched_badge_updates_the_existing_record_instead_of_duplicating(
    fake_collection,
):
    """
    Intent: When the model recognises a re-discovered badge as one already stored
        under a different slug, the write must land on the existing record. Inserting
        it would leave two rows for one badge — exactly what a reviewer corrected the
        title to avoid.
    Success: One document remains, carrying the freshly researched description and
        its corrected title, with no record under the discovered slug.
    Feature: Badge synchronisation — merging a re-discovered badge.
    """
    skill_badges.upsert_badges([badge("atlas-search-fundamentals", name="Altas Search")])
    skill_badges.set_name("atlas-search-fundamentals", "Atlas Search Fundamentals")

    summary = skill_badges.upsert_badges(
        [badge("atlas-search-basics", description="Refreshed description.")],
        matches={"atlas-search-basics": "atlas-search-fundamentals"},
    )

    assert fake_collection.count_documents({}) == 1
    assert fake_collection.find_one({"slug": "atlas-search-basics"}) is None
    doc = fake_collection.find_one({"slug": "atlas-search-fundamentals"})
    assert doc["description"] == "Refreshed description."
    assert doc["name"] == "Atlas Search Fundamentals"
    assert summary["merged"] == {"atlas-search-basics": "atlas-search-fundamentals"}


def test_a_merged_slug_is_remembered_as_an_alias(fake_collection):
    """
    Intent: Remembering the slug a badge was discovered under means the next run
        recognises it by lookup instead of paying for another model comparison — and
        it records for a reviewer why two names refer to one badge.
    Success: The discovered slug appears in the record's aliases and in known_slugs().
    Feature: Badge synchronisation — remembering merged identities.
    """
    skill_badges.upsert_badges([badge("atlas-search-fundamentals")])
    skill_badges.upsert_badges(
        [badge("atlas-search-basics")],
        matches={"atlas-search-basics": "atlas-search-fundamentals"},
    )

    doc = fake_collection.find_one({"slug": "atlas-search-fundamentals"})
    assert doc["aliases"] == ["atlas-search-basics"]
    assert "atlas-search-basics" in skill_badges.known_slugs()


def test_an_alias_is_recorded_once_however_many_runs_see_it(fake_collection):
    """
    Intent: A badge re-discovered under the same wrong slug on every run must not grow
        a duplicate-filled alias list.
    Success: Repeating the same merge leaves a single alias entry.
    Feature: Badge synchronisation — remembering merged identities.
    """
    skill_badges.upsert_badges([badge("atlas-search-fundamentals")])
    for _ in range(3):
        skill_badges.upsert_badges(
            [badge("atlas-search-basics")],
            matches={"atlas-search-basics": "atlas-search-fundamentals"},
        )

    assert fake_collection.find_one({"slug": "atlas-search-fundamentals"})["aliases"] == [
        "atlas-search-basics"
    ]


def test_known_slugs_covers_stored_slugs_and_aliases(fake_collection):
    """
    Intent: The synchronisation step uses this set to decide which badges even need a
        model comparison. Missing an alias would send a known badge back through
        matching; missing a slug would risk duplicating it.
    Success: Both the stored slug and its alias are reported as known.
    Feature: Badge synchronisation — avoiding pointless model calls.
    """
    skill_badges.upsert_badges([badge("a"), badge("b")])
    skill_badges.upsert_badges([badge("c")], matches={"c": "a"})

    assert skill_badges.known_slugs() == {"a", "b", "c"}


def test_known_slugs_is_empty_for_a_fresh_collection(fake_collection):
    """
    Intent: On a first run there are no known slugs, which is what lets the
        synchronisation step skip the model comparison entirely.
    Success: An empty set is returned.
    Feature: Badge synchronisation — avoiding pointless model calls.
    """
    assert skill_badges.known_slugs() == set()


# --- curated reference links ---


def test_curated_links_survive_a_later_discovery_run(fake_collection):
    """
    Intent: A reviewer removes a link that points at the wrong badge, or adds the
        catalog page a run failed to cite. A later run must not undo that work, or
        curating links is pointless.
    Success: After set_source_urls, a re-run citing the original link leaves the
        curated list in place.
    Feature: Badge review — curated reference links are protected.
    """
    skill_badges.upsert_badges(
        [badge("atlas-search", source_urls=["https://www.credly.com/wrong-badge"])]
    )
    skill_badges.set_source_urls(
        "atlas-search", ["https://learn.mongodb.com/skills/atlas-search"]
    )

    skill_badges.upsert_badges(
        [badge("atlas-search", source_urls=["https://www.credly.com/wrong-badge"])]
    )

    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert doc["source_urls"] == ["https://learn.mongodb.com/skills/atlas-search"]
    assert doc["sources_locked"] is True


def test_uncurated_links_are_refreshed_by_a_discovery_run(fake_collection):
    """
    Intent: Links nobody has curated should track what research finds, so a badge that
        gains a catalog page picks it up on the next run.
    Success: A re-run replaces the links of a badge whose sources were never locked.
    Feature: Badge synchronisation — refreshing researched fields.
    """
    skill_badges.upsert_badges([badge("atlas-search", source_urls=["https://old"])])
    skill_badges.upsert_badges([badge("atlas-search", source_urls=["https://new"])])

    assert fake_collection.find_one({"slug": "atlas-search"})["source_urls"] == [
        "https://new"
    ]


def test_links_can_be_cleared_entirely(fake_collection):
    """
    Intent: Every cited link may be wrong, and a reviewer must be able to say so —
        an empty list is a meaningful statement that nothing verifies this badge.
    Success: Setting an empty list stores it and locks the field.
    Feature: Badge review — curated reference links.
    """
    skill_badges.upsert_badges([badge("atlas-search", source_urls=["https://wrong"])])
    assert skill_badges.set_source_urls("atlas-search", []) is True

    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert doc["source_urls"] == []
    assert doc["sources_locked"] is True


def test_set_source_urls_reports_an_unknown_slug(fake_collection):
    """
    Intent: The endpoint must distinguish a real edit from a stale slug so it can
        return 404 rather than silently discarding the reviewer's work.
    Success: False for an unknown slug, and no document is created.
    Feature: Badge review — curated reference links.
    """
    assert skill_badges.set_source_urls("nope", ["https://x"]) is False
    assert fake_collection.count_documents({}) == 0


# --- deletion ---


def test_a_retired_badge_can_be_deleted(fake_collection):
    """
    Intent: Retired badges accumulate — wrong entries, things that were never badges —
        and a reviewer needs a way to clear them out so the retired list stays a
        meaningful record rather than a junk drawer.
    Success: A retired badge is removed from the collection and reported deleted.
    Feature: Badge review — deleting a retired badge.
    """
    skill_badges.upsert_badges([badge("retired-thing")])
    skill_badges.set_status("retired-thing", "retired")

    assert skill_badges.delete_badge("retired-thing") is True
    assert fake_collection.count_documents({}) == 0


def test_a_badge_that_is_not_retired_cannot_be_deleted(fake_collection):
    """
    Intent: Deletion is the one irreversible action here, so it must require the
        reversible step first. A single mis-click in the candidate queue must not be
        able to destroy a badge and its curated title and links.
    Success: Deleting a candidate or approved badge fails and the record survives.
    Feature: Badge review — deletion requires retirement first.
    """
    skill_badges.upsert_badges([badge("atlas-search"), badge("approved-one")])
    skill_badges.set_status("approved-one", "approved")

    assert skill_badges.delete_badge("atlas-search") is False
    assert skill_badges.delete_badge("approved-one") is False
    assert fake_collection.count_documents({}) == 2


def test_deleting_an_unknown_badge_reports_failure(fake_collection):
    """
    Intent: The endpoint must distinguish "deleted" from "was never there" so it can
        answer 404 rather than implying it destroyed something.
    Success: False is returned for an unknown slug.
    Feature: Badge review — deleting a retired badge.
    """
    assert skill_badges.delete_badge("never-existed") is False


# --- vector index, embeddings and merging ---


def test_merging_keeps_the_survivor_and_remembers_the_other_slug(fake_collection):
    """
    Intent: A merge resolves a duplicate, so the dropped record must disappear while its
        slug stays known — otherwise the next sync re-creates it as a new badge and the
        duplicate returns.
    Success: Only the survivor remains, carrying the dropped slug as an alias, and
        known_slugs still answers to it.
    Feature: Duplicate resolution — merging duplicates.
    """
    skill_badges.upsert_badges([badge("keep-me"), badge("drop-me")])

    assert skill_badges.merge_badges("drop-me", "keep-me") is True
    assert fake_collection.find_one({"slug": "drop-me"}) is None
    assert fake_collection.find_one({"slug": "keep-me"})["aliases"] == ["drop-me"]
    assert "drop-me" in skill_badges.known_slugs()


def test_merging_never_overwrites_curated_fields_on_the_survivor(fake_collection):
    """
    Intent: A merge must not be able to lose review work — the whole point of keeping the
        curated record is that its corrected title and links survive.
    Success: The survivor's locked title and curated links are unchanged after the merge.
    Feature: Duplicate resolution — protecting curated records.
    """
    skill_badges.upsert_badges([badge("keep-me"), badge("drop-me")])
    skill_badges.set_name("keep-me", "Curated Title")
    skill_badges.set_source_urls("keep-me", ["https://learn.mongodb.com/kept"])

    skill_badges.merge_badges("drop-me", "keep-me")

    doc = fake_collection.find_one({"slug": "keep-me"})
    assert doc["name"] == "Curated Title"
    assert doc["source_urls"] == ["https://learn.mongodb.com/kept"]


def test_merging_fills_gaps_on_the_survivor(fake_collection):
    """
    Intent: The dropped record may hold canonical identity the survivor lacks — a Credly
        URL or an artwork title. Deleting it should not throw that away.
    Success: A field missing on the survivor is filled from the dropped record.
    Feature: Duplicate resolution — merging duplicates.
    """
    skill_badges.upsert_badges([badge("keep-me")])
    skill_badges.upsert_badges(
        [badge("drop-me", credly_url="https://www.credly.com/org/mongodb/badge/x")]
    )

    skill_badges.merge_badges("drop-me", "keep-me")
    assert fake_collection.find_one({"slug": "keep-me"})["credly_url"] == (
        "https://www.credly.com/org/mongodb/badge/x"
    )


def test_a_badge_cannot_be_merged_into_itself_or_a_missing_one(fake_collection):
    """
    Intent: A self-merge would delete the record it was meant to keep, and merging into a
        slug that does not exist would delete the source with nowhere to put its data.
    Success: Both are refused and nothing is deleted.
    Feature: Duplicate resolution — safe merges.
    """
    skill_badges.upsert_badges([badge("only-one")])

    assert skill_badges.merge_badges("only-one", "only-one") is False
    assert skill_badges.merge_badges("only-one", "does-not-exist") is False
    assert fake_collection.count_documents({}) == 1


def test_the_artwork_title_is_stored_and_protected(fake_collection):
    """
    Intent: The artwork title is the canonical name, so it must be stored as the badge's
        name — but a human correction still outranks it, or a re-sync would undo the fix.
    Success: The artwork title becomes the name; after a manual correction a re-sync
        leaves the correction in place.
    Feature: Badge titles — artwork title as the canonical name.
    """
    skill_badges.upsert_badges(
        [badge("mongodb-overview", name="MongoDB Overview", image_title="MongoDB Overview")]
    )
    assert fake_collection.find_one({"slug": "mongodb-overview"})["image_title"] == (
        "MongoDB Overview"
    )

    skill_badges.set_name("mongodb-overview", "Reviewer's Title")
    skill_badges.upsert_badges(
        [badge("mongodb-overview", name="MongoDB Overview", image_title="MongoDB Overview")]
    )
    assert fake_collection.find_one({"slug": "mongodb-overview"})["name"] == (
        "Reviewer's Title"
    )


def test_both_canonical_urls_are_stored(fake_collection):
    """
    Intent: The truth about a badge lives on two pages — its Credly badge page and its
        learn.mongodb.com page — and a reviewer needs both to check a questionable badge.
    Success: Both URLs are stored as their own fields.
    Feature: Badge review — canonical links.
    """
    skill_badges.upsert_badges(
        [
            badge(
                "mongodb-overview",
                credly_url="https://www.credly.com/org/mongodb/badge/overview",
                mongodb_url="https://learn.mongodb.com/courses/overview",
            )
        ]
    )
    doc = fake_collection.find_one({"slug": "mongodb-overview"})
    assert doc["credly_url"] == "https://www.credly.com/org/mongodb/badge/overview"
    assert doc["mongodb_url"] == "https://learn.mongodb.com/courses/overview"


def test_a_slug_already_recorded_as_an_alias_updates_its_owner(fake_collection):
    """
    Intent: Once a badge has been merged, its old slug is remembered as an alias — which
        makes it "known", so identity matching skips it. The upsert must then redirect it
        to the owning record itself, or a later sync silently re-creates the duplicate
        that the merge just resolved.
    Success: Re-discovering the aliased slug updates the owning record and inserts nothing.
    Feature: Badge synchronisation — remembered merges stay merged.
    """
    skill_badges.upsert_badges([badge("canonical")])
    skill_badges.upsert_badges([badge("old-slug")], matches={"old-slug": "canonical"})

    summary = skill_badges.upsert_badges(
        [badge("old-slug", description="Refreshed again.")]
    )

    assert summary["inserted"] == 0
    assert fake_collection.count_documents({}) == 1
    assert fake_collection.find_one({"slug": "canonical"})["description"] == (
        "Refreshed again."
    )


def test_the_similarity_search_sends_text_to_the_auto_embedding_index(fake_collection):
    """
    Intent: The Atlas index embeds `description` itself, so the query must be the text on
        that path — a queryVector would be rejected by an autoEmbed index, and storing
        vectors here would duplicate what Atlas already does.
    Success: The nearest badge is returned for a text query, excluding the badge itself.
    Feature: Duplicate detection — querying the Atlas auto-embedding index.
    """
    skill_badges.upsert_badges(
        [
            badge("atlas-search", description="Covers search index basics."),
            badge("sharding", description="Covers shard key selection."),
        ]
    )

    results = skill_badges.similar_by_description(
        "Covers search index basics.",
        "skill-badge-description-vector",
        limit=5,
        exclude_slug="atlas-search",
    )
    assert [r["slug"] for r in results] == ["sharding"]
    assert results[0]["score"] >= 0


# --- badge artwork ---


def test_artwork_is_stored_on_the_document_and_read_back(fake_collection):
    """
    Intent: The artwork is what lets a person recognise a badge, and hotlinking Credly
        breaks when their asset URLs change — so the bytes must live in the badge document
        and be readable back with their content type.
    Success: The stored bytes and content type are returned for the badge.
    Feature: Badge artwork — storing the image in the document.
    """
    skill_badges.upsert_badges([badge("mongodb-overview")])
    assert skill_badges.set_image(
        "mongodb-overview", b"\x89PNG", "image/png", "https://images.credly.com/x"
    ) is True

    assert skill_badges.get_image("mongodb-overview") == (b"\x89PNG", "image/png")


def test_a_badge_without_artwork_reports_none(fake_collection):
    """
    Intent: The image endpoint must be able to answer 404 rather than serving an empty body
        that renders as a broken image.
    Success: None is returned for a badge with no stored artwork and for an unknown slug.
    Feature: Badge artwork — storing the image in the document.
    """
    skill_badges.upsert_badges([badge("mongodb-overview")])
    assert skill_badges.get_image("mongodb-overview") is None
    assert skill_badges.get_image("does-not-exist") is None


def test_listings_never_carry_the_artwork_bytes(fake_collection):
    """
    Intent: Every page render lists the whole collection. Including the image bytes would
        pull megabytes through the app for images the browser fetches separately anyway.
    Success: The listing reports that an image exists but omits the bytes.
    Feature: Badge artwork — keeping listings small.
    """
    skill_badges.upsert_badges([badge("mongodb-overview")])
    skill_badges.set_image(
        "mongodb-overview", b"\x89PNG", "image/png", "https://images.credly.com/x"
    )

    doc = skill_badges.list_badges()[0]
    assert doc["image_stored"] is True
    assert "image_data" not in doc


def test_only_badges_whose_artwork_moved_are_re_downloaded(fake_collection):
    """
    Intent: Re-downloading every image on every sync wastes time and bandwidth. Only a
        badge with no artwork, or whose image URL changed, needs fetching.
    Success: A badge with a new image URL is pending; once stored it is not; changing the
        URL makes it pending again.
    Feature: Badge artwork — fetching only what changed.
    """
    skill_badges.upsert_badges(
        [badge("mongodb-overview", image_url="https://images.credly.com/one")]
    )
    assert [d["slug"] for d in skill_badges.badges_needing_art()] == ["mongodb-overview"]

    skill_badges.set_image(
        "mongodb-overview", b"\x89PNG", "image/png", "https://images.credly.com/one"
    )
    assert skill_badges.badges_needing_art() == []

    skill_badges.upsert_badges(
        [badge("mongodb-overview", image_url="https://images.credly.com/two")]
    )
    assert [d["slug"] for d in skill_badges.badges_needing_art()] == ["mongodb-overview"]


def test_a_badge_with_no_image_url_is_never_pending(fake_collection):
    """
    Intent: Some badges may have no artwork at all. They must not be retried on every sync
        as though their download had failed.
    Success: A badge without an image URL is not reported as needing artwork.
    Feature: Badge artwork — fetching only what changed.
    """
    skill_badges.upsert_badges([badge("no-art")])
    assert skill_badges.badges_needing_art() == []


# --- slugs derived from titles ---


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Vector Search Fundamentals", "vector-search-fundamentals"),
        ("Data Resilience: Atlas", "data-resilience-atlas"),
        ("Performance Tools & Techniques", "performance-tools-techniques"),
        ("Secure MongoDB Self-Managed: AuthN & AuthZ", "secure-mongodb-self-managed-authn-authz"),
        ("  Padded  Title  ", "padded-title"),
    ],
)
def test_a_slug_is_derived_from_the_title(name, expected):
    """
    Intent: The slug is the badge's identity and appears in URLs, so it must be derived
        from the reviewed title in a predictable, URL-safe way — lowercase, hyphenated,
        with punctuation dropped rather than escaped.
    Success: Each title maps to its expected slug.
    Feature: Badge identity — slug derived from the title.
    """
    assert skill_badges.slug_from_name(name) == expected


def test_renaming_a_slug_remembers_the_old_one(fake_collection):
    """
    Intent: The slug is what a sync matches on, so changing it must leave the old slug
        recorded as an alias — otherwise the next sync sees an unknown badge and inserts a
        duplicate of the one just renamed.
    Success: The badge moves to the new slug, the old slug becomes an alias, and
        known_slugs still answers to it.
    Feature: Badge identity — renaming a slug safely.
    """
    skill_badges.upsert_badges([badge("old-name")])

    assert skill_badges.rename_slug("old-name", "new-name") == "renamed"
    assert fake_collection.find_one({"slug": "old-name"}) is None
    doc = fake_collection.find_one({"slug": "new-name"})
    assert doc["aliases"] == ["old-name"]
    assert "old-name" in skill_badges.known_slugs()


def test_renaming_onto_an_existing_slug_is_refused(fake_collection):
    """
    Intent: Two badges whose titles reduce to the same slug must not silently collapse into
        one record — that would delete a real badge without review.
    Success: The rename is refused and both records survive unchanged.
    Feature: Badge identity — renaming a slug safely.
    """
    skill_badges.upsert_badges([badge("first"), badge("second")])

    assert skill_badges.rename_slug("first", "second") == "conflict"
    assert fake_collection.count_documents({}) == 2
    assert fake_collection.find_one({"slug": "first"}) is not None


def test_renaming_to_the_same_slug_changes_nothing(fake_collection):
    """
    Intent: Re-running the rename over an already-correct collection must be a no-op, not
        add a badge's own slug to its alias list as if it had been renamed.
    Success: "unchanged" is reported and no alias is recorded.
    Feature: Badge identity — renaming a slug safely.
    """
    skill_badges.upsert_badges([badge("same")])

    assert skill_badges.rename_slug("same", "same") == "unchanged"
    assert fake_collection.find_one({"slug": "same"}).get("aliases") is None


def test_renaming_an_unknown_slug_is_reported(fake_collection):
    """
    Intent: A stale slug must be reported rather than silently creating a record under the
        new name.
    Success: "conflict" is reported and the collection stays empty.
    Feature: Badge identity — renaming a slug safely.
    """
    assert skill_badges.rename_slug("ghost", "new-name") == "conflict"
    assert fake_collection.count_documents({}) == 0


# --- Credly page titles ---


def test_the_credly_page_title_is_stored_separately(fake_collection):
    """
    Intent: Three sources name these badges differently — the collection API, the artwork,
        and the badge's own Credly page. The page title is recorded in its own field so a
        reviewer can compare all three rather than having one silently overwrite another.
    Success: The page title is stored without disturbing the badge's name or artwork title.
    Feature: Badge titles — Credly page title.
    """
    skill_badges.upsert_badges(
        [badge("ai-agents-with-mongodb", name="AI Agents with MongoDB", image_title="AI Agents with MongoDB")]
    )

    assert skill_badges.set_credly_title(
        "ai-agents-with-mongodb",
        "Building AI Agents with MongoDB",
        "https://www.credly.com/org/mongodb/badge/building-ai-agents-with-mongodb",
    ) is True

    doc = fake_collection.find_one({"slug": "ai-agents-with-mongodb"})
    assert doc["credly_title"] == "Building AI Agents with MongoDB"
    assert doc["name"] == "AI Agents with MongoDB"
    assert doc["image_title"] == "AI Agents with MongoDB"


def test_only_badges_with_an_unverified_credly_page_are_refetched(fake_collection):
    """
    Intent: Re-reading every Credly page on every sync is wasted traffic. Only a badge with
        no recorded page title, or whose Credly URL changed since it was read, needs it.
    Success: A new badge is pending; once verified it is not; changing its Credly URL makes
        it pending again.
    Feature: Badge titles — verifying only what changed.
    """
    skill_badges.upsert_badges(
        [badge("one", credly_url="https://www.credly.com/org/mongodb/badge/one")]
    )
    assert [d["slug"] for d in skill_badges.badges_needing_credly_title()] == ["one"]

    skill_badges.set_credly_title(
        "one", "Badge One", "https://www.credly.com/org/mongodb/badge/one"
    )
    assert skill_badges.badges_needing_credly_title() == []

    skill_badges.upsert_badges(
        [badge("one", credly_url="https://www.credly.com/org/mongodb/badge/one-v2")]
    )
    assert [d["slug"] for d in skill_badges.badges_needing_credly_title()] == ["one"]


def test_a_badge_without_a_credly_url_is_never_pending(fake_collection):
    """
    Intent: Badges outside the Credly collection have no page to read, and must not be
        retried on every sync as though verification had failed.
    Success: A badge with no Credly URL is not reported as needing verification.
    Feature: Badge titles — verifying only what changed.
    """
    skill_badges.upsert_badges([badge("no-credly")])
    assert skill_badges.badges_needing_credly_title() == []


# --- learn.mongodb.com titles ---


def test_the_mongodb_title_is_stored_in_its_own_field(fake_collection):
    """
    Intent: MongoDB, Credly and the artwork all name a badge differently. Keeping the
        MongoDB title separate lets a reviewer compare all four names rather than having
        the newest source overwrite the others.
    Success: The MongoDB title is stored without disturbing the name, artwork or Credly
        titles.
    Feature: Badge titles — learn.mongodb.com title.
    """
    skill_badges.upsert_badges([badge("memory-for-ai-applications", name="Memory for AI Applications")])
    skill_badges.set_credly_title("memory-for-ai-applications", "Memory for AI Applications with MongoDB", "https://www.credly.com/x")

    assert skill_badges.set_mongodb_title(
        "memory-for-ai-applications",
        "Memory for AI Applications Skill Badge",
        "https://learn.mongodb.com/courses/memory-for-ai-applications",
    ) is True

    doc = fake_collection.find_one({"slug": "memory-for-ai-applications"})
    assert doc["mongodb_title"] == "Memory for AI Applications Skill Badge"
    assert doc["credly_title"] == "Memory for AI Applications with MongoDB"
    assert doc["name"] == "Memory for AI Applications"


def test_only_badges_with_an_unverified_mongodb_title_are_looked_up(fake_collection):
    """
    Intent: Each lookup is a search-backed model call, so repeating it for badges already
        verified would make every sync slower and more expensive for no new information.
    Success: A new badge is pending; once verified it is not; changing its MongoDB URL
        makes it pending again.
    Feature: Badge titles — verifying only what changed.
    """
    skill_badges.upsert_badges(
        [badge("one", mongodb_url="https://learn.mongodb.com/courses/one")]
    )
    assert [d["slug"] for d in skill_badges.badges_needing_mongodb_title()] == ["one"]

    skill_badges.set_mongodb_title("one", "One Skill Badge", "https://learn.mongodb.com/courses/one")
    assert skill_badges.badges_needing_mongodb_title() == []

    skill_badges.upsert_badges(
        [badge("one", mongodb_url="https://learn.mongodb.com/courses/one-v2")]
    )
    assert [d["slug"] for d in skill_badges.badges_needing_mongodb_title()] == ["one"]


def test_a_badge_without_a_mongodb_url_is_never_looked_up(fake_collection):
    """
    Intent: A badge with no learn.mongodb.com page has nothing to look up, and must not be
        retried on every sync as though the lookup had failed.
    Success: It is not reported as needing a MongoDB title.
    Feature: Badge titles — verifying only what changed.
    """
    skill_badges.upsert_badges([badge("no-mongodb-page")])
    assert skill_badges.badges_needing_mongodb_title() == []


def test_the_desired_slug_comes_from_the_artwork_title(fake_collection):
    """
    Intent: The artwork is the authority for a badge's name, so it must also be the
        authority for its identity — a slug taken from the catalog wording would drift from
        the name a reviewer sees on the badge.
    Success: The slug is derived from the artwork title, not the catalog or in-use title.
    Feature: Badge identity — slug follows the artwork title.
    """
    doc = {
        "name": "Renamed By A Reviewer",
        "image_title": "Vector Search Fundamentals",
        "text_title": "Building AI-Powered Search with MongoDB Vector Search",
    }
    assert skill_badges.desired_slug(doc) == "vector-search-fundamentals"


def test_a_badge_without_artwork_falls_back_to_its_title(fake_collection):
    """
    Intent: A badge whose artwork carries no readable title still needs a usable identity,
        and the title in use is the best name available.
    Success: The slug is derived from the badge's name when there is no artwork title.
    Feature: Badge identity — slug follows the artwork title.
    """
    assert skill_badges.desired_slug({"name": "Cluster Reliability"}) == "cluster-reliability"


def test_normalising_slugs_moves_badges_to_their_artwork_slug(fake_collection):
    """
    Intent: Slug derivation was previously a one-off script, so a rebuilt collection would
        not reproduce it. Making it a repeatable action means the rule lives in the program
        rather than in someone's shell history.
    Success: A badge whose slug disagrees with its artwork title is renamed and reported,
        and the old slug is remembered.
    Feature: Badge identity — normalising slugs.
    """
    skill_badges.upsert_badges(
        [badge("building-ai-powered-search", image_title="Vector Search Fundamentals")]
    )

    result = skill_badges.normalise_slugs()

    assert result["renamed"] == [
        {"from": "building-ai-powered-search", "to": "vector-search-fundamentals"}
    ]
    doc = fake_collection.find_one({"slug": "vector-search-fundamentals"})
    assert doc["aliases"] == ["building-ai-powered-search"]


def test_normalising_leaves_correct_slugs_alone(fake_collection):
    """
    Intent: Running the action on an already-correct collection must be a no-op, not add
        every badge's own slug to its alias list.
    Success: The badge is counted as unchanged and gains no alias.
    Feature: Badge identity — normalising slugs.
    """
    skill_badges.upsert_badges(
        [badge("vector-search-fundamentals", image_title="Vector Search Fundamentals")]
    )

    result = skill_badges.normalise_slugs()

    assert result["unchanged"] == 1
    assert result["renamed"] == []
    assert fake_collection.find_one({"slug": "vector-search-fundamentals"}).get("aliases") is None


def test_normalising_reports_a_slug_collision_instead_of_merging(fake_collection):
    """
    Intent: Two badges whose artwork titles reduce to the same slug must not silently
        collapse into one record — that would delete a real badge without review.
    Success: The collision is reported and both records survive.
    Feature: Badge identity — normalising slugs safely.
    """
    skill_badges.upsert_badges(
        [
            badge("search-fundamentals", image_title="Search Fundamentals"),
            badge("other-slug", image_title="Search Fundamentals"),
        ]
    )

    result = skill_badges.normalise_slugs()

    assert [c["slug"] for c in result["slug_conflicts"]] == ["other-slug"]
    assert fake_collection.count_documents({}) == 2


def test_normalising_skips_a_badge_with_no_usable_name(fake_collection):
    """
    Intent: A record with neither an artwork title nor a name has no slug to derive. It must
        be skipped rather than renamed to an empty slug, which would make it unreachable.
    Success: The badge keeps its slug and is not counted as renamed.
    Feature: Badge identity — normalising slugs safely.
    """
    skill_badges.upsert_badges([badge("keeps-its-slug")])
    skill_badges.collection().update_one(
        {"slug": "keeps-its-slug"}, {"$set": {"name": "", "image_title": ""}}
    )

    result = skill_badges.normalise_slugs()

    assert result["renamed"] == []
    assert fake_collection.find_one({"slug": "keeps-its-slug"}) is not None
