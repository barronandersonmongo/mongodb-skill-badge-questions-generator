"""Tests for app/services/doc_corpus.py — crawling MongoDB's published docs index.

No test reaches the network: `_get` is replaced with a scripted fetcher.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import httpx
import pytest

from app.config import Settings
from app.services import doc_corpus

ROOT = "https://www.mongodb.com/docs/llms.txt"
INDEX = "https://www.mongodb.com/docs/manual/llms.txt"


@pytest.fixture
def web(monkeypatch):
    """Serve canned pages, and record what was requested."""
    def install(pages: dict[str, str], failures: dict[str, Exception] | None = None):
        requested: list[str] = []

        def get(url, settings):
            requested.append(url)
            if failures and url in failures:
                raise failures[url]
            if url not in pages:
                raise RuntimeError(f"404 {url}")
            return pages[url]

        monkeypatch.setattr(doc_corpus, "_get", get)
        return requested

    return install


@pytest.fixture
def stored(monkeypatch):
    """Capture what the crawl would write, without a database."""
    written: list[dict] = []

    def upsert(pages, run_id=None):
        written.extend(pages)
        return {"inserted": len(pages), "updated": 0, "unchanged": 0}

    monkeypatch.setattr(doc_corpus.doc_pages, "upsert_pages", upsert)
    monkeypatch.setattr(doc_corpus.doc_pages, "delete_not_in_run", lambda run_id: 0)
    return written


def test_the_index_yields_its_pages_and_nested_indexes():
    """
    Intent: The published index mixes page links with links to further indexes, and the
        crawl treats them completely differently. Confusing the two would either fetch an
        index as though it were documentation or skip a whole product's docs.
    Success: .md links are returned as pages and llms.txt links as indexes.
    Feature: Documentation corpus — the index is parsed correctly.
    """
    text = "- [A](https://x/a.md)\n- [B](https://x/b/llms.txt)\n- [C](https://x/c.md)\n"
    pages, indexes = doc_corpus.index_links(text)
    assert pages == ["https://x/a.md", "https://x/c.md"]
    assert indexes == ["https://x/b/llms.txt"]


def test_a_link_listed_twice_is_fetched_once():
    """
    Intent: The indexes repeat links across sections. Fetching a page once per mention
        would multiply a 10,000-page crawl by an unknown factor for no benefit.
    Success: A repeated link appears once.
    Feature: Documentation corpus — no duplicate fetches.
    """
    pages, _ = doc_corpus.index_links("[A](https://x/a.md) [A again](https://x/a.md)")
    assert pages == ["https://x/a.md"]


def test_non_documentation_links_are_ignored():
    """
    Intent: The index contains ordinary marketing and product links. Fetching those would
        store pages that are not documentation and cannot ground a question.
    Success: Links that are neither .md nor llms.txt are dropped.
    Feature: Documentation corpus — only documentation is stored.
    """
    pages, indexes = doc_corpus.index_links("[Pricing](https://www.mongodb.com/pricing)")
    assert pages == [] and indexes == []


def test_sources_are_discovered_from_the_root_index(web, settings):
    """
    Intent: Most of the corpus is driver and CLI reference a badge quiz never draws on, so
        the crawl must be selectable per source. That requires enumerating the sources
        before fetching anything.
    Success: discover_sources returns the root plus every index it names.
    Feature: Documentation corpus — sources are discoverable.
    """
    web({ROOT: f"[Manual]({INDEX})\n[Root page](https://www.mongodb.com/docs/x.md)"})
    assert doc_corpus.discover_sources(settings=settings) == [ROOT, INDEX]


def test_the_root_index_is_itself_a_source(web, settings):
    """
    Intent: The root index names a handful of pages directly. If it were treated only as a
        list of other indexes, those pages could never be stored.
    Success: The root URL is included among the sources.
    Feature: Documentation corpus — no page is unreachable.
    """
    web({ROOT: f"[Manual]({INDEX})"})
    assert ROOT in doc_corpus.discover_sources(settings=settings)


def test_a_pages_own_heading_becomes_its_title(web, settings):
    """
    Intent: A title is how a person recognises a stored page on screen and how a question's
        citation reads. The page's own heading is the accurate source for it.
    Success: The first Markdown heading is used as the title.
    Feature: Documentation corpus — pages carry a readable title.
    """
    assert doc_corpus.page_title("# Model IoT Data\n\nbody", "https://x/a.md") == "Model IoT Data"


def test_a_page_without_a_heading_falls_back_to_its_filename(web, settings):
    """
    Intent: Some pages have no heading. An empty title would render as a blank row that
        cannot be identified, so a usable fallback matters more than an exact one.
    Success: The filename without its extension is used.
    Feature: Documentation corpus — every page has a title.
    """
    assert doc_corpus.page_title("no heading here", "https://x/aggregation.md") == "aggregation"


def root_with(index_body: dict) -> dict:
    """Script the root index pointing at one source, plus that source's pages."""
    return {ROOT: f"[Manual]({INDEX})", **index_body}


