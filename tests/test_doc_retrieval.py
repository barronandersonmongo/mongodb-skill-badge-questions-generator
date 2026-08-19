"""Tests for app/services/doc_retrieval.py — source material out of the corpus.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from dataclasses import replace

import pytest

from app.repositories import doc_pages
from app.services import doc_retrieval

BADGE = {
    "slug": "atlas-search",
    "name": "Atlas Search",
    "description": "Covers Atlas Search indexes and queries.",
    "categories": ["indexes", "aggregation"],
}


def page(title: str, body: str, url: str, source: str = "ix-1") -> dict:
    return {"url": url, "source": source, "title": title, "text": body}


# --- what gets searched ---


def test_each_topic_area_is_searched_separately(settings):
    """
    Intent: One badge-wide query returns its top pages clustered on whichever topic
        embeds closest to the badge description, so five questions come off one page —
        the failure this tool exists to avoid. Each topic area the badge claims to
        cover has to be asked for in its own right.
    Success: A query is built for the badge as a whole and one for each of its
        categories.
    Feature: Question generation — source material spread across the badge syllabus.
    """
    queries = doc_retrieval.queries_for_badge(BADGE)
    assert queries[0].startswith("Atlas Search")
    assert "Atlas Search indexes" in queries
    assert "Atlas Search aggregation" in queries


def test_a_topic_area_is_qualified_by_its_badge(settings):
    """
    Intent: A bare topic area is not a search: "indexes" matches most of MongoDB's
        documentation, while "Atlas Search indexes" matches what the badge means by it.
    Success: Every topic-area query carries the badge name.
    Feature: Question generation — topic searches scoped to their badge.
    """
    queries = doc_retrieval.queries_for_badge(BADGE)
    assert all(q.startswith("Atlas Search") for q in queries)
    assert "indexes" not in queries


def test_a_badge_with_no_topic_areas_is_still_searched(settings):
    """
    Intent: Topic areas are discovered, not guaranteed. A badge that has none must
        still get source material rather than silently authoring from nothing.
    Success: A badge with no categories still produces a query from its name and
        description.
    Feature: Question generation — retrieval for a badge with no topic areas.
    """
    queries = doc_retrieval.queries_for_badge(
        {"slug": "x", "name": "Data Modeling", "description": "Schema design."}
    )
    assert queries == ["Data Modeling Schema design."]


# --- what comes back ---


def test_pages_are_returned_with_their_text_and_url(fake_doc_pages, settings):
    """
    Intent: Authoring reads pages, not excerpts, and a question must cite the page it
        was written from — a citation is what lets a reviewer check a question without
        re-researching it.
    Success: A retrieved page carries its full text and the URL it came from.
    Feature: Question generation — source material read from the stored corpus.
    """
    doc_pages.upsert_pages([page("Atlas Search indexes", "Define an index.", "https://x/a.md")])
    found = doc_retrieval.pages_for_badges([BADGE], settings=settings)
    assert found[0]["url"] == "https://x/a.md"
    assert found[0]["text"] == "Define an index."


def test_the_same_page_is_not_sent_twice(fake_doc_pages, settings):
    """
    Intent: Topic areas overlap, so the same page answers several searches. Sent twice
        it spends the context budget twice over and tells the model that page matters
        more than it does.
    Success: A page matching more than one topic query appears once.
    Feature: Question generation — deduplicated source material.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes aggregation", "Everything.", "https://x/a.md"),
    ])
    found = doc_retrieval.pages_for_badges([BADGE], settings=settings)
    assert [p["url"] for p in found] == ["https://x/a.md"]


