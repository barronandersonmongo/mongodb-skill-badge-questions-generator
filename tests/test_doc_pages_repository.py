"""Tests for app/repositories/doc_pages.py — the stored documentation corpus.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from app.repositories import doc_pages


def page(url: str = "https://www.mongodb.com/docs/a.md", text: str = "# A\nbody", **kw) -> dict:
    return {"url": url, "source": "https://www.mongodb.com/docs/llms.txt",
            "title": "A", "text": text, **kw}


def test_a_fetched_page_is_stored_with_its_text(fake_doc_pages):
    """
    Intent: The whole point of the corpus is that authoring can read source material
        from the database instead of fetching the web mid-run. A record without the
        page text would be an index, not a corpus.
    Success: The stored document carries the page text, its source and its title.
    Feature: Documentation corpus — pages are stored with their content.
    """
    doc_pages.upsert_pages([page()])
    stored = fake_doc_pages.docs[0]
    assert stored["text"] == "# A\nbody"
    assert stored["title"] == "A"
    assert stored["source"].endswith("llms.txt")


def test_a_page_is_keyed_on_its_url(fake_doc_pages):
    """
    Intent: The published index identifies pages by URL, and a question's citation
        should point at one. Any other key would let the same page be stored twice
        under different identities.
    Success: Re-fetching the same URL updates one document rather than adding another.
    Feature: Documentation corpus — a page is its URL.
    """
    doc_pages.upsert_pages([page(text="first")])
    doc_pages.upsert_pages([page(text="second")])
    assert len(fake_doc_pages.docs) == 1
    assert fake_doc_pages.docs[0]["text"] == "second"


def test_an_unchanged_page_is_reported_as_unchanged_and_not_rewritten(fake_doc_pages):
    """
    Intent: A refresh of 10,000 pages that rewrote every document would make "updated"
        meaningless and cost a full collection write each time. Recognising unchanged
        content is what makes a re-run cheap and its report informative.
    Success: Re-storing identical text reports unchanged, and the content hash is
        unchanged.
    Feature: Documentation corpus — refreshes are incremental.
    """
    doc_pages.upsert_pages([page()])
    before = fake_doc_pages.docs[0]["content_hash"]
    result = doc_pages.upsert_pages([page()])
    assert result == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert fake_doc_pages.docs[0]["content_hash"] == before


def test_a_changed_page_is_reported_as_updated(fake_doc_pages):
    """
    Intent: Documentation changes, and a stale corpus produces questions about behaviour
        that no longer holds. The refresh must notice and say so.
    Success: Different text for the same URL reports updated and replaces the stored text.
    Feature: Documentation corpus — changed pages are refreshed.
    """
    doc_pages.upsert_pages([page(text="old")])
    result = doc_pages.upsert_pages([page(text="new")])
    assert result == {"inserted": 0, "updated": 1, "unchanged": 0}
    assert fake_doc_pages.docs[0]["text"] == "new"


def test_a_new_page_is_reported_as_inserted(fake_doc_pages):
    """
    Intent: The counts on the admin screen are how an operator knows a refresh did
        anything. A first crawl must report its pages as new rather than unchanged.
    Success: Storing a previously unseen URL reports one insert.
    Feature: Documentation corpus — new pages are reported.
    """
    assert doc_pages.upsert_pages([page()]) == {"inserted": 1, "updated": 0, "unchanged": 0}


def test_an_unchanged_page_still_records_that_it_was_checked(fake_doc_pages):
    """
    Intent: "Unchanged" and "not looked at since last month" are different states. Without
        recording the check, a page nobody has verified for weeks looks identical to one
        confirmed current a minute ago.
    Success: A refetch of unchanged content advances fetched_at.
    Feature: Documentation corpus — freshness is tracked separately from change.
    """
    doc_pages.upsert_pages([page()])
    first = fake_doc_pages.docs[0]["fetched_at"]
    fake_doc_pages.docs[0]["fetched_at"] = first.replace(year=2020)
    doc_pages.upsert_pages([page()])
    assert fake_doc_pages.docs[0]["fetched_at"] > first.replace(year=2020)


def test_when_a_page_was_first_seen_is_not_overwritten(fake_doc_pages):
    """
    Intent: first_seen_at records when this corpus learned of a page. Overwriting it on
        every refresh would make every page look newly discovered, losing the only signal
        of what the upstream docs recently added.
    Success: first_seen_at survives a later refresh that changes the text.
    Feature: Documentation corpus — discovery time is preserved.
    """
    doc_pages.upsert_pages([page(text="old")])
    first_seen = fake_doc_pages.docs[0]["first_seen_at"]
    doc_pages.upsert_pages([page(text="new")])
    assert fake_doc_pages.docs[0]["first_seen_at"] == first_seen


def test_storing_nothing_writes_nothing(fake_doc_pages):
    """
    Intent: A source whose pages all failed to fetch yields an empty batch. That must be
        an empty result rather than a write or a raise, so one bad source does not fail a
        crawl.
    Success: An empty list reports zeros and stores nothing.
    Feature: Documentation corpus — an empty batch is not an error.
    """
    assert doc_pages.upsert_pages([]) == {"inserted": 0, "updated": 0, "unchanged": 0}
    assert fake_doc_pages.docs == []


def test_listings_omit_the_page_text(fake_doc_pages):
    """
    Intent: The corpus is around 80 MB. A listing that carried page text would pull all of
        it into a screen render.
    Success: Listed pages have no text field, and no Mongo _id.
    Feature: Documentation corpus — listings are lightweight.
    """
    doc_pages.upsert_pages([page()])
    listed = doc_pages.list_pages()
    assert listed and "text" not in listed[0]
    assert "_id" not in listed[0]


def test_a_single_page_can_be_read_with_its_text(fake_doc_pages):
    """
    Intent: Authoring needs the actual text, and a reviewer checking what was captured
        needs to see it. Listings deliberately omit it, so there must be a way to get it.
    Success: get_page returns the stored text for a URL.
    Feature: Documentation corpus — a page's text is retrievable.
    """
    doc_pages.upsert_pages([page()])
    assert doc_pages.get_page(page()["url"])["text"] == "# A\nbody"


def test_pages_can_be_listed_for_one_source(fake_doc_pages):
    """
    Intent: Most of the corpus is driver and CLI reference a badge quiz never draws on.
        Working with one source at a time is the normal case, so filtering by it must work.
    Success: Filtering by source returns only that source's pages.
    Feature: Documentation corpus — per-source listing.
    """
    doc_pages.upsert_pages([
        page(url="https://www.mongodb.com/docs/a.md", source="ix-1"),
        page(url="https://www.mongodb.com/docs/b.md", source="ix-2"),
    ])
    assert [p["url"] for p in doc_pages.list_pages("ix-2")] == ["https://www.mongodb.com/docs/b.md"]


def test_pages_an_earlier_refresh_stored_are_swept_away(fake_doc_pages):
    """
    Intent: A refresh replaces the corpus rather than adding to it. A page withdrawn
        upstream, or moved to a new URL, must disappear — otherwise the corpus keeps
        serving documentation that no longer exists, and a question could be written from
        it. Replaces a per-source delete, which is no longer how the corpus is managed.
    Success: Pages not stamped with the current run are deleted; the current run's survive.
    Feature: Documentation corpus — a refresh replaces what is stored.
    """
    doc_pages.upsert_pages([page(url="https://www.mongodb.com/docs/old.md")], "run-1")
    doc_pages.upsert_pages([page(url="https://www.mongodb.com/docs/new.md")], "run-2")
    assert doc_pages.delete_not_in_run("run-2") == 1
    assert [d["url"] for d in fake_doc_pages.docs] == ["https://www.mongodb.com/docs/new.md"]


def test_a_page_seen_again_is_kept_by_the_sweep(fake_doc_pages):
    """
    Intent: Most pages are unchanged between refreshes. If an unchanged page were not
        re-stamped with the current run, the sweep would delete the entire corpus every
        time and re-fetch it from scratch.
    Success: A page whose content did not change is stamped with the new run and survives.
    Feature: Documentation corpus — unchanged pages survive a refresh.
    """
    doc_pages.upsert_pages([page()], "run-1")
    result = doc_pages.upsert_pages([page()], "run-2")
    assert result["unchanged"] == 1
    assert doc_pages.delete_not_in_run("run-2") == 0
    assert len(fake_doc_pages.docs) == 1


def test_the_corpus_can_be_emptied(fake_doc_pages):
    """
    Intent: There has to be a way to discard the whole corpus — a crawl that captured the
        wrong thing, or a change of scope — without editing the collection by hand.
    Success: delete_all removes every page and reports the count.
    Feature: Documentation corpus — the corpus can be emptied.
    """
    doc_pages.upsert_pages([
        page(url="https://www.mongodb.com/docs/a.md"),
        page(url="https://www.mongodb.com/docs/b.md"),
    ])
    assert doc_pages.delete_all() == 2
    assert fake_doc_pages.docs == []


def test_the_fields_the_screen_and_crawl_query_are_indexed(fake_doc_pages):
    """
    Intent: Every refresh looks pages up by URL, and the screen groups by source. Unindexed
        those are collection scans over ~10,000 documents on every crawl batch — and a
        duplicate URL would break page identity.
    Success: Indexes exist for url (unique), source and fetched_at.
    Feature: Documentation corpus — queryable by url and source.
    """
    doc_pages.ensure_indexes()
    by_name = {index["name"]: index for index in fake_doc_pages.indexes}
    assert {"url_unique", "source", "fetched_at"} <= by_name.keys()
    assert by_name["url_unique"]["unique"] is True


def test_the_repository_targets_the_configured_collection(monkeypatch):
    """
    Intent: The corpus must land in its own collection in the configured database. Writing
        ~10,000 documentation pages into the questions or badge collection would bury the
        data those screens read, and nothing else in the program would notice.
    Success: collection() resolves to the configured database and doc_pages collection.
    Feature: Documentation corpus — the configured storage target.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    resolved = doc_pages.collection()
    assert resolved.name == "doc_pages"
    assert resolved.database.name == "skill-badge-questions"


