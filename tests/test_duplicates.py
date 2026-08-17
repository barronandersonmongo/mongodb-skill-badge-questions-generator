"""Tests for app/services/duplicates.py — duplicate detection and merging.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app.config import Settings
from app.models.skill_badge import DiscoveredBadge
from app.repositories import skill_badges
from app.services import badge_discovery, duplicates
from app.services.duplicates import DuplicateVerdict
from tests.fakes import FakeAnthropic, FakeParsedResponse


@pytest.fixture
def settings() -> Settings:
    # The fake index scores by word overlap, which is stricter than cosine over
    # real embeddings, so these tests set their own floor.
    return Settings(
        mongodb_uri="mongodb://test", gateway_key="k", duplicate_score_threshold=0.3
    )


def store(slug: str, description: str, **extra) -> None:
    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                slug=slug,
                name=extra.pop("name", slug.replace("-", " ").title()),
                description=description,
                confidence="high",
                source_urls=["https://learn.mongodb.com/skills"],
                **{k: v for k, v in extra.items() if k in DiscoveredBadge.model_fields},
            )
        ]
    )
    for field, value in extra.items():
        if field not in DiscoveredBadge.model_fields:
            skill_badges.collection().update_one({"slug": slug}, {"$set": {field: value}})


@pytest.fixture
def judge(monkeypatch):
    """Judge on description word overlap, so unrelated neighbours are not duplicates."""

    def install(verdict: DuplicateVerdict):
        def fake_judge(left, right, settings=None):
            words = lambda b: set((b.get("description") or "").lower().split())  # noqa: E731
            if len(words(left) & words(right)) >= 4:
                return verdict
            return DuplicateVerdict(
                duplicate=False, confident=True, reason="Different material."
            )

        monkeypatch.setattr(duplicates, "judge_pair", fake_judge)

    return install


def test_a_pair_the_model_confirms_is_reported(fake_collection, judge, settings):
    """
    Intent: The same badge is titled differently on Credly, on learn.mongodb.com and on
        its artwork, so duplicates cannot be found by slug or title. Searching
        descriptions by meaning must surface the pair for resolution.
    Success: One pair is reported, naming both slugs and the model's reason.
    Feature: Duplicate detection — finding duplicates by description.
    """
    store("atlas-search-fundamentals", "Covers building a search index and queries.")
    store("search-with-mongodb", "Covers creating a search index and running queries.")
    judge(DuplicateVerdict(duplicate=True, confident=True, reason="Same search material."))

    found = duplicates.find_duplicates(settings=settings)

    assert len(found) == 1
    assert {found[0]["keep"], found[0]["drop"]} == {
        "atlas-search-fundamentals",
        "search-with-mongodb",
    }
    assert found[0]["reason"] == "Same search material."


def test_a_pair_the_model_rejects_is_not_reported(fake_collection, judge, settings):
    """
    Intent: Similar descriptions are not proof of duplication — an Atlas and a
        self-managed badge on one topic read almost identically. The model's judgement,
        not vector proximity, must decide.
    Success: Nothing is reported when the model says the badges differ.
    Feature: Duplicate detection — vector search proposes, the model decides.
    """
    store("networking-security-atlas", "Covers securing networking in Atlas.")
    store("networking-security-self-managed", "Covers securing networking self-managed.")
    judge(DuplicateVerdict(duplicate=False, confident=True, reason="Atlas vs self-managed."))

    assert duplicates.find_duplicates(settings=settings) == []


def test_each_pair_is_reported_once(fake_collection, judge, settings):
    """
    Intent: Every badge is compared against its neighbours, so each pair comes up twice.
        Reporting it twice would invite a reviewer to merge an already-merged pair.
    Success: A single pair is reported for two mutually-similar badges.
    Feature: Duplicate detection — finding duplicates by description.
    """
    store("a-search", "Covers search index basics.")
    store("b-search", "Covers search index basics.")
    judge(DuplicateVerdict(duplicate=True, confident=True, reason="Same."))

    assert len(duplicates.find_duplicates(settings=settings)) == 1


def test_the_record_carrying_review_work_is_kept(fake_collection, judge, settings):
    """
    Intent: A merge deletes one record. It must never be the one a reviewer curated, or
        the merge silently discards their corrected title or links.
    Success: The badge with a locked name is chosen to keep, and the other to drop.
    Feature: Duplicate resolution — protecting curated records.
    """
    store("curated-one", "Covers search index basics.")
    store("raw-one", "Covers search index basics.")
    skill_badges.set_name("curated-one", "Curated Title")
    judge(DuplicateVerdict(duplicate=True, confident=True, reason="Same."))

    found = duplicates.find_duplicates(settings=settings)
    assert found[0]["keep"] == "curated-one"
    assert found[0]["drop"] == "raw-one"


def test_confident_duplicates_are_merged_and_the_rest_left_for_review(fake_collection, judge, settings):
    """
    Intent: The program should resolve duplicates it is sure about, but a merge destroys
        a record, so an unsure pair must wait for a person rather than being applied.
    Success: A confident pair is merged and reported as merged; an unconfident pair is
        left in place and reported for review.
    Feature: Duplicate resolution — automatic merge with human fallback.
    """
    store("keep-me", "Covers search index basics.")
    store("drop-me", "Covers search index basics.")

    judge(DuplicateVerdict(duplicate=True, confident=True, reason="Same."))
    result = duplicates.merge_confident_duplicates(settings=settings)
    assert len(result["merged"]) == 1
    assert result["needs_review"] == []
    assert skill_badges.collection().count_documents({}) == 1

    store("other-a", "Covers shard key selection.")
    store("other-b", "Covers shard key selection.")
    judge(DuplicateVerdict(duplicate=True, confident=False, reason="Not sure."))
    result = duplicates.merge_confident_duplicates(settings=settings)
    assert result["merged"] == []
    assert len(result["needs_review"]) == 1
    assert skill_badges.collection().count_documents({}) == 3


def test_badges_without_a_description_are_skipped(fake_collection, judge, settings):
    """
    Intent: Comparison is by description, so a record with none cannot be judged. It must
        be skipped rather than embedded as an empty string, which would match everything.
    Success: No pair is reported when the only other badge has no description.
    Feature: Duplicate detection — finding duplicates by description.
    """
    store("has-description", "Covers search index basics.")
    store("no-description", "")
    judge(DuplicateVerdict(duplicate=True, confident=True, reason="Same."))

    found = duplicates.find_duplicates(settings=settings)
    assert all("no-description" not in (p["keep"], p["drop"]) for p in found)


def test_a_judgement_without_structured_output_fails_loudly(monkeypatch, fake_collection, settings):
    """
    Intent: If the judgement returns nothing parseable, treating it as "not a duplicate"
        would silently stop finding duplicates. It must raise instead.
    Success: RuntimeError naming the missing structured output is raised.
    Feature: Duplicate detection — failure handling.
    """
    client = FakeAnthropic(parsed=FakeParsedResponse(None, stop_reason="max_tokens"))
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(RuntimeError, match="no structured output"):
        duplicates.judge_pair({"slug": "a"}, {"slug": "b"}, settings=settings)


def test_the_judgement_brief_rules_out_the_known_near_miss_pairs(settings):
    """
    Intent: The collection genuinely contains pairs that read almost identically but are
        different badges — Atlas versus self-managed, fundamentals versus advanced,
        authentication versus networking. Merging any of those destroys a real badge, so
        the brief must name them explicitly.
    Success: The brief rules out those pairs and requires confidence before merging.
    Feature: Duplicate detection — protecting near-miss pairs.
    """
    brief = duplicates.JUDGE_SYSTEM
    assert "self-managed" in brief
    assert "fundamentals" in brief
    assert "confident" in brief


def test_the_pair_is_judged_on_descriptions_and_artwork_titles(monkeypatch, settings):
    """
    Intent: Titles differ across sources by design, so the judgement must see the
        descriptions and both titles to decide on substance rather than wording.
    Success: The prompt carries both descriptions and both artwork titles.
    Feature: Duplicate detection — finding duplicates by description.
    """
    client = FakeAnthropic(
        parsed=DuplicateVerdict(duplicate=False, confident=True, reason="Different.")
    )
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    duplicates.judge_pair(
        {"slug": "a", "name": "A", "image_title": "Artwork A", "description": "Covers A."},
        {"slug": "b", "name": "B", "image_title": "Artwork B", "description": "Covers B."},
        settings=settings,
    )
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "Covers A." in prompt and "Covers B." in prompt
    assert "Artwork A" in prompt and "Artwork B" in prompt


def test_credential_failures_in_the_judgement_are_actionable(monkeypatch, settings):
    """
    Intent: The judgement is another Claude call, so it can be where a missing key first
        surfaces; it must say which variable to set rather than repeating the SDK's
        internal message.
    Success: RuntimeError naming ANTHROPIC_API_KEY is raised.
    Feature: Duplicate detection — missing credential diagnostics.
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
        duplicates.judge_pair({"slug": "a"}, {"slug": "b"}, settings=settings)