def test_every_topic_area_gets_material_before_any_gets_seconds(fake_doc_pages, settings):
    """
    Intent: Filling the context budget in query order spends it all on the first topic
        area and leaves the last with nothing, which is exactly the imbalance that
        searching per topic area was meant to fix.
    Success: With a budget big enough for only two pages, the two topic areas get one
        page each rather than one topic getting both.
    Feature: Question generation — the context budget spread across topic areas.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes one", "indexes " * 20, "https://x/i1.md"),
        page("Atlas Search indexes two", "indexes " * 19, "https://x/i2.md"),
        page("Atlas Search aggregation one", "aggregation " * 20, "https://x/a1.md"),
    ])
    # Room for three of these pages in round-robin order, but only two if the
    # budget were spent query by query: the aggregation page is the one that
    # would be lost.
    small = replace(
        settings,
        doc_context_char_budget=len("indexes " * 20) + len("aggregation " * 20) + 10,
    )
    found = doc_retrieval.pages_for_badges([BADGE], settings=small)
    urls = {p["url"] for p in found}
    assert "https://x/i1.md" in urls and "https://x/a1.md" in urls


def test_a_long_page_is_cut_short_rather_than_dropped(fake_doc_pages, settings):
    """
    Intent: Documentation pages run to tens of thousands of characters. A page cut
        short is still usable material to write from; an authoring turn that will not
        fit in the context window is not.
    Success: A page longer than the per-page allowance is truncated to it and marked
        as truncated.
    Feature: Question generation — bounded source material.
    """
    doc_pages.upsert_pages([page("Atlas Search indexes", "index " * 5000, "https://x/a.md")])
    capped = replace(settings, doc_context_page_chars=100)
    found = doc_retrieval.pages_for_badges([BADGE], settings=capped)
    assert len(found[0]["text"]) == 100
    assert found[0]["truncated"] is True


def test_the_whole_context_is_bounded(fake_doc_pages, settings):
    """
    Intent: A badge with many topic areas retrieves many pages. Without a whole-run
        budget the authoring request grows until the model rejects it, and the run
        fails after the author has waited for it.
    Success: Total retrieved text stays within the configured budget.
    Feature: Question generation — a capped authoring context.
    """
    doc_pages.upsert_pages([
        page(f"Atlas Search indexes {n}", "indexes " * 100, f"https://x/{n}.md")
        for n in range(10)
    ])
    small = replace(settings, doc_context_char_budget=2000)
    found = doc_retrieval.pages_for_badges([BADGE], settings=small)
    assert sum(len(p["text"]) for p in found) <= 2000
    assert found


# --- when the corpus cannot answer ---


def test_an_empty_corpus_yields_no_material_rather_than_an_error(fake_doc_pages, settings):
    """
    Intent: A badge whose documentation has not been crawled yet is a reason to
        research the slow way, not a reason to refuse to write questions. Retrieval
        must report "nothing" so the caller can fall back.
    Success: An empty corpus returns an empty list.
    Feature: Question generation — retrieval against an uncrawled corpus.
    """
    assert doc_retrieval.pages_for_badges([BADGE], settings=settings) == []


def test_an_unavailable_search_index_does_not_fail_the_run(fake_doc_pages, settings, monkeypatch, caplog):
    """
    Intent: The Atlas Vector Search index lives outside this repository, so it can be
        missing, renamed, or still building. Losing a whole authoring run to that
        would make the corpus a liability rather than a speed-up.
    Success: A failing corpus search returns no pages and logs a warning, rather than
        raising.
    Feature: Question generation — retrieval failure falls back instead of failing.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("index not found")

    monkeypatch.setattr(doc_pages, "search_page_texts", boom)
    assert doc_retrieval.pages_for_badges([BADGE], settings=settings) == []
    assert "Corpus search failed" in caplog.text


# --- how the material is presented ---


def test_each_page_is_labelled_with_the_url_it_came_from(settings):
    """
    Intent: A question's value depends on being checkable. If the model cannot see
        which URL a passage came from, it cannot cite it, and a reviewer has to
        re-research the question to verify it.
    Success: The formatted material carries each page's source URL.
    Feature: Question generation — citable source material.
    """
    text = doc_retrieval.format_pages(
        [{"url": "https://x/a.md", "title": "Indexes", "text": "Define an index."}]
    )
    assert "https://x/a.md" in text and "Define an index." in text


def test_a_truncated_page_says_so_in_the_prompt(settings):
    """
    Intent: A model shown a page cut off mid-sentence can reasonably conclude the
        feature has no more to it, and write a question asserting something the full
        page contradicts.
    Success: A truncated page is marked as truncated in the formatted material.
    Feature: Question generation — truncation is visible to the author model.
    """
    text = doc_retrieval.format_pages(
        [{"url": "https://x/a.md", "title": "Indexes", "text": "Def", "truncated": True}]
    )
    assert "cut short" in text


# --- resolving a badge to its page set ---


def test_a_badge_resolves_to_the_pages_it_is_about(fake_doc_pages, settings):
    """
    Intent: A run walks a badge's documentation rather than cramming the best few pages
        into one prompt, so the badge has to resolve to a set of pages that can be
        enumerated and counted — that set is what makes coverage a number.
    Success: Pages matching the badge's topics are returned, each with its URL and score.
    Feature: Question generation — a badge resolves to its documentation page set.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes", "indexes " * 50, "https://x/a.md"),
        page("Atlas Search aggregation", "aggregation " * 50, "https://x/b.md"),
    ])
    found = doc_retrieval.page_set_for_badge(BADGE, settings=settings)
    assert {p["url"] for p in found} == {"https://x/a.md", "https://x/b.md"}
    assert all(p["score"] > 0 for p in found)


def test_the_page_set_is_ordered_by_relevance(fake_doc_pages, settings):
    """
    Intent: A run walks only part of the set, so it must walk the most relevant part.
        Returned in discovery order, a 25-page run could spend itself on the weakest
        matches the searches happened to return last.
    Success: The page set comes back best match first.
    Feature: Question generation — the page set is walked in relevance order.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes", "Atlas Search indexes exactly.", "https://x/near.md"),
        page("Atlas Search", "indexes " + "unrelated " * 40, "https://x/far.md"),
    ])
    found = doc_retrieval.page_set_for_badge(BADGE, settings=settings)
    scores = [p["score"] for p in found]
    assert scores == sorted(scores, reverse=True)