def test_pages_are_fetched_and_attributed_to_their_source(web, settings, stored):
    """
    Intent: Per-source counts, per-source refreshes and per-source deletion all depend on
        each page recording which index it came from. Without it the corpus is one
        undifferentiated blob.
    Success: Fetched pages carry their text and their source index.
    Feature: Documentation corpus — pages are attributed to a source.
    """
    web(root_with({INDEX: "[A](https://x/a.md)", "https://x/a.md": "# A\nbody"}))
    doc_corpus.refresh(settings=settings)
    assert stored[0]["text"] == "# A\nbody"
    assert stored[0]["source"] == INDEX


def test_one_unreachable_page_does_not_lose_the_rest(web, settings, stored):
    """
    Intent: Across 10,000 requests to a site this program does not own, some will fail.
        Aborting the crawl would mean it could never complete, and the pages already
        fetched would be discarded.
    Success: The good page is stored and the failure is reported.
    Feature: Documentation corpus — partial failures are reported, not fatal.
    """
    web(
        root_with({INDEX: "[A](https://x/a.md)\n[B](https://x/b.md)", "https://x/a.md": "# A"}),
        failures={"https://x/b.md": RuntimeError("connection reset")},
    )
    result = doc_corpus.refresh(settings=settings)
    assert [p["url"] for p in stored] == ["https://x/a.md"]
    assert result["failure_count"] == 1
    assert "connection reset" in result["failures"][0]["error"]


def test_an_unreadable_index_does_not_abandon_the_other_sources(web, settings, stored):
    """
    Intent: A renamed or temporarily unavailable index must not stop the rest of a
        whole-corpus refresh, which takes long enough that restarting it is expensive.
    Success: The reachable source is crawled and the bad index is reported.
    Feature: Documentation corpus — one bad source does not fail the crawl.
    """
    web({
        ROOT: f"[Missing](https://x/missing/llms.txt)\n[Manual]({INDEX})",
        INDEX: "[A](https://x/a.md)",
        "https://x/a.md": "# A",
    })
    result = doc_corpus.refresh(settings=settings)
    assert [p["url"] for p in stored] == ["https://x/a.md"]
    assert any("index unreadable" in f["error"] for f in result["failures"])
    assert result["sources_done"] == 3


def test_an_enormous_page_is_skipped_rather_than_stored(web, settings, stored):
    """
    Intent: Some pages are generated API dumps rather than prose. Storing megabytes of that
        crowds the corpus with text no question can be written from, and inflates every
        read of it.
    Success: A page over the size limit is skipped and reported.
    Feature: Documentation corpus — oversized pages are excluded.
    """
    huge = "x" * (doc_corpus.MAX_PAGE_BYTES + 1)
    web(root_with({INDEX: "[Big](https://x/big.md)", "https://x/big.md": huge}))
    result = doc_corpus.refresh(settings=settings)
    assert stored == []
    assert "exceeds the page limit" in result["failures"][0]["error"]


def test_refreshing_everything_walks_the_published_index(web, settings, stored):
    """
    Intent: "Refresh everything" has to mean everything the index names, or the corpus
        silently omits products and nobody can tell which.
    Success: With no sources named, the root is read and each index it names is crawled.
    Feature: Documentation corpus — whole-corpus refresh.
    """
    web({
        ROOT: f"[Manual]({INDEX})\n[Root page](https://x/root.md)",
        "https://x/root.md": "# Root",
        INDEX: "[A](https://x/a.md)",
        "https://x/a.md": "# A",
    })
    result = doc_corpus.refresh(settings=settings)
    assert result["sources_done"] == 2
    assert sorted(p["url"] for p in stored) == ["https://x/a.md", "https://x/root.md"]


def test_progress_is_reported_while_the_crawl_runs(web, settings, stored):
    """
    Intent: A full crawl takes minutes. Without progress the screen can only show a
        spinner, and an operator cannot tell a slow crawl from a stuck one.
    Success: The progress callback is invoked with rising counts during the run.
    Feature: Documentation corpus — visible progress.
    """
    web(root_with({INDEX: "[A](https://x/a.md)", "https://x/a.md": "# A"}))
    seen: list[dict] = []
    doc_corpus.refresh(settings=settings, progress=seen.append)
    assert seen, "no progress was reported"
    assert seen[-1]["pages_seen"] == 1
    assert seen[-1]["sources_done"] == 2