def test_a_neighbour_that_is_no_longer_stored_is_ignored(
    fake_collection, judge, settings, monkeypatch
):
    """
    Intent: The index can return a badge that has since been merged away or deleted.
        Judging against a record that no longer exists would propose merging a ghost.
    Success: A stale neighbour is skipped and nothing is reported.
    Feature: Duplicate detection — safe application of matches.
    """
    store("still-here", "Covers search index basics.")
    judge(DuplicateVerdict(duplicate=True, confident=True, reason="Same."))
    monkeypatch.setattr(
        skill_badges,
        "similar_by_description",
        lambda *a, **k: [{"slug": "already-merged-away", "score": 0.99}],
    )

    assert duplicates.find_duplicates(settings=settings) == []


def test_the_survivor_is_chosen_consistently_whichever_order_the_pair_arrives(fake_collection, judge, settings):
    """
    Intent: Every pair is seen from both sides, so survivor choice must not depend on
        which badge happened to be compared first — otherwise the same scan could delete
        either record depending on iteration order.
    Success: The badge with canonical identity is kept regardless of argument order.
    Feature: Duplicate resolution — protecting curated records.
    """
    canonical = {"slug": "canonical", "credly_url": "https://credly/x", "image_title": "T"}
    plain = {"slug": "plain"}

    assert duplicates._choose_survivor(canonical, plain)[0]["slug"] == "canonical"
    assert duplicates._choose_survivor(plain, canonical)[0]["slug"] == "canonical"


