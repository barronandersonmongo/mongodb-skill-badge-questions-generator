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


# --- resolving a badge to its chunk set ---


def seed_chunks(*specs):
    """Store chunks directly, as a refresh would after splitting a page."""
    from app.repositories import doc_chunks

    for ordinal, (chunk_id, url, heading, text) in enumerate(specs):
        doc_chunks.replace_page_chunks(
            url,
            [
                {
                    "chunk_id": chunk_id,
                    "url": url,
                    "anchor": heading.lower().replace(" ", "-"),
                    "source": "ix-1",
                    "page_title": "Atlas Search",
                    "heading": heading,
                    "heading_path": ["Atlas Search"],
                    "heading_level": 2,
                    "ordinal": 0,
                    "text": text,
                    "embed_text": f"{heading}\n\n{text}",
                    "chars": len(text),
                    "bytes": len(text.encode("utf-8")),
                }
            ],
        )


def test_a_badge_resolves_to_the_sections_it_is_about(fake_doc_chunks, settings):
    """
    Intent: Replaces a test resolving a badge to whole pages. A page was the wrong unit
        twice over — sent whole, one 1.7 MB page cost $2.58 for three questions; capped,
        everything past the cap was unreachable. A section of a page is affordable to send
        and specific enough to retrieve, and it is what a walk now steps through.
    Success: Chunks matching the badge's topics come back with their ids and scores.
    Feature: Question generation — a badge resolves to its documentation sections.
    """
    seed_chunks(
        ("c1", "https://x/a.md", "Atlas Search indexes", "indexes " * 30),
        ("c2", "https://x/b.md", "Atlas Search aggregation", "aggregation " * 30),
    )
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=settings)
    assert {c["chunk_id"] for c in found} == {"c1", "c2"}
    assert all(c["score"] > 0 for c in found)


def test_a_section_carries_the_heading_that_explains_it(fake_doc_chunks, settings):
    """
    Intent: A section read away from its page needs its context: "Limitations" says nothing
        on its own and everything under "Atlas Vector Search > Filtering > Limitations". The
        set has to carry that, or the walk cannot tell the author what it is reading.
    Success: A resolved section reports its heading and the path above it.
    Feature: Question generation — sections carry their heading context.
    """
    seed_chunks(("c1", "https://x/a.md", "Atlas Search indexes", "indexes " * 30))
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=settings)
    assert found[0]["heading"] == "Atlas Search indexes"
    assert found[0]["heading_path"] == ["Atlas Search"]
    assert found[0]["url"] == "https://x/a.md"


def test_the_chunk_set_is_ordered_by_relevance(fake_doc_chunks, settings):
    """
    Intent: Replaces the page-ordering test. A run walks only part of the set, so it must
        walk the most relevant part — returned in discovery order, a 25-section run could
        spend itself on the weakest matches the searches happened to return last.
    Success: The set comes back best match first.
    Feature: Question generation — the chunk set is walked in relevance order.
    """
    seed_chunks(
        ("c1", "https://x/a.md", "Atlas Search indexes", "Atlas Search indexes exactly."),
        ("c2", "https://x/b.md", "Atlas Search", "indexes " + "unrelated " * 40),
    )
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=settings)
    scores = [c["score"] for c in found]
    assert scores == sorted(scores, reverse=True)


def test_a_weak_section_is_not_this_badges_material(fake_doc_chunks, settings):
    """
    Intent: Replaces the page-level floor test. Badge categories come from Credly's
        marketing tags: on the live Cluster Reliability badge the tag "Cluster IP" — a
        Kubernetes term — pulled VPC peering and IP access lists in at 0.64-0.69 while real
        matches scored 0.70-0.86. The floor keeps a tagging artifact out, and it applies to
        sections for the same reason it applied to pages.
    Success: A section scoring below the floor is excluded.
    Feature: Question generation — a relevance floor on the chunk set.
    """
    seed_chunks(
        ("c1", "https://x/a.md", "Atlas Search indexes", "Atlas Search indexes."),
        ("c2", "https://x/b.md", "Unrelated", "indexes " + "kubernetes " * 60),
    )
    strict = replace(settings, doc_page_set_score_floor=0.5)
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=strict)
    assert "c2" not in {c["chunk_id"] for c in found}


def test_reference_sections_are_not_question_material(fake_doc_chunks, settings):
    """
    Intent: Replaces the page-level exclusion. Measured 2026-08-19, 3,318 of 7,162 stored
        pages are parameter lists, CLI synopses and command references — and chunking them
        does not make them teachable, it just makes more of them.
    Success: Sections from reference, cli, api and command paths are excluded.
    Feature: Question generation — reference sections are not walked.
    """
    seed_chunks(
        ("c1", "https://x/guide/a.md", "Atlas Search indexes", "Atlas Search indexes."),
        ("c2", "https://x/reference/command/b.md", "Atlas Search indexes", "Atlas Search indexes."),
        ("c3", "https://x/cli/c.md", "Atlas Search indexes", "Atlas Search indexes."),
    )
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=settings)
    assert {c["chunk_id"] for c in found} == {"c1"}


