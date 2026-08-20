"""Tests for app/repositories/doc_chunks.py — storing the sections.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from app.repositories import doc_chunks


def chunk(chunk_id: str, url: str = "https://x/a.md", ordinal: int = 0, **overrides) -> dict:
    return {
        "chunk_id": chunk_id,
        "url": url,
        "source": "https://x/llms.txt",
        "page_title": "Replication",
        "heading": "Failover",
        "heading_path": ["Replication"],
        "ordinal": ordinal,
        "text": "An election picks a new primary.",
        "embed_text": "Replication > Failover\n\nAn election picks a new primary.",
        "chars": 31,
        "bytes": 31,
        **overrides,
    }


def test_a_pages_chunks_are_stored(fake_doc_chunks):
    """
    Intent: Chunks are what retrieval and authoring read, so they have to be stored — a
        chunker whose output went nowhere would leave the corpus unusable while looking
        healthy.
    Success: Storing a page's chunks makes them retrievable by id.
    Feature: Chunk storage — chunks are persisted.
    """
    doc_chunks.replace_page_chunks("https://x/a.md", [chunk("c1"), chunk("c2", ordinal=1)])
    assert doc_chunks.count() == 2
    assert doc_chunks.get_chunk("c1")["heading"] == "Failover"


def test_rechunking_a_page_replaces_its_chunks(fake_doc_chunks):
    """
    Intent: Chunking is deterministic, but a page whose text has changed produces a different
        number of chunks. Adding to the old set would leave orphans from the previous shape
        retrievable — sections of text the page no longer contains.
    Success: Re-storing a page's chunks leaves only the new ones.
    Feature: Chunk storage — a page's chunks are replaced as a set.
    """
    doc_chunks.replace_page_chunks(
        "https://x/a.md", [chunk("c1"), chunk("c2", ordinal=1), chunk("c3", ordinal=2)]
    )
    doc_chunks.replace_page_chunks("https://x/a.md", [chunk("c1")])
    assert doc_chunks.count() == 1
    assert doc_chunks.get_chunk("c2") is None


def test_rechunking_one_page_leaves_another_alone(fake_doc_chunks):
    """
    Intent: The replacement is scoped to a page. Scoped any wider, re-chunking one page during
        a refresh would delete the corpus a page at a time.
    Success: Replacing one page's chunks does not touch another page's.
    Feature: Chunk storage — replacement is per page.
    """
    doc_chunks.replace_page_chunks("https://x/a.md", [chunk("a1")])
    doc_chunks.replace_page_chunks("https://x/b.md", [chunk("b1", url="https://x/b.md")])
    doc_chunks.replace_page_chunks("https://x/a.md", [chunk("a2")])
    assert doc_chunks.get_chunk("b1") is not None


def test_chunks_this_run_did_not_write_are_swept(fake_doc_chunks):
    """
    Intent: A refresh sweeps pages MongoDB no longer publishes. A chunk outliving its page is
        invisible and harmful: retrieval keeps offering it, and a question written from it cites
        a URL that now 404s.
    Success: Chunks stamped with an earlier run are removed.
    Feature: Chunk storage — chunks are swept with their pages.
    """
    doc_chunks.replace_page_chunks("https://x/old.md", [chunk("old", url="https://x/old.md")], "run-1")
    doc_chunks.replace_page_chunks("https://x/new.md", [chunk("new", url="https://x/new.md")], "run-2")
    assert doc_chunks.delete_not_in_run("run-2") == 1
    assert doc_chunks.get_chunk("old") is None
    assert doc_chunks.get_chunk("new") is not None


def test_a_pages_chunks_come_back_in_order(fake_doc_chunks):
    """
    Intent: A page's chunks are its sections in reading order, and a viewer that listed them
        arbitrarily would misrepresent the page it is showing.
    Success: Chunks are listed by their position in the page.
    Feature: Chunk storage — a page's chunks are ordered.
    """
    doc_chunks.replace_page_chunks(
        "https://x/a.md",
        [chunk("c3", ordinal=2), chunk("c1", ordinal=0), chunk("c2", ordinal=1)],
    )
    listed = doc_chunks.chunks_for_page("https://x/a.md")
    assert [c["chunk_id"] for c in listed] == ["c1", "c2", "c3"]


def test_a_listing_leaves_out_the_chunk_text(fake_doc_chunks):
    """
    Intent: The corpus is 18,000 chunks. Carrying their text into a listing is megabytes that
        no screen showing a table of sections uses.
    Success: Listed chunks omit the text and the embedded text; the full chunk has both.
    Feature: Chunk storage — listings are summaries.
    """
    doc_chunks.replace_page_chunks("https://x/a.md", [chunk("c1")])
    listed = doc_chunks.chunks_for_page("https://x/a.md")[0]
    assert "text" not in listed and "embed_text" not in listed
    assert doc_chunks.get_chunk("c1")["text"]


def test_an_unknown_chunk_is_absent_rather_than_an_error(fake_doc_chunks):
    """
    Intent: A walk resolves a set and then reads each chunk, and a rebuild between those two
        steps can remove one. The walk has to be able to step over it, which needs absence to
        be an answer rather than an exception.
    Success: Fetching an unknown id returns None.
    Feature: Chunk storage — an unknown chunk is reported as absent.
    """
    assert doc_chunks.get_chunk("nope") is None


def test_the_corpus_totals_describe_the_chunking(fake_doc_chunks):
    """
    Intent: The band is a judgement that will want re-tuning, and the only way to see whether a
        change helped is the shape it produced — how many chunks, over how many pages, at what
        mean size.
    Success: Totals report chunk count, page count and mean size.
    Feature: Chunk storage — the chunking is measurable.
    """
    doc_chunks.replace_page_chunks("https://x/a.md", [chunk("a1", chars=100), chunk("a2", ordinal=1, chars=300)])
    doc_chunks.replace_page_chunks("https://x/b.md", [chunk("b1", url="https://x/b.md", chars=200)])
    totals = doc_chunks.totals()
    assert totals["chunks"] == 3
    assert totals["pages"] == 2
    assert totals["mean_chars"] == 200


def test_empty_totals_do_not_divide_by_zero(fake_doc_chunks):
    """
    Intent: The documentation screen is opened before anything is chunked, and an empty
        aggregation returns no rows at all. Read naively that is a crash on the first render.
    Success: With nothing stored the totals are zeroes.
    Feature: Chunk storage — an empty corpus totals cleanly.
    """
    assert doc_chunks.totals() == {"chunks": 0, "chars": 0, "pages": 0, "mean_chars": 0}


def test_chunks_are_indexed_for_the_way_they_are_used(fake_doc_chunks):
    """
    Intent: Every read keys on chunk_id or on url with an order, and a refresh replaces a page's
        chunks 3,844 times. Unindexed those are collection scans over 18,000 documents, and a
        duplicate chunk_id would make two sections indistinguishable.
    Success: Indexes exist for chunk_id (unique), url with ordinal, and source.
    Feature: Chunk storage — queryable by id, page and source.
    """
    doc_chunks.ensure_indexes()
    by_name = {index["name"]: index for index in fake_doc_chunks.indexes}
    assert {"chunk_id_unique", "url_ordinal", "source"} <= by_name.keys()
    assert by_name["chunk_id_unique"]["unique"] is True