def test_other_judgement_failures_propagate(monkeypatch, settings):
    """
    Intent: A real API error during judging must surface rather than being relabelled as a
        credential problem, which would send the operator after the wrong cause.
    Success: The original exception propagates.
    Feature: Duplicate detection — failure handling.
    """
    class Raising:
        def parse(self, **kwargs):
            raise ConnectionError("connection reset by peer")

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(ConnectionError, match="connection reset"):
        duplicates.judge_pair({"slug": "a"}, {"slug": "b"}, settings=settings)


def test_distant_neighbours_are_not_sent_to_the_model(fake_collection, settings, monkeypatch):
    """
    Intent: Vector search always returns its top matches, however unrelated. Judging all
        of them would spend a model call per pair across the whole collection; only
        neighbours close enough to be plausible duplicates are worth asking about.
    Success: A pair below the similarity threshold is skipped without a judgement call.
    Feature: Duplicate detection — accelerating the scan with a similarity floor.
    """
    store("search-badge", "Covers search index basics.")
    store("shard-badge", "Covers shard key selection.")

    judged = []

    def counting_judge(left, right, settings=None):
        judged.append((left["slug"], right["slug"]))
        return DuplicateVerdict(duplicate=False, confident=True, reason="n/a")

    monkeypatch.setattr(duplicates, "judge_pair", counting_judge)
    duplicates.find_duplicates(settings=settings)

    assert judged == []


def test_the_search_queries_the_atlas_index_with_description_text(
    fake_collection, judge, settings, monkeypatch
):
    """
    Intent: The Atlas index is configured with autoEmbed, so Atlas embeds both the stored
        descriptions and the query. Sending a vector instead of text would be rejected,
        and this program must store no vectors of its own.
    Success: The search is called with the badge's description text and the configured
        index name.
    Feature: Duplicate detection — querying the Atlas auto-embedding index.
    """
    store("atlas-search", "Covers search index basics.")
    calls = []

    def record(description, index_name, **kwargs):
        calls.append((description, index_name))
        return []

    monkeypatch.setattr(skill_badges, "similar_by_description", record)
    duplicates.find_duplicates(settings=settings)

    assert calls == [("Covers search index basics.", settings.vector_index_name)]


def test_badges_citing_the_same_canonical_url_are_duplicates_without_a_model_call(
    fake_collection, settings, monkeypatch
):
    """
    Intent: Two records pointing at the same Credly badge page are the same badge by
        definition. That is proof, not a hint, so it must be settled deterministically —
        spending a model call on it risks the model disagreeing with a certainty.
    Success: The pair is reported as a confident duplicate, citing the shared URL, with
        no judgement call made.
    Feature: Duplicate detection — identical canonical URLs.
    """
    store("badge-one", "Covers search.", credly_url="https://www.credly.com/org/mongodb/badge/x")
    store("badge-two", "Totally different wording.", credly_url="https://www.credly.com/org/mongodb/badge/x")

    def fail(*args, **kwargs):
        raise AssertionError("a shared URL needs no judgement")

    monkeypatch.setattr(duplicates, "judge_pair", fail)
    monkeypatch.setattr(skill_badges, "similar_by_description", lambda *a, **k: [])

    found = duplicates.find_duplicates(settings=settings)
    assert len(found) == 1
    assert {found[0]["keep"], found[0]["drop"]} == {"badge-one", "badge-two"}
    assert found[0]["confident"] is True
    assert "same Credly page" in found[0]["reason"]