def test_the_reported_failure_list_is_capped(web, settings, stored, monkeypatch):
    """
    Intent: A network outage part-way through a 10,000-page crawl would otherwise put
        thousands of entries into the run state, which is held in memory and rendered on a
        page. The count is what matters; a sample is what gets acted on.
    Success: The failure list is truncated while the count reports the true total.
    Feature: Documentation corpus — failure reporting is bounded.
    """
    urls = [f"https://x/p{i}.md" for i in range(60)]
    web(
        root_with({INDEX: "\n".join(f"[P]({u})" for u in urls)}),
        failures={u: RuntimeError("boom") for u in urls},
    )
    result = doc_corpus.refresh(settings=settings)
    # One extra failure records that nothing was stored, so nothing was swept.
    assert result["failure_count"] == 61
    assert len(result["failures"]) == 50


def test_the_index_location_is_configurable(web, stored):
    """
    Intent: The published index has moved before and will again. A hardcoded URL would mean
        editing and redeploying code to follow it.
    Success: The configured index URL is the one fetched.
    Feature: Documentation corpus — configurable index location.
    """
    settings = Settings(mongodb_uri="mongodb://test", docs_index_url="https://x/custom/llms.txt")
    requested = web({"https://x/custom/llms.txt": ""})
    doc_corpus.discover_sources(settings=settings)
    assert requested == ["https://x/custom/llms.txt"]


def test_pages_are_written_in_batches_during_a_long_crawl(web, settings, monkeypatch):
    """
    Intent: A crawl that only wrote at the end would show no progress for minutes and lose
        everything if it failed part-way. Batched writes make progress durable.
    Success: A source with more pages than the batch size produces more than one write.
    Feature: Documentation corpus — durable, incremental writes.
    """
    monkeypatch.setattr(doc_corpus, "WRITE_BATCH", 2)
    urls = [f"https://x/p{i}.md" for i in range(5)]
    web(root_with({INDEX: "\n".join(f"[P]({u})" for u in urls), **{u: "# P" for u in urls}}))

    writes: list[int] = []

    def upsert(pages, run_id=None):
        writes.append(len(pages))
        return {"inserted": len(pages), "updated": 0, "unchanged": 0}

    monkeypatch.setattr(doc_corpus.doc_pages, "upsert_pages", upsert)
    monkeypatch.setattr(doc_corpus.doc_pages, "delete_not_in_run", lambda run_id: 0)
    doc_corpus.refresh(settings=settings)
    assert len(writes) == 3 and sum(writes) == 5


# --- the real HTTP path, against a local stub rather than mongodb.com ---