def test_pages_can_be_filtered_by_title_or_url(fake_doc_pages):
    """
    Intent: One source runs to nearly a thousand pages, so reaching a particular page by
        scrolling is impractical. Matching on the title and the URL both matters: a reader
        may remember either.
    Success: A filter matches on title and on URL, case-insensitively, and excludes the rest.
    Feature: Documentation corpus — find a page within a source.
    """
    doc_pages.upsert_pages([
        {"url": "https://x/aggregation.md", "source": "ix", "title": "Pipelines", "text": "a"},
        {"url": "https://x/indexes.md", "source": "ix", "title": "Index Design", "text": "b"},
    ])
    assert [p["url"] for p in doc_pages.list_pages("ix", contains="AGGREGATION")] == [
        "https://x/aggregation.md"
    ]
    assert [p["title"] for p in doc_pages.list_pages("ix", contains="index design")] == [
        "Index Design"
    ]
    assert doc_pages.list_pages("ix", contains="nothing here") == []


def test_a_sources_pages_can_be_counted_without_listing_them(fake_doc_pages):
    """
    Intent: The listing is capped, so "showing 500 of 940" needs a real total. Counting by
        listing everything would defeat the cap it exists to describe.
    Success: count_pages reports the total for a source, independent of any list limit.
    Feature: Documentation corpus — page totals per source.
    """
    doc_pages.upsert_pages([
        {"url": "https://x/a.md", "source": "ix-1", "title": "A", "text": "a"},
        {"url": "https://x/b.md", "source": "ix-1", "title": "B", "text": "b"},
        {"url": "https://x/c.md", "source": "ix-2", "title": "C", "text": "c"},
    ])
    assert doc_pages.count_pages("ix-1") == 2
    assert doc_pages.count_pages() == 3