def test_a_shared_learn_mongodb_page_also_settles_identity(fake_collection, settings, monkeypatch):
    """
    Intent: A badge is earned from one course page, so two records citing the same
        learn.mongodb.com page are the same badge — the same reasoning as the Credly page.
    Success: The pair is reported, citing the shared learn.mongodb.com page.
    Feature: Duplicate detection — identical canonical URLs.
    """
    store("one", "Covers search.", mongodb_url="https://learn.mongodb.com/courses/search")
    store("two", "Covers searching.", mongodb_url="https://learn.mongodb.com/courses/search")
    monkeypatch.setattr(skill_badges, "similar_by_description", lambda *a, **k: [])

    found = duplicates.find_duplicates(settings=settings)
    assert len(found) == 1
    assert "learn.mongodb.com page" in found[0]["reason"]


def test_url_matching_ignores_trailing_slashes_and_case(fake_collection, settings, monkeypatch):
    """
    Intent: The same page is cited inconsistently across sources, so a trailing slash or
        different capitalisation must not hide a certain duplicate.
    Success: URLs differing only by case or a trailing slash are treated as the same page.
    Feature: Duplicate detection — identical canonical URLs.
    """
    store("one", "Covers search.", credly_url="https://www.credly.com/org/MongoDB/badge/X/")
    store("two", "Covers search.", credly_url="https://www.credly.com/org/mongodb/badge/x")
    monkeypatch.setattr(skill_badges, "similar_by_description", lambda *a, **k: [])

    assert len(duplicates.find_duplicates(settings=settings)) == 1


def test_badges_with_different_urls_are_left_to_the_model(fake_collection, judge, settings):
    """
    Intent: Different URLs prove nothing either way — the same badge is often cited by
        different pages — so those pairs must still go through description comparison.
    Success: A pair with different URLs is judged rather than reported as a URL match.
    Feature: Duplicate detection — identical canonical URLs.
    """
    store("one", "Covers search index basics.", credly_url="https://www.credly.com/a")
    store("two", "Covers search index basics.", credly_url="https://www.credly.com/b")
    judge(DuplicateVerdict(duplicate=True, confident=False, reason="Judged on description."))

    found = duplicates.find_duplicates(settings=settings)
    assert found[0]["reason"] == "Judged on description."
    assert found[0]["confident"] is False


def test_a_url_matched_pair_is_not_judged_again_by_description(
    fake_collection, settings, monkeypatch
):
    """
    Intent: A pair settled by URL must not also be reported from the vector pass, or the
        reviewer sees the same duplicate twice and may try to merge an already-merged pair.
    Success: One entry is reported even though the pair is also a description neighbour.
    Feature: Duplicate detection — reporting each pair once.
    """
    store("one", "Covers search index basics.", credly_url="https://www.credly.com/x")
    store("two", "Covers search index basics.", credly_url="https://www.credly.com/x")
    monkeypatch.setattr(
        duplicates,
        "judge_pair",
        lambda a, b, settings=None: DuplicateVerdict(
            duplicate=True, confident=True, reason="Also similar."
        ),
    )

    found = duplicates.find_duplicates(settings=settings)
    assert len(found) == 1
    assert "same Credly page" in found[0]["reason"]


def test_the_judgement_prompt_includes_both_canonical_urls(monkeypatch, settings):
    """
    Intent: Even when URLs are not identical, they carry signal — one may be a course page
        the other references — so the model must see both.
    Success: Both URLs appear in the prompt.
    Feature: Duplicate detection — judging with canonical URLs.
    """
    client = FakeAnthropic(
        parsed=DuplicateVerdict(duplicate=False, confident=True, reason="Different.")
    )
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    duplicates.judge_pair(
        {"slug": "a", "credly_url": "https://credly/a", "mongodb_url": "https://learn/a"},
        {"slug": "b", "credly_url": "https://credly/b", "mongodb_url": "https://learn/b"},
        settings=settings,
    )
    prompt = client.messages.parse_calls[0]["messages"][0]["content"]
    assert "https://credly/a" in prompt and "https://learn/b" in prompt


def test_a_pair_sharing_both_canonical_urls_is_reported_once(
    fake_collection, settings, monkeypatch
):
    """
    Intent: Two records can share both their Credly page and their learn.mongodb.com page,
        which would otherwise report the same duplicate twice — once per URL field.
    Success: A single entry is reported for the pair.
    Feature: Duplicate detection — reporting each pair once.
    """
    store(
        "one",
        "Covers search.",
        credly_url="https://www.credly.com/x",
        mongodb_url="https://learn.mongodb.com/courses/x",
    )
    store(
        "two",
        "Covers search.",
        credly_url="https://www.credly.com/x",
        mongodb_url="https://learn.mongodb.com/courses/x",
    )
    monkeypatch.setattr(skill_badges, "similar_by_description", lambda *a, **k: [])

    assert len(duplicates.find_duplicates(settings=settings)) == 1