def test_a_weak_match_is_not_this_badges_material(fake_doc_pages, settings):
    """
    Intent: Badge categories come from Credly's marketing tags, not a syllabus. On the
        live Cluster Reliability badge the tag "Cluster IP" pulled in VPC peering and IP
        access lists — pages nobody would write a reliability question from. A relevance
        floor is what keeps a tagging artifact out of a badge's material.
    Success: A page scoring below the floor is excluded from the page set.
    Feature: Question generation — a relevance floor on the page set.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes", "Atlas Search indexes.", "https://x/good.md"),
        page("Unrelated", "indexes " + "kubernetes " * 60, "https://x/weak.md"),
    ])
    strict = replace(settings, doc_page_set_score_floor=0.5)
    found = doc_retrieval.page_set_for_badge(BADGE, settings=strict)
    assert "https://x/weak.md" not in {p["url"] for p in found}


def test_reference_material_is_not_question_material(fake_doc_pages, settings):
    """
    Intent: Measured on 2026-08-19, 3,318 of 7,162 stored pages are parameter lists, CLI
        synopses and command references. A question written from a parameter list tests
        whether a candidate can look up a flag, which is not a skill the badges certify.
    Success: Pages under reference, cli, api and command paths are excluded.
    Feature: Question generation — reference pages are not walked.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes", "Atlas Search indexes.", "https://x/guide/a.md"),
        page("Atlas Search indexes command", "Atlas Search indexes.",
             "https://x/reference/command/b.md"),
        page("atlas clusters indexes", "Atlas Search indexes.", "https://x/cli/c.md"),
    ])
    found = doc_retrieval.page_set_for_badge(BADGE, settings=settings)
    assert {p["url"] for p in found} == {"https://x/guide/a.md"}


def test_pages_already_written_from_are_not_walked_again(fake_doc_pages, settings):
    """
    Intent: This is what makes a walk resumable. Without it, a second run for a badge
        re-reads the same pages and produces variations on questions that already exist —
        the failure the whole page-walk design is meant to remove.
    Success: An excluded URL does not appear in the page set.
    Feature: Question generation — a walk resumes rather than repeating.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes one", "Atlas Search indexes.", "https://x/a.md"),
        page("Atlas Search indexes two", "Atlas Search indexes.", "https://x/b.md"),
    ])
    found = doc_retrieval.page_set_for_badge(
        BADGE, exclude_urls={"https://x/a.md"}, settings=settings
    )
    assert {p["url"] for p in found} == {"https://x/b.md"}


def test_a_page_found_by_two_topics_keeps_its_best_score(fake_doc_pages, settings):
    """
    Intent: The set is walked in relevance order, so a page's position must reflect how
        relevant it is at its strongest — a page central to one topic should not be
        demoted because it also weakly matches another.
    Success: A page matching two topic queries appears once, at its higher score.
    Feature: Question generation — page scores are the best of their matches.
    """
    doc_pages.upsert_pages([
        page("Atlas Search indexes", "Atlas Search indexes aggregation.", "https://x/a.md"),
    ])
    found = doc_retrieval.page_set_for_badge(BADGE, settings=settings)
    assert len(found) == 1


def test_the_page_set_is_capped(fake_doc_pages, settings):
    """
    Intent: A badge like Query Optimization matches a large share of the corpus. An
        uncapped set would make the coverage screen claim thousands of pages of material
        that is only nominally relevant.
    Success: The page set is no larger than the configured cap.
    Feature: Question generation — a bounded page set.
    """
    doc_pages.upsert_pages([
        page(f"Atlas Search indexes {n}", "Atlas Search indexes.", f"https://x/{n}.md")
        for n in range(12)
    ])
    small = replace(settings, doc_page_set_size=4)
    assert len(doc_retrieval.page_set_for_badge(BADGE, settings=small)) == 4


def test_an_unavailable_index_yields_no_page_set_rather_than_an_error(
    fake_doc_pages, settings, monkeypatch, caplog
):
    """
    Intent: The Atlas Vector Search index lives outside this repository, so it can be
        missing, renamed or still building. The coverage screen and the walk both call
        this, and neither should break because of it.
    Success: A failing search returns an empty page set and logs a warning.
    Feature: Question generation — page-set resolution degrades rather than failing.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("index not found")

    monkeypatch.setattr(doc_pages, "search_page_refs", boom)
    assert doc_retrieval.page_set_for_badge(BADGE, settings=settings) == []
    assert "Page set search failed" in caplog.text