def test_the_page_listing_is_capped(fake_doc_pages):
    """
    Intent: Without a cap, opening the Atlas CLI source would render nearly a thousand rows
        and pull their metadata into one response.
    Success: The limit bounds how many pages are returned.
    Feature: Documentation corpus — bounded page listing.
    """
    doc_pages.upsert_pages([
        {"url": f"https://x/p{i}.md", "source": "ix", "title": f"P{i}", "text": "t"}
        for i in range(10)
    ])
    assert len(doc_pages.list_pages("ix", limit=4)) == 4


# --- searching the whole corpus ---


def content(title: str, body: str, url: str, source: str = "ix-1") -> dict:
    return {"url": url, "source": source, "title": title, "text": body}


def test_a_page_is_found_by_words_in_its_text(fake_doc_pages):
    """
    Intent: Which of the 74 sources holds a topic is not something an author knows — the
        C# driver's real documentation is not under the drivers index — so searching the
        whole corpus by keyword is the only practical way to find source material.
    Success: A page is returned for a term that appears only in its body.
    Feature: Documentation search — find a page by its content.
    """
    doc_pages.upsert_pages([
        content("Pipelines", "The $lookup stage joins another collection.", "https://x/a.md"),
        content("Indexes", "A compound index covers several fields.", "https://x/b.md"),
    ])
    found = doc_pages.search_pages("lookup")
    assert [p["url"] for p in found] == ["https://x/a.md"]