def test_sections_already_written_from_are_not_walked_again(fake_doc_chunks, settings):
    """
    Intent: Replaces a test excluding whole pages. Excluding a page was too coarse once a
        page is several sections: one question written from a page's opening would have made
        the rest of that page unreachable forever, which is the reverse of what chunking is
        for.
    Success: An excluded section id is absent, and its page's other sections are not.
    Feature: Question generation — a walk resumes section by section.
    """
    seed_chunks(
        ("c1", "https://x/a.md", "Atlas Search indexes", "Atlas Search indexes."),
        ("c2", "https://x/b.md", "Atlas Search indexes two", "Atlas Search indexes."),
    )
    found = doc_retrieval.chunk_set_for_badge(
        BADGE, exclude_chunk_ids={"c1"}, settings=settings
    )
    assert {c["chunk_id"] for c in found} == {"c2"}


def test_a_section_found_by_two_topics_keeps_its_best_score(fake_doc_chunks, settings):
    """
    Intent: The set is walked in relevance order, so a section's position must reflect how
        relevant it is at its strongest — a section central to one topic should not be
        demoted because it also weakly matches another.
    Success: A section matching two topic queries appears once.
    Feature: Question generation — section scores are the best of their matches.
    """
    seed_chunks(
        ("c1", "https://x/a.md", "Atlas Search indexes", "Atlas Search indexes aggregation."),
    )
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=settings)
    assert len(found) == 1


def test_the_chunk_set_is_capped(fake_doc_chunks, settings):
    """
    Intent: Chunking multiplies the candidates — 3,844 pages become 18,421 sections — so the
        cap matters more than it did, not less. Uncapped, the coverage screen would claim
        thousands of sections of material that is only nominally relevant.
    Success: The set is no larger than the configured cap.
    Feature: Question generation — a bounded chunk set.
    """
    seed_chunks(*[
        (f"c{n}", f"https://x/{n}.md", "Atlas Search indexes", "Atlas Search indexes.")
        for n in range(12)
    ])
    small = replace(settings, doc_page_set_size=4)
    assert len(doc_retrieval.chunk_set_for_badge(BADGE, settings=small)) == 4


def test_an_unavailable_index_yields_no_chunk_set_rather_than_an_error(
    fake_doc_chunks, settings, monkeypatch, caplog
):
    """
    Intent: The Atlas Vector Search index lives outside this repository, so it can be
        missing, renamed or still building — and it has to be recreated against the chunk
        field for this change, which is exactly when it will be absent. The coverage screen
        and the walk both call this, and neither should break.
    Success: A failing search returns an empty set and logs a warning.
    Feature: Question generation — chunk-set resolution degrades rather than failing.
    """
    from app.repositories import doc_chunks

    def boom(*args, **kwargs):
        raise RuntimeError("index not found")

    monkeypatch.setattr(doc_chunks, "search_chunk_refs", boom)
    assert doc_retrieval.chunk_set_for_badge(BADGE, settings=settings) == []
    assert "Chunk set search failed" in caplog.text


def test_one_page_does_not_crowd_out_the_others(fake_doc_chunks, settings):
    """
    Intent: Measured on the live corpus: the 25 sections a Vector Search Fundamentals run
        walked came from six pages, because 85 of that badge's 252 sections were hard-split
        slices of one 1.7 MB page — the same code sample in a dozen languages under one
        heading. Twenty of those 25 produced no question, while a badge spread over 24 pages
        produced 72. Pure relevance order is not enough when one page scores well throughout.
    Success: The head of the set draws from several pages rather than one.
    Feature: Question generation — a badge's sections are spread across pages.
    """
    from app.repositories import doc_chunks

    # One page with many strong sections, and two others with one each.
    doc_chunks.replace_page_chunks("https://x/big.md", [
        {
            "chunk_id": f"big{n}", "url": "https://x/big.md", "source": "ix-1",
            "page_title": "Atlas Search", "heading": "Atlas Search indexes",
            "heading_path": [], "ordinal": n,
            "text": "Atlas Search indexes.", "embed_text": "Atlas Search indexes",
            "chars": 20, "bytes": 20,
        }
        for n in range(10)
    ])
    for name in ("one", "two"):
        doc_chunks.replace_page_chunks(f"https://x/{name}.md", [{
            "chunk_id": name, "url": f"https://x/{name}.md", "source": "ix-1",
            "page_title": "Atlas Search", "heading": "Atlas Search indexes",
            "heading_path": [], "ordinal": 0,
            "text": "Atlas Search indexes.", "embed_text": "Atlas Search indexes",
            "chars": 20, "bytes": 20,
        }])
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=settings)
    heads = {c["url"] for c in found[:3]}
    assert len(heads) == 3, [c["chunk_id"] for c in found[:3]]


def test_a_held_back_section_is_still_offered_later(fake_doc_chunks, settings):
    """
    Intent: The per-page limit reorders the set, it does not shrink it. A badge whose material
        is genuinely concentrated in a few pages would otherwise resolve to almost nothing, and
        report itself exhausted while sections sat unused.
    Success: Every section still appears in the set, just later.
    Feature: Question generation — spreading reorders rather than discards.
    """
    from app.repositories import doc_chunks

    doc_chunks.replace_page_chunks("https://x/big.md", [
        {
            "chunk_id": f"big{n}", "url": "https://x/big.md", "source": "ix-1",
            "page_title": "Atlas Search", "heading": "Atlas Search indexes",
            "heading_path": [], "ordinal": n,
            "text": "Atlas Search indexes.", "embed_text": "Atlas Search indexes",
            "chars": 20, "bytes": 20,
        }
        for n in range(8)
    ])
    found = doc_retrieval.chunk_set_for_badge(BADGE, settings=settings)
    assert len(found) == 8