@pytest.fixture
def stub_site():
    """Serve one canned document over HTTP, and record the request."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    state: dict = {"path": None, "body": b"# Stub page\ncontent", "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["path"] = self.path
            self.send_response(state["status"])
            self.send_header("Content-Type", "text/markdown")
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def test_a_page_is_fetched_over_http(stub_site, settings):
    """
    Intent: Every other test in this file replaces the fetcher, so without this one the real
        HTTP path — the thing that actually talks to MongoDB's site — is never executed and
        could be broken in a way the suite cannot see.
    Success: The fetcher retrieves the served body from the requested URL.
    Feature: Documentation corpus — pages are fetched over HTTP.
    """
    assert doc_corpus._get(stub_site["base"] + "/a.md", settings) == "# Stub page\ncontent"
    assert stub_site["path"] == "/a.md"


def test_an_http_error_is_raised_rather_than_stored_as_a_page(stub_site, settings):
    """
    Intent: An error page returned as content would be stored as documentation, and a
        question could then be written from a 500 page. The status must be checked.
    Success: A non-success status raises instead of returning the body.
    Feature: Documentation corpus — error responses are not stored as documentation.
    """
    stub_site["status"] = 500
    with pytest.raises(httpx.HTTPStatusError):
        doc_corpus._get(stub_site["base"] + "/a.md", settings)


# --- a refresh replaces the corpus ---


def test_a_refresh_removes_pages_that_are_no_longer_published(web, settings, monkeypatch):
    """
    Intent: Refreshing means "make the corpus what MongoDB publishes now". A page withdrawn
        upstream has to go, or the corpus accumulates documentation for features that no
        longer exist and a question could be written from it.
    Success: The crawl sweeps pages not written by this run, and reports how many.
    Feature: Documentation corpus — a refresh replaces what is stored.
    """
    swept: list[str] = []
    monkeypatch.setattr(doc_corpus.doc_pages, "upsert_pages",
                        lambda pages, run_id=None: {"inserted": len(pages), "updated": 0,
                                                    "unchanged": 0})
    monkeypatch.setattr(doc_corpus.doc_pages, "delete_not_in_run",
                        lambda run_id: swept.append(run_id) or 7)
    web(root_with({INDEX: "[A](https://x/a.md)", "https://x/a.md": "# A"}))
    result = doc_corpus.refresh(settings=settings)
    assert result["removed"] == 7
    assert swept == [result["run_id"]]


def test_a_crawl_that_stored_nothing_does_not_wipe_the_corpus(web, settings, monkeypatch):
    """
    Intent: The dangerous case. If the index moves or the network fails, the crawl stores
        nothing — and a blind sweep would then delete the entire corpus and report a
        successful replacement. The previous corpus must survive a failed refresh.
    Success: With no pages stored, nothing is swept and the run says so.
    Feature: Documentation corpus — a failed refresh leaves the corpus intact.
    """
    swept: list[str] = []
    monkeypatch.setattr(doc_corpus.doc_pages, "delete_not_in_run",
                        lambda run_id: swept.append(run_id) or 0)
    web({ROOT: f"[Manual]({INDEX})", INDEX: "[A](https://x/a.md)"},
        failures={"https://x/a.md": RuntimeError("connection reset")})
    result = doc_corpus.refresh(settings=settings)
    assert swept == []
    assert result["removed"] == 0
    assert any("nothing was removed" in f["error"] for f in result["failures"])


def test_each_refresh_uses_its_own_run_stamp(web, settings, stored):
    """
    Intent: The sweep deletes everything not stamped with the current run. If two refreshes
        shared a stamp, the second would not be able to tell its own pages from the first's
        and stale pages would survive forever.
    Success: Two refreshes report different run ids.
    Feature: Documentation corpus — each refresh is identifiable.
    """
    web(root_with({INDEX: "[A](https://x/a.md)", "https://x/a.md": "# A"}))
    first = doc_corpus.refresh(settings=settings)["run_id"]
    second = doc_corpus.refresh(settings=settings)["run_id"]
    assert first != second


# --- progress reporting: how far through, how fast, how much longer ---


def test_the_total_is_known_before_any_page_is_fetched(web, settings, stored):
    """
    Intent: A progress report that only counts what it has already done cannot say how far
        through it is or how much longer it will take — the two things a person watching a
        ten-minute crawl actually wants. Reading the indexes first costs one request each
        against thousands for the pages.
    Success: The first fetching-phase snapshot already knows the page total.
    Feature: Documentation corpus — progress knows the size of the job.
    """
    web({
        ROOT: f"[Manual]({INDEX})",
        INDEX: "[A](https://x/a.md)\n[B](https://x/b.md)",
        "https://x/a.md": "# A", "https://x/b.md": "# B",
    })
    seen: list[dict] = []
    doc_corpus.refresh(settings=settings, progress=seen.append)
    fetching = [s for s in seen if s["phase"] == "fetching"]
    assert fetching and fetching[0]["pages_total"] == 2


def test_the_planning_phase_is_reported_as_its_own_phase(web, settings, stored):
    """
    Intent: Reading 74 indexes takes seconds during which no page has been fetched. Reported
        as "0 of 0 pages" that reads as a stuck crawl, so the phase has to be visible.
    Success: A planning snapshot is reported, with no page total yet, before fetching.
    Feature: Documentation corpus — the planning phase is visible.
    """
    web({ROOT: f"[Manual]({INDEX})", INDEX: "[A](https://x/a.md)", "https://x/a.md": "# A"})
    seen: list[dict] = []
    doc_corpus.refresh(settings=settings, progress=seen.append)
    phases = [s["phase"] for s in seen]
    assert phases[0] == "planning"
    assert phases.index("planning") < phases.index("fetching")
    assert seen[0]["percent"] is None


def test_progress_reports_rate_share_and_time_remaining(web, settings, stored):
    """
    Intent: "How fast", "how far" and "how much longer" are the three questions a long crawl
        has to answer, and each is derived from figures only the crawl holds.
    Success: A fetching snapshot carries a rate, a percentage and an estimate, and the final
        snapshot is at 100%.
    Feature: Documentation corpus — useful progress figures.
    """
    urls = [f"https://x/p{i}.md" for i in range(6)]
    web({
        ROOT: f"[Manual]({INDEX})",
        INDEX: "\n".join(f"[P]({u})" for u in urls),
        **{u: "# P" for u in urls},
    })
    seen: list[dict] = []
    doc_corpus.refresh(settings=settings, progress=seen.append)
    mid = [s for s in seen if s["phase"] == "fetching" and s["pages_seen"]]
    assert mid, "no page-level progress was reported"
    assert mid[-1]["percent"] == 100.0
    assert all("pages_per_second" in s and "eta_seconds" in s for s in seen)
    assert all(s["elapsed_seconds"] >= 0 for s in seen)


def test_no_estimate_is_invented_before_the_work_is_understood(web, settings, stored):
    """
    Intent: A confident "0% complete, 0 seconds remaining" is worse than admitting the crawl
        does not know yet — it reads as finished, or as broken.
    Success: While planning, the percentage and the estimate are both absent.
    Feature: Documentation corpus — honest progress.
    """
    web({ROOT: f"[Manual]({INDEX})", INDEX: "[A](https://x/a.md)", "https://x/a.md": "# A"})
    seen: list[dict] = []
    doc_corpus.refresh(settings=settings, progress=seen.append)
    planning = [s for s in seen if s["phase"] == "planning"]
    assert planning
    assert all(s["percent"] is None and s["eta_seconds"] is None for s in planning)


def test_a_page_named_by_two_indexes_is_counted_and_fetched_once(web, settings, stored):
    """
    Intent: The indexes overlap. Counting a shared page twice would inflate the total, so the
        crawl would appear to stall short of 100%, and fetching it twice would pay for it
        twice.
    Success: A page named by two indexes appears once in the total and once in storage.
    Feature: Documentation corpus — no duplicate work.
    """
    other = "https://www.mongodb.com/docs/other/llms.txt"
    web({
        ROOT: f"[One]({INDEX})\n[Two]({other})",
        INDEX: "[Shared](https://x/shared.md)",
        other: "[Shared](https://x/shared.md)",
        "https://x/shared.md": "# Shared",
    })
    result = doc_corpus.refresh(settings=settings)
    assert result["pages_total"] == 1
    assert [p["url"] for p in stored] == ["https://x/shared.md"]


def test_the_phases_end_at_done(web, settings, stored):
    """
    Intent: The screen decides what to show from the phase. A run that never reports a final
        phase would leave the progress bar animating after the crawl had finished.
    Success: The last snapshot's phase is "done" and includes the total elapsed time.
    Feature: Documentation corpus — the end of a run is reported.
    """
    web({ROOT: f"[Manual]({INDEX})", INDEX: "[A](https://x/a.md)", "https://x/a.md": "# A"})
    seen: list[dict] = []
    doc_corpus.refresh(settings=settings, progress=seen.append)
    assert seen[-1]["phase"] == "done"
    assert seen[-1]["elapsed_seconds"] >= 0
    assert "sweeping" in [s["phase"] for s in seen]


def test_an_unreadable_root_index_fails_the_run_without_sweeping(web, settings, monkeypatch):
    """
    Intent: Without the root index there is nothing to crawl, so a sweep would delete a
        corpus this run cannot replace. This is the one failure that must stop the run
        rather than be collected and carried on from.
    Success: The run reports the failure, phase "failed", and never sweeps.
    Feature: Documentation corpus — an unreachable index cannot destroy the corpus.
    """
    swept: list[str] = []
    monkeypatch.setattr(doc_corpus.doc_pages, "delete_not_in_run",
                        lambda run_id: swept.append(run_id) or 0)
    web({}, failures={ROOT: RuntimeError("dns failure")})
    result = doc_corpus.refresh(settings=settings)
    assert result["phase"] == "failed"
    assert swept == []
    assert "dns failure" in result["failures"][0]["error"]


def test_every_source_is_planned_even_when_one_index_is_unreadable(web, settings, stored):
    """
    Intent: One bad index among 74 must not reduce the total to the pages of the sources read
        before it — the percentage would then be measured against the wrong denominator and
        the crawl would appear to overshoot.
    Success: The readable source's pages are planned and fetched, and the bad index is
        reported as a failure.
    Feature: Documentation corpus — planning survives a bad index.
    """
    bad = "https://www.mongodb.com/docs/bad/llms.txt"
    web({
        ROOT: f"[Bad]({bad})\n[Manual]({INDEX})",
        INDEX: "[A](https://x/a.md)",
        "https://x/a.md": "# A",
    }, failures={bad: RuntimeError("500 error")})
    result = doc_corpus.refresh(settings=settings)
    assert result["pages_total"] == 1
    assert result["pages_seen"] == 1
    assert any("index unreadable" in f["error"] for f in result["failures"])
