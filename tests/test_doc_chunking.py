"""Tests for app/services/doc_chunking.py — splitting a page into sections.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from dataclasses import replace

from app.services import doc_chunking


def page(text: str, **overrides) -> dict:
    return {
        "url": "https://x/a.md",
        "source": "https://x/llms.txt",
        "title": "Replication",
        "text": text,
        **overrides,
    }


def test_a_page_is_cut_at_its_headings(settings):
    """
    Intent: A page was the wrong unit twice over: sent whole, a 1.7 MB page cost $2.58 for
        three questions, and capped, everything past the cap was unreachable. Headings are
        where a page already divides itself, so they are where it should be cut.
    Success: A page with two well-sized sections becomes two chunks, each holding its own
        section.
    Feature: Documentation chunking — pages are split at headings.
    """
    text = (
        "## Replica sets\n\n" + "A replica set keeps copies. " * 80 +
        "\n\n## Failover\n\n" + "An election picks a new primary. " * 80
    )
    chunks = doc_chunking.split_page(page(text), settings=settings)
    assert len(chunks) == 2
    assert chunks[0]["heading"] == "Replica sets"
    assert chunks[1]["heading"] == "Failover"
    assert "election" in chunks[1]["text"]


def test_small_sections_are_merged_rather_than_stored_alone(settings):
    """
    Intent: Measured on this corpus on 2026-08-19: splitting at H1-H3 gives 40,561 sections
        with a median of 642 characters and 47% under 500. A corpus of heading stubs embeds
        badly and supports no question at all, so merging is the dominant operation — not
        splitting.
    Success: Several tiny sections become one chunk rather than several.
    Feature: Documentation chunking — small sections are merged.
    """
    text = "".join(f"## Heading {n}\n\nOne short line.\n\n" for n in range(8))
    chunks = doc_chunking.split_page(page(text), settings=settings)
    assert len(chunks) == 1
    assert "Heading 0" in chunks[0]["text"] and "Heading 7" in chunks[0]["text"]


def test_an_oversize_section_is_split_on_paragraphs(settings):
    """
    Intent: A handful of sections run to hundreds of kilobytes because the heading structure
        gives out inside one enormous code block or table. Left whole they reproduce the very
        bug chunking exists to fix, so the ceiling has to hold even where headings do not.
    Success: A section far over the ceiling becomes several chunks, none over it.
    Feature: Documentation chunking — the ceiling holds without headings.
    """
    body = "\n\n".join("A paragraph about replication." * 20 for _ in range(40))
    chunks = doc_chunking.split_page(page(f"## Replication\n\n{body}"), settings=settings)
    assert len(chunks) > 1
    assert all(c["chars"] <= settings.chunk_ceiling_chars for c in chunks)


def test_a_single_huge_paragraph_is_still_cut(settings):
    """
    Intent: One unbroken block — a giant generated table, a minified sample — has no paragraph
        boundary to cut on. Sending it whole is exactly the $2.58 failure, so a blunt cut is
        the right answer: the alternative is no bound at all.
    Success: A single paragraph over the ceiling is cut into pieces within it.
    Feature: Documentation chunking — no chunk exceeds the ceiling.
    """
    chunks = doc_chunking.split_page(page("## Big\n\n" + "x" * 50_000), settings=settings)
    assert len(chunks) > 1
    assert all(c["chars"] <= settings.chunk_ceiling_chars for c in chunks)


def test_the_page_opening_is_kept(settings):
    """
    Intent: The text before the first heading is often the only prose that says what the page
        is for. Dropped, the most orienting part of the page would be the one part no question
        could ever be written from.
    Success: Text ahead of the first heading appears in the chunks.
    Feature: Documentation chunking — the page opening is not discarded.
    """
    text = "This page explains replication in detail.\n\n## Replica sets\n\nCopies."
    chunks = doc_chunking.split_page(page(text), settings=settings)
    assert any("This page explains replication" in c["text"] for c in chunks)


def test_a_chunk_carries_the_heading_path_above_it(settings):
    """
    Intent: A section read away from its page needs its context: "Limitations" means nothing
        alone and everything under "Atlas Vector Search > Filtering > Limitations". Without the
        path a chunk cannot be embedded usefully or judged by a reader.
    Success: A nested heading's chunk reports the headings above it, outermost first.
    Feature: Documentation chunking — chunks know where they sit.
    """
    # Each section is substantial, so none is absorbed into the one before it — when a
    # section is too small to stand alone the merge keeps the earlier, broader heading,
    # which would hide the nested one this test is about.
    text = (
        "# Atlas Vector Search\n\n" + "Intro prose. " * 200 +
        "\n\n## Filtering\n\n" + "How filters work. " * 200 +
        "\n\n### Limitations\n\n" + "What you cannot do. " * 200
    )
    chunks = doc_chunking.split_page(page(text), settings=settings)
    deepest = [c for c in chunks if c["heading"] == "Limitations"]
    assert deepest, [c["heading"] for c in chunks]
    assert deepest[0]["heading_path"] == ["Atlas Vector Search", "Filtering"]


def test_the_embedded_text_leads_with_that_context(settings):
    """
    Intent: A chunk is embedded and retrieved out of context by definition, so the context has
        to travel inside the embedded text. "Limitations" embedded bare matches every
        limitations section in the corpus.
    Success: The embedded text opens with the page title and heading path before the body.
    Feature: Documentation chunking — context is embedded with the chunk.
    """
    text = "## Failover\n\n" + "An election picks a new primary. " * 80
    chunk = doc_chunking.split_page(page(text), settings=settings)[0]
    assert chunk["embed_text"].startswith("Replication > Failover")
    assert "election" in chunk["embed_text"]


def test_a_chunk_records_where_it_came_from(settings):
    """
    Intent: A chunk has to be traceable without joining back to its page — a question cites the
        URL, a reviewer needs the page title and heading, and a rebuild needs the position. All
        of it is cheap to store and impossible to reconstruct later.
    Success: A chunk carries its url, source index, page title, heading, ordinal and size.
    Feature: Documentation chunking — chunks carry their provenance.
    """
    text = "## Failover\n\n" + "An election picks a new primary. " * 80
    chunk = doc_chunking.split_page(page(text), settings=settings)[0]
    assert chunk["url"] == "https://x/a.md"
    assert chunk["source"] == "https://x/llms.txt"
    assert chunk["page_title"] == "Replication"
    assert chunk["heading"] == "Failover"
    assert chunk["ordinal"] == 0
    assert chunk["chars"] > 0 and chunk["bytes"] > 0
    assert chunk["content_hash"]


def test_a_chunk_has_an_anchor_for_its_heading(settings):
    """
    Intent: A chunk is part of a page, and pointing a reader at roughly the right place in that
        page is worth the few characters. Approximate by nature, so nothing depends on it
        resolving.
    Success: A heading becomes a slug anchor.
    Feature: Documentation chunking — chunks carry a heading anchor.
    """
    text = "## Replica Set Elections\n\n" + "How elections work. " * 100
    chunk = doc_chunking.split_page(page(text), settings=settings)[0]
    assert chunk["anchor"] == "replica-set-elections"


def test_chunk_ids_are_stable_across_a_rebuild(settings):
    """
    Intent: The band will want re-tuning against real question quality, so chunks get rebuilt.
        If ids changed each time, every question's record of what it was written from would go
        stale and a walk would re-mine material it had already used.
    Success: Splitting the same page twice produces the same ids.
    Feature: Documentation chunking — chunk identity survives a rebuild.
    """
    text = "## Failover\n\n" + "An election picks a new primary. " * 80
    first = doc_chunking.split_page(page(text), settings=settings)
    second = doc_chunking.split_page(page(text), settings=settings)
    assert [c["chunk_id"] for c in first] == [c["chunk_id"] for c in second]


def test_different_positions_get_different_ids(settings):
    """
    Intent: Ids key on the page and the position within it. Two chunks of one page colliding
        would make them indistinguishable — a walk would skip one having used the other, and
        lose material silently.
    Success: The chunks of one page all have distinct ids.
    Feature: Documentation chunking — chunk ids are unique within a page.
    """
    text = "".join(
        f"## Heading {n}\n\n" + "Substantial prose about replication. " * 100
        for n in range(4)
    )
    chunks = doc_chunking.split_page(page(text), settings=settings)
    ids = [c["chunk_id"] for c in chunks]
    assert len(set(ids)) == len(ids) > 1


def test_a_page_with_no_headings_still_chunks(settings):
    """
    Intent: Not every page has headings — some are a single block of prose. Returning nothing
        would silently exclude them from the corpus, and they would look like pages nobody had
        got round to.
    Success: A page with no headings produces at least one chunk.
    Feature: Documentation chunking — a page without headings is still usable.
    """
    chunks = doc_chunking.split_page(
        page("Just prose about replication, with no headings at all."), settings=settings
    )
    assert len(chunks) == 1
    assert chunks[0]["heading"] is None


def test_an_empty_page_produces_nothing(settings):
    """
    Intent: A page that failed to fetch, or is whitespace, has nothing to write from. Storing an
        empty chunk would put a retrievable record in the corpus that can only waste a call.
    Success: An empty or whitespace page produces no chunks.
    Feature: Documentation chunking — nothing is made from nothing.
    """
    assert doc_chunking.split_page(page(""), settings=settings) == []
    assert doc_chunking.split_page(page("   \n\n  "), settings=settings) == []


def test_the_band_is_configurable(settings):
    """
    Intent: The floor and ceiling were measured against this corpus, not derived from first
        principles, so they will want re-tuning against real question quality. Hard-coded, that
        would mean a code change and a redeploy to try a different band.
    Success: A different ceiling changes how the same page is cut.
    Feature: Documentation chunking — the band comes from configuration.
    """
    text = "".join(
        f"## Heading {n}\n\n" + "Prose about replication. " * 60 for n in range(6)
    )
    wide = doc_chunking.split_page(
        page(text), settings=replace(settings, chunk_ceiling_chars=20_000)
    )
    narrow = doc_chunking.split_page(
        page(text), settings=replace(settings, chunk_ceiling_chars=2_000)
    )
    assert len(narrow) > len(wide)


def test_headings_deeper_than_the_depth_are_not_split_on(settings):
    """
    Intent: Splitting at every heading level makes a chunk per bullet-heading in a reference
        table, which is how a corpus fills with stubs. The depth is a measured choice — H1-H3
        gave the best size distribution on this corpus — so it has to be honoured.
    Success: An H4 does not start a new chunk when the depth is 3.
    Feature: Documentation chunking — the split depth is respected.
    """
    text = (
        "## Failover\n\n" + "Prose about elections. " * 100 +
        "\n\n#### A deep aside\n\n" + "More detail. " * 20
    )
    chunks = doc_chunking.split_page(
        page(text), settings=replace(settings, chunk_heading_depth=3)
    )
    assert all(c["heading"] != "A deep aside" for c in chunks)