def test_a_title_match_outranks_a_passing_mention(fake_doc_pages):
    """
    Intent: A page titled for the topic is nearly always the one being looked for, while a
        page that merely mentions the word once is nearly never. Unranked, the useful
        result sits below the noise.
    Success: The page whose title matches is returned first.
    Feature: Documentation search — ranked results.
    """
    doc_pages.upsert_pages([
        content("Release Notes", "Fixed an aggregation bug.", "https://x/notes.md"),
        content("Aggregation", "Aggregation combines documents.", "https://x/agg.md"),
    ])
    found = doc_pages.search_pages("aggregation")
    assert found[0]["url"] == "https://x/agg.md"
    assert found[0]["score"] >= found[-1]["score"]


def test_results_carry_an_excerpt_around_the_match(fake_doc_pages):
    """
    Intent: A list of titles cannot answer "is this the page I meant" — the alternative is
        opening every result, which is what the search exists to avoid.
    Success: The excerpt contains the matched term and is far shorter than the page.
    Feature: Documentation search — excerpts show why a page matched.
    """
    body = ("padding " * 100) + "the $unwind stage flattens arrays" + (" padding" * 100)
    doc_pages.upsert_pages([content("Stages", body, "https://x/a.md")])
    found = doc_pages.search_pages("unwind")
    assert "unwind" in found[0]["excerpt"]
    assert len(found[0]["excerpt"]) < len(body) / 2


def test_an_excerpt_is_a_single_readable_line(fake_doc_pages):
    """
    Intent: Documentation is Markdown — headings, code fences, blank lines. Dropped into a
        list row verbatim it renders as unreadable fragments.
    Success: The excerpt has no newlines and no runs of whitespace.
    Feature: Documentation search — excerpts are readable in a list.
    """
    body = "# Heading\n\n```js\ndb.c.aggregate([])\n```\n\nThe aggregate command runs a pipeline." * 5
    doc_pages.upsert_pages([content("Aggregate", body, "https://x/a.md")])
    excerpt = doc_pages.search_pages("aggregate")[0]["excerpt"]
    assert "\n" not in excerpt and "  " not in excerpt


def test_search_results_do_not_carry_the_whole_page(fake_doc_pages):
    """
    Intent: A hundred results carrying full page text would move megabytes to render one
        list, when the excerpt is all the list shows.
    Success: Results carry an excerpt and no full text field.
    Feature: Documentation search — results are lightweight.
    """
    doc_pages.upsert_pages([content("Pipelines", "The $lookup stage joins.", "https://x/a.md")])
    found = doc_pages.search_pages("lookup")
    assert "excerpt" in found[0]
    assert "text" not in found[0]


def test_an_empty_search_returns_nothing_rather_than_everything(fake_doc_pages):
    """
    Intent: A blank query reaching the database as a text search would either error or
        return the whole corpus. Neither is what an empty search box means.
    Success: An empty or whitespace query returns no results.
    Feature: Documentation search — an empty query is not a match-all.
    """
    doc_pages.upsert_pages([content("Pipelines", "The $lookup stage joins.", "https://x/a.md")])
    assert doc_pages.search_pages("") == []
    assert doc_pages.search_pages("   ") == []


def test_the_search_result_count_is_bounded(fake_doc_pages):
    """
    Intent: A broad term matches thousands of pages in a 7,000-page corpus. Returning all
        of them would be unreadable and would carry every excerpt.
    Success: The limit bounds how many results come back.
    Feature: Documentation search — bounded results.
    """
    doc_pages.upsert_pages([
        content("Page", "aggregation pipeline", f"https://x/p{i}.md") for i in range(10)
    ])
    assert len(doc_pages.search_pages("aggregation", limit=3)) == 3


def test_the_corpus_is_text_indexed_on_title_and_content(fake_doc_pages):
    """
    Intent: Without a text index the search is a full scan of 72 MB on every keystroke-worth
        of query. The title is weighted because a title match is nearly always the wanted
        page.
    Success: A text index exists over title and text, with the title weighted higher.
    Feature: Documentation search — indexed and weighted.
    """
    doc_pages.ensure_indexes()
    text_index = next(i for i in fake_doc_pages.indexes if i["name"] == "title_text_search")
    assert dict(text_index["keys"]) == {"title": "text", "text": "text"}
    assert text_index["weights"]["title"] > text_index["weights"]["text"]


def test_stubs_stored_before_the_floor_existed_can_be_pruned(fake_doc_pages):
    """
    Intent: A refresh would drop them, since the sweep removes what a run did not write —
        but that means a full crawl before the listings stop being cluttered by pages
        nothing can be written from.
    Success: Pruning removes pages under the floor and keeps the rest.
    Feature: Documentation corpus — existing stubs can be removed without a full crawl.
    """
    doc_pages.upsert_pages([
        content("Stub", "x" * 50, "https://x/stub.md"),
        content("Real", "y" * 5000, "https://x/real.md"),
    ])
    assert doc_pages.delete_stubs(500) == 1
    assert [d["url"] for d in fake_doc_pages.docs] == ["https://x/real.md"]


def test_a_title_only_match_still_gets_an_excerpt(fake_doc_pages):
    """
    Intent: A page can match on its title alone, with the term absent from the body. With no
        excerpt the result row would be a bare title, which is the case where context is
        most needed — nothing on screen would say what the page contains.
    Success: The excerpt falls back to the start of the page rather than being empty.
    Feature: Documentation search — every result shows context.
    """
    doc_pages.upsert_pages([
        content("Aggregation", "Documents pass through stages in order.", "https://x/a.md")
    ])
    found = doc_pages.search_pages("aggregation")
    assert found[0]["excerpt"].startswith("Documents pass through stages")


def test_a_long_page_matched_only_by_title_is_excerpted_not_dumped(fake_doc_pages):
    """
    Intent: Falling back to "the start of the page" must still be a window. A 77 KB page
        returned whole would defeat the point of excerpts and move megabytes into a list.
    Success: The fallback excerpt is truncated and marked as truncated.
    Feature: Documentation search — excerpts stay short.
    """
    doc_pages.upsert_pages([
        content("Aggregation", "prose " * 5000, "https://x/a.md")
    ])
    excerpt = doc_pages.search_pages("aggregation")[0]["excerpt"]
    assert len(excerpt) < 400
    assert excerpt.endswith("…")
