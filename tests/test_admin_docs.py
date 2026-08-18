"""Tests for the documentation corpus screen and its API.

Assertions on the page are on markup, never a bare word: the template ships
JavaScript containing the same labels it renders.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import time

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from app.main import app
from app.repositories import doc_pages
from app.routers import admin_docs as api_module

API = "/api/admin/docs"
PAGE = "/admin/docs"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def reset_run_state():
    reset = dict(
        running=False, last_result=None, last_error=None, last_traceback=None,
        started_at=None, finished_at=None, progress=None,
    )
    api_module._run_state.update(reset)
    yield
    api_module._run_state.update(reset)


def seed(url: str = "https://www.mongodb.com/docs/a.md", source: str = "ix-1") -> None:
    doc_pages.upsert_pages([{"url": url, "source": source, "title": "A", "text": "# A\nbody"}])


def test_a_refresh_runs_in_the_background(client, monkeypatch, fake_doc_pages):
    """
    Intent: A whole-corpus crawl is around 10,000 pages and takes minutes. Doing it inside
        the request would hold the browser open past its timeout and lose the run.
    Success: POST /refresh returns started immediately, and the crawl runs.
    Feature: Documentation corpus — long refreshes do not block the request.
    """
    calls: list = []
    monkeypatch.setattr(api_module.doc_corpus, "refresh",
                        lambda **kw: calls.append(kw) or {})
    response = client.post(API + "/refresh")
    assert response.status_code == 200
    assert response.json() == {"started": True, "mode": "replace"}
    assert len(calls) == 1
    assert calls[0]["mode"] == "replace"


def test_a_second_refresh_is_refused_while_one_runs(client, fake_doc_pages):
    """
    Intent: Two concurrent crawls would fight over the same documents and overwrite each
        other's reported progress, so the screen would describe neither run accurately.
    Success: A refresh returns 409 while one is in progress.
    Feature: Documentation corpus — one refresh at a time.
    """
    api_module._run_state["running"] = True
    assert client.post(API + "/refresh").status_code == 409


def test_a_refresh_does_not_block_the_other_jobs(client, monkeypatch, fake_doc_pages):
    """
    Intent: A docs crawl takes minutes. If it shared run state with generation or the badge
        sync, it would block unrelated work and its result could be reported as theirs.
    Success: A running docs refresh leaves the question-generation state untouched.
    Feature: Documentation corpus — independent run state.
    """
    from app.routers import questions as questions_api

    api_module._run_state["running"] = True
    assert questions_api.run_state()["running"] is False


def test_progress_is_reported_while_a_refresh_runs(client, monkeypatch, fake_doc_pages):
    """
    Intent: Without progress the screen shows a spinner for minutes and an operator cannot
        tell a slow crawl from a stuck one.
    Success: The status endpoint exposes the crawl's progress snapshot.
    Feature: Documentation corpus — visible progress.
    """
    def crawl(mode="replace", progress=None):
        progress({"sources_done": 1, "sources_requested": 2, "pages_seen": 40,
                  "inserted": 40, "updated": 0})
        return {"inserted": 40}

    monkeypatch.setattr(api_module.doc_corpus, "refresh", crawl)
    client.post(API + "/refresh")
    state = client.get(API + "/refresh/status").json()
    assert state["progress"]["pages_seen"] == 40


def test_the_refresh_is_timed_on_the_server(client, monkeypatch, fake_doc_pages):
    """
    Intent: A crawl outlives the page. Timing it in the browser would restart the elapsed
        count at zero whenever someone navigated away and back, as happened on the other
        screens.
    Success: The status reports started_at, finished_at and the server clock.
    Feature: Documentation corpus — elapsed time survives leaving the page.
    """
    monkeypatch.setattr(api_module.doc_corpus, "refresh", lambda **kw: {"inserted": 0})
    client.post(API + "/refresh")
    state = client.get(API + "/refresh/status").json()
    assert state["finished_at"] >= state["started_at"]
    assert abs(state["server_time"] - time.time()) < 5


def test_a_failed_refresh_is_reported_with_its_traceback(client, monkeypatch, fake_doc_pages):
    """
    Intent: A background failure has nowhere to surface. Swallowed, it looks like a corpus
        that simply has no pages.
    Success: The status reports the error and a traceback, and is no longer running.
    Feature: Documentation corpus — failures are surfaced.
    """
    def explode(**kwargs):
        raise RuntimeError("index unreachable")

    monkeypatch.setattr(api_module.doc_corpus, "refresh", explode)
    client.post(API + "/refresh")
    state = client.get(API + "/refresh/status").json()
    assert state["running"] is False
    assert "index unreachable" in state["last_error"]
    assert "RuntimeError" in state["last_traceback"]


def test_the_sources_endpoint_reports_stored_and_available_sources(
    client, monkeypatch, fake_doc_pages
):
    """
    Intent: A source that exists upstream but has never been crawled must be listable, or it
        can never be selected for a first fetch. Reporting only what is stored would hide
        every new product's docs.
    Success: Both the stored summary and the upstream source list are returned.
    Feature: Documentation corpus — uncrawled sources are visible.
    """
    seed(source="ix-1")
    monkeypatch.setattr(api_module.doc_corpus, "discover_sources",
                        lambda: ["ix-1", "ix-2-never-crawled"])
    body = client.get(API + "/sources").json()
    assert [s["source"] for s in body["stored"]] == ["ix-1"]
    assert "ix-2-never-crawled" in body["available"]
    assert body["totals"]["pages"] == 1


def test_an_unreachable_index_does_not_hide_what_is_stored(client, monkeypatch, fake_doc_pages):
    """
    Intent: If reading the published index failed and that blanked the screen, an operator
        would conclude the corpus was empty and re-crawl 10,000 pages they already had.
    Success: The stored sources are still returned, with the discovery error named.
    Feature: Documentation corpus — storage survives an upstream outage.
    """
    seed(source="ix-1")

    def explode():
        raise RuntimeError("dns failure")

    monkeypatch.setattr(api_module.doc_corpus, "discover_sources", explode)
    body = client.get(API + "/sources").json()
    assert [s["source"] for s in body["stored"]] == ["ix-1"]
    assert "dns failure" in body["discovery_error"]


def test_stored_pages_can_be_listed_without_their_text(client, fake_doc_pages):
    """
    Intent: Confirming what a crawl captured needs the page list; the corpus is ~80 MB, so
        that list must not carry the text.
    Success: Listed pages omit the text field.
    Feature: Documentation corpus — lightweight listing.
    """
    seed()
    body = client.get(API + "/pages").json()
    assert body and "text" not in body[0]
    assert body[0]["url"].endswith("a.md")


def test_a_single_page_can_be_read_with_its_text(client, fake_doc_pages):
    """
    Intent: Authoring reads the stored text, and a reviewer checking grounding needs to see
        exactly what was captured for a URL.
    Success: The page endpoint returns the stored text.
    Feature: Documentation corpus — a page's text is retrievable.
    """
    seed()
    body = client.get(API + "/page", params={"url": "https://www.mongodb.com/docs/a.md"}).json()
    assert body["text"] == "# A\nbody"


def test_asking_for_an_unstored_page_is_a_404(client, fake_doc_pages):
    """
    Intent: A URL that was never crawled must be reported as absent rather than as an empty
        page, which authoring would treat as real but contentless source material.
    Success: An unknown URL returns 404.
    Feature: Documentation corpus — unknown pages are reported.
    """
    assert client.get(API + "/page", params={"url": "https://x/none.md"}).status_code == 404


def test_the_screen_is_in_the_admin_area(client, fake_doc_pages):
    """
    Intent: Maintaining the corpus is curation work, like the badge catalog — nobody writing
        a question should have to think about it. It belongs behind /admin and must be
        reachable from the nav.
    Success: The page renders under /admin with its nav link marked as admin.
    Feature: Documentation corpus — an admin screen.
    """
    response = client.get(PAGE)
    assert response.status_code == 200
    assert 'href="/admin/docs"' in response.text
    assert 'data-admin-area="true"' in response.text


def test_the_screen_says_how_much_is_stored(client, fake_doc_pages):
    """
    Intent: The first question anyone asks is whether the corpus is populated and how fresh
        it is. Without totals the screen cannot answer either.
    Success: Page count, size and last-fetched time are rendered.
    Feature: Documentation corpus — the screen reports what is stored.
    """
    seed()
    body = client.get(PAGE).text
    assert 'data-total-pages="true"' in body
    assert 'data-total-bytes="true"' in body
    assert 'data-total-newest="true"' in body


def test_the_screen_offers_one_refresh_control(client, fake_doc_pages):
    """
    Intent: The refresh is ad-hoc — nothing schedules it — and it is a single action: replace
        the corpus with what is published now. Offering a choice of scope, or per-source
        delete buttons, made the screen a control panel for decisions nobody needed to make.
        Replaces a test that required two buttons.
    Success: The page offers one refresh button and no selection or delete controls.
    Feature: Documentation corpus — one ad-hoc refresh.
    """
    body = client.get(PAGE).text
    assert 'id="refresh-btn"' in body
    assert 'id="refresh-selected-btn"' not in body
    assert "js-pick" not in body and "js-drop" not in body


def test_a_refresh_in_progress_is_picked_up_on_load(client, fake_doc_pages):
    """
    Intent: A crawl outlives the page — an operator reloading, or opening a second tab, must
        see it is running rather than starting a second one.
    Success: With a refresh running, the page starts polling on load.
    Feature: Documentation corpus — run state survives a page load.
    """
    api_module._run_state["running"] = True
    body = client.get(PAGE).text
    assert "if (true) { pollStatus(); }" in body
    assert "adoptServerClock(state)" in body


def test_the_last_refresh_result_is_reported(client, fake_doc_pages):
    """
    Intent: After the page reloads the run alert is gone, so the page itself must say what
        the crawl did — otherwise a completed refresh looks like it did nothing.
    Success: The counts and the failure summary are rendered.
    Feature: Documentation corpus — the last refresh is reported.
    """
    api_module._run_state["last_result"] = {
        "source": "docs-refresh", "sources_done": 3, "inserted": 120,
        "updated": 4, "unchanged": 900, "removed": 9, "failure_count": 2,
        "failures": [{"url": "https://x/a.md", "error": "timeout"}],
    }
    body = client.get(PAGE).text
    assert "120 new" in body
    assert "9 removed" in body
    assert 'data-refresh-failures="true"' in body
    assert "timeout" in body


def test_a_failed_refresh_is_explained_on_the_screen(client, fake_doc_pages):
    """
    Intent: A background failure has nowhere else to surface, and without the message an
        operator has to read server logs to learn the index was unreachable.
    Success: A danger alert carries the error and the trace.
    Feature: Documentation corpus — failures are explained on screen.
    """
    api_module._run_state.update(
        last_error="index unreachable", last_traceback="Traceback: RuntimeError"
    )
    body = client.get(PAGE).text
    assert 'class="alert alert-danger"' in body
    assert "index unreachable" in body
    assert "<details" in body


def test_an_unreachable_database_is_explained_rather_than_crashing(
    client, monkeypatch, fake_doc_pages
):
    """
    Intent: A wrong connection string is the likeliest setup mistake, and a stack trace
        tells an operator nothing about which variable to fix.
    Success: The page still returns 200 and names the failure.
    Feature: Documentation corpus — storage failures are explained.
    """
    def explode():
        raise ServerSelectionTimeoutError("no route to host")

    monkeypatch.setattr(doc_pages, "totals", explode)
    response = client.get(PAGE)
    assert response.status_code == 200
    assert "no route to host" in response.text


def test_the_docs_api_is_mounted(client):
    """
    Intent: A router written but never included fails only in production, on a screen whose
        buttons then do nothing.
    Success: The refresh and sources endpoints appear in the served schema.
    Feature: Application wiring — documentation corpus API is reachable.
    """
    paths = client.get("/openapi.json").json()["paths"]
    assert API + "/refresh" in paths
    assert API + "/sources" in paths


# --- drilling in: source -> page -> rendered markdown ---


def test_a_source_can_be_opened_to_list_its_pages(client, fake_doc_pages):
    """
    Intent: The corpus screen showed only which indexes existed, which told a reader nothing
        about what was actually captured. Being able to open a source is the step from "we
        store this" to "here is what is in it".
    Success: The source screen lists the stored page, linking to the page view.
    Feature: Documentation corpus — drill into a source.
    """
    seed(url="https://www.mongodb.com/docs/a.md", source="ix-1")
    response = client.get("/admin/docs/source", params={"source": "ix-1"})
    assert response.status_code == 200
    assert 'data-doc-page="true"' in response.text
    assert "/admin/docs/page?url=" in response.text


def test_the_corpus_screen_links_into_a_stored_source(client, fake_doc_pages):
    """
    Intent: A drill-down nobody can find is not a feature. The route in has to be the source
        row itself, which is what a reader would click.
    Success: The corpus screen's script builds links to the source view.
    Feature: Documentation corpus — the drill-down is reachable.
    """
    body = client.get(PAGE).text
    assert "/admin/docs/source?source=" in body
    assert "drillIn" in body


def test_a_source_listing_says_how_much_it_is_showing(client, fake_doc_pages):
    """
    Intent: The listing is capped at 500 while a source can hold nearly a thousand pages.
        Showing a truncated list without saying so would read as the whole source.
    Success: The screen reports how many of the total are shown.
    Feature: Documentation corpus — the listing is honest about truncation.
    """
    seed(url="https://www.mongodb.com/docs/a.md", source="ix-1")
    body = client.get("/admin/docs/source", params={"source": "ix-1"}).text
    assert 'data-page-count="true"' in body
    assert "Showing 1 of 1 page(s)" in body


def test_pages_in_a_source_can_be_filtered(client, fake_doc_pages):
    """
    Intent: With hundreds of pages per source, a filter is the only practical way to reach
        one. Without it the cap makes later pages unreachable entirely.
    Success: A filter narrows the list to the matching page.
    Feature: Documentation corpus — find a page within a source.
    """
    seed(url="https://www.mongodb.com/docs/aggregation.md", source="ix-1")
    seed(url="https://www.mongodb.com/docs/indexes.md", source="ix-1")
    body = client.get("/admin/docs/source", params={"source": "ix-1", "q": "aggregation"}).text
    assert "aggregation.md" in body
    assert "indexes.md" not in body


def test_a_filter_with_no_match_is_distinguished_from_an_empty_source(client, fake_doc_pages):
    """
    Intent: "Nothing matches your filter" and "this source has no pages" call for different
        actions — one is a typo, the other means the corpus needs refreshing.
    Success: The empty state names the filter when one is applied.
    Feature: Documentation corpus — distinct empty states.
    """
    seed(url="https://www.mongodb.com/docs/a.md", source="ix-1")
    body = client.get("/admin/docs/source", params={"source": "ix-1", "q": "zzz"}).text
    assert 'data-empty="true"' in body
    assert "zzz" in body


def test_a_stored_page_is_shown_as_rendered_markdown(client, fake_doc_pages):
    """
    Intent: Reading raw Markdown in a table cell is not reading documentation. The point of
        the viewer is that a stored page can be read as the page it came from.
    Success: The page view renders, offers both rendered and raw views, and loads a Markdown
        renderer.
    Feature: Documentation viewer — a stored page is readable.
    """
    seed(url="https://www.mongodb.com/docs/a.md", source="ix-1")
    response = client.get("/admin/docs/page", params={"url": "https://www.mongodb.com/docs/a.md"})
    assert response.status_code == 200
    assert 'data-rendered="true"' in response.text
    assert 'data-raw="true"' in response.text
    assert "marked.min.js" in response.text


def test_the_page_text_is_passed_as_data_not_markup(client, fake_doc_pages):
    """
    Intent: A documentation page is fetched content, and this program does not get to assume
        what is in it. Interpolating it into the document would let a page's own text be
        parsed as part of this application's HTML; passing it as JSON and sanitising before
        rendering cannot.
    Success: The text reaches the page as a JSON string and is sanitised before insertion.
    Feature: Documentation viewer — fetched content cannot alter the page.
    """
    doc_pages.upsert_pages([{
        "url": "https://www.mongodb.com/docs/x.md", "source": "ix-1", "title": "X",
        "text": "# Heading\n<script>window.__owned = true;</script>",
    }])
    body = client.get("/admin/docs/page",
                      params={"url": "https://www.mongodb.com/docs/x.md"}).text
    assert "DOMPurify.sanitize(marked.parse(MARKDOWN))" in body
    # The script tag from the document must appear only inside the JSON string literal,
    # escaped, never as a live tag in the markup.
    assert "<script>window.__owned" not in body


def test_the_viewer_links_back_to_the_source_and_the_corpus(client, fake_doc_pages):
    """
    Intent: A reader who drills three levels down needs a way back, or the only route out is
        the browser's back button and the screens read as dead ends.
    Success: The page view carries breadcrumbs to its source and to the corpus screen.
    Feature: Documentation viewer — navigable.
    """
    seed(url="https://www.mongodb.com/docs/a.md", source="ix-1")
    body = client.get("/admin/docs/page", params={"url": "https://www.mongodb.com/docs/a.md"}).text
    assert 'data-breadcrumb="true"' in body
    assert 'href="/admin/docs"' in body
    assert "/admin/docs/source?source=" in body


def test_the_viewer_links_to_the_page_on_mongodbs_site(client, fake_doc_pages):
    """
    Intent: The corpus can be stale, and a reader judging a question's grounding needs to
        compare against what is published now. Without the original URL there is no way
        back to it.
    Success: The stored page's own URL is linked.
    Feature: Documentation viewer — the original is one click away.
    """
    seed(url="https://www.mongodb.com/docs/a.md", source="ix-1")
    body = client.get("/admin/docs/page", params={"url": "https://www.mongodb.com/docs/a.md"}).text
    assert 'data-source-link="true"' in body
    assert 'href="https://www.mongodb.com/docs/a.md"' in body


def test_viewing_a_page_that_is_not_stored_is_a_404(client, fake_doc_pages):
    """
    Intent: A stale link or a hand-typed URL must say the page is not in the corpus, rather
        than rendering an empty document that reads as a page with no content.
    Success: An unstored URL returns 404.
    Feature: Documentation viewer — unknown pages are reported.
    """
    assert client.get("/admin/docs/page",
                      params={"url": "https://x/never-crawled.md"}).status_code == 404


def test_the_source_screen_survives_an_unreachable_database(client, monkeypatch, fake_doc_pages):
    """
    Intent: These screens are reached mid-investigation; a stack trace there tells the reader
        nothing about which variable to fix.
    Success: The source screen returns 200 and names the storage failure.
    Feature: Documentation corpus — storage failures are explained.
    """
    def explode(*args, **kwargs):
        raise ServerSelectionTimeoutError("no route to host")

    monkeypatch.setattr(doc_pages, "list_pages", explode)
    response = client.get("/admin/docs/source", params={"source": "ix-1"})
    assert response.status_code == 200
    assert "no route to host" in response.text


def test_the_page_viewer_survives_an_unreachable_database(client, monkeypatch, fake_doc_pages):
    """
    Intent: A storage failure must not be reported as "this page is not in the corpus" — that
        would send a reader off to re-crawl documentation they already have, over a
        connection problem.
    Success: The viewer returns 200 and names the storage failure instead of a 404.
    Feature: Documentation viewer — storage failures are explained, not mistaken for absence.
    """
    def explode(*args, **kwargs):
        raise ServerSelectionTimeoutError("no route to host")

    monkeypatch.setattr(doc_pages, "get_page", explode)
    response = client.get("/admin/docs/page", params={"url": "https://x/a.md"})
    assert response.status_code == 200
    assert "no route to host" in response.text


# --- the progress panel ---


def test_the_screen_has_a_progress_panel_with_the_useful_figures(client, fake_doc_pages):
    """
    Intent: A ten-minute crawl behind a spinner is indistinguishable from a stuck one. The
        panel has to answer how far through, how fast, how long so far and how much longer,
        or an operator cannot tell progress from a hang.
    Success: The page carries a progress bar and cells for pages, rate, elapsed, remaining,
        sources and stored counts.
    Feature: Documentation corpus — useful refresh status.
    """
    body = client.get(PAGE).text
    assert 'data-progress-panel="true"' in body
    assert 'data-progress-bar="true"' in body
    for stat in ("pages", "rate", "elapsed", "eta", "sources", "stored"):
        assert f'data-stat="{stat}"' in body


def test_the_panel_is_driven_by_the_servers_progress_snapshot(client, fake_doc_pages):
    """
    Intent: The figures must come from the crawl itself. Derived in the browser they would be
        wrong after a reload, and could not know the page total the planning pass discovered.
    Success: The page reads pages_total, pages_per_second, eta_seconds and percent from the
        polled state.
    Feature: Documentation corpus — progress figures come from the server.
    """
    body = client.get(PAGE).text
    for field in ("pages_total", "pages_seen", "pages_per_second", "eta_seconds",
                  "percent", "phase"):
        assert field in body


def test_the_panel_shows_no_percentage_until_the_total_is_known(client, fake_doc_pages):
    """
    Intent: During planning the crawl genuinely does not know how much work there is. A
        rendered "0%" would read as a stalled run, so the page must handle an absent
        percentage rather than formatting a null.
    Success: The page guards on the percentage being present before showing one.
    Feature: Documentation corpus — honest progress display.
    """
    body = client.get(PAGE).text
    assert "p.percent !== null" in body
    assert "indexes read" in body


# --- corpus-wide search ---


def seed_content(title: str, body: str, url: str, source: str = "ix-1") -> None:
    doc_pages.upsert_pages([{"url": url, "source": source, "title": title, "text": body}])


def test_the_search_endpoint_returns_ranked_results_with_excerpts(client, fake_doc_pages):
    """
    Intent: Finding source material for a question means searching the whole corpus by
        keyword; a result without an excerpt cannot be judged without opening it.
    Success: The endpoint returns the matching page with a score and an excerpt.
    Feature: Documentation search — the API answers keyword queries.
    """
    seed_content("Pipelines", "The $lookup stage joins another collection.", "https://x/a.md")
    body = client.get(API + "/search", params={"q": "lookup"}).json()
    assert body and body[0]["url"] == "https://x/a.md"
    assert "lookup" in body[0]["excerpt"]
    assert body[0]["score"] > 0


@pytest.mark.parametrize(
    "params", [{"q": "a"}, {"q": ""}, {"q": "ok", "limit": 0}, {"q": "ok", "limit": 5000}],
    ids=["too-short", "empty", "no-limit", "limit-too-high"],
)
def test_a_malformed_search_is_refused(client, fake_doc_pages, params):
    """
    Intent: A one-character query matches most of a 7,000-page corpus, and an unbounded
        limit would return all of it with an excerpt each. Both are caught at the boundary.
    Success: Each malformed search is a validation error.
    Feature: Documentation search — bounded request.
    """
    assert client.get(API + "/search", params=params).status_code == 422


def test_stubs_can_be_pruned_without_a_full_crawl(client, fake_doc_pages):
    """
    Intent: A refresh drops stubs by sweeping what it did not write, but that means waiting
        twelve minutes before the listings stop being cluttered by pages nothing can be
        written from.
    Success: The prune endpoint deletes pages under the floor and reports the count and the
        floor it used.
    Feature: Documentation corpus — existing stubs can be pruned on demand.
    """
    seed_content("Stub", "x" * 50, "https://x/stub.md")
    seed_content("Real", "y" * 5000, "https://x/real.md")
    body = client.post(API + "/prune-stubs").json()
    assert body == {"deleted": 1, "smaller_than": 500}
    assert [p["url"] for p in doc_pages.list_pages()] == ["https://x/real.md"]


# --- the search screen ---


def test_the_search_screen_finds_pages_across_every_source(client, fake_doc_pages):
    """
    Intent: The corpus looked useless because its content was reachable only by guessing
        which of 74 sources held it — the C# driver's real documentation is not under the
        drivers index. Searching everything is what makes the corpus usable for writing
        questions.
    Success: A page in one source is found by a keyword search that names no source, and
        links to the viewer.
    Feature: Documentation search — corpus-wide, from the screen.
    """
    seed_content("LINQ", "The aggregation pipeline is built from stages.", "https://x/a.md",
                 source="ix-csharp")
    response = client.get("/admin/docs/search", params={"q": "aggregation pipeline"})
    assert response.status_code == 200
    assert 'data-search-result="true"' in response.text
    assert "/admin/docs/page?url=" in response.text
    assert 'data-excerpt="true"' in response.text


def test_the_corpus_screen_offers_the_search(client, fake_doc_pages):
    """
    Intent: A search nobody can find does not fix findability. The route in belongs on the
        screen where someone has just discovered a source is a hub of links.
    Success: The corpus screen carries a search form pointing at the search screen.
    Feature: Documentation search — reachable from the corpus screen.
    """
    body = client.get(PAGE).text
    assert 'data-corpus-search="true"' in body
    assert 'action="/admin/docs/search"' in body


def test_the_search_screen_reports_how_many_results_it_found(client, fake_doc_pages):
    """
    Intent: Results are capped, so a reader needs to know whether they are seeing everything
        or the top of a broad match they should narrow.
    Success: The count is shown for a query.
    Feature: Documentation search — honest result count.
    """
    seed_content("Pipelines", "The $lookup stage joins.", "https://x/a.md")
    body = client.get("/admin/docs/search", params={"q": "lookup"}).text
    assert 'data-result-count="true"' in body
    assert "1 result(s)" in body


def test_a_search_with_no_match_says_so(client, fake_doc_pages):
    """
    Intent: "The corpus has nothing on this" is a useful answer — it tells an author the
        material for a question is not there — but only if it is distinguishable from a
        broken search.
    Success: A query with no match names the query in the empty state.
    Feature: Documentation search — distinct empty state.
    """
    body = client.get("/admin/docs/search", params={"q": "quantum tunnelling"}).text
    assert 'data-empty="true"' in body
    assert "quantum tunnelling" in body


def test_the_search_screen_prompts_before_a_query_is_entered(client, fake_doc_pages):
    """
    Intent: Arriving at the screen with no query must not look like a search that found
        nothing, which would read as an empty corpus.
    Success: With no query the screen prompts rather than reporting no results.
    Feature: Documentation search — a bare screen invites a query.
    """
    body = client.get("/admin/docs/search").text
    assert "Enter a search above" in body
    assert "result(s)" not in body


def test_a_missing_text_index_is_explained_rather_than_crashing(client, monkeypatch, fake_doc_pages):
    """
    Intent: A corpus stored before search existed has no text index, so the query fails. That
        is a fixable state — refresh once — and saying so is more useful than a stack trace
        or an empty result that reads as "we have nothing on that".
    Success: The screen returns 200 and explains, naming the failure.
    Feature: Documentation search — a missing index is explained.
    """
    from pymongo.errors import OperationFailure

    def explode(*args, **kwargs):
        raise OperationFailure("text index required for $text query")

    monkeypatch.setattr(doc_pages, "search_pages", explode)
    response = client.get("/admin/docs/search", params={"q": "anything"})
    assert response.status_code == 200
    assert 'data-search-error="true"' in response.text
    assert "text index required" in response.text


# --- recovering a partial load ---


def test_fill_mode_is_requested_as_its_own_mode(client, monkeypatch, fake_doc_pages):
    """
    Intent: The mode decides whether the run may delete pages. If the request dropped it,
        pressing Fill gaps would start a full replace — the slow, block-provoking thing the
        button exists to avoid.
    Success: mode=fill reaches the crawl as fill.
    Feature: Documentation corpus — a resume is requested explicitly.
    """
    calls: list = []
    monkeypatch.setattr(api_module.doc_corpus, "refresh",
                        lambda **kw: calls.append(kw) or {})
    response = client.post(API + "/refresh", params={"mode": "fill"})
    assert response.json() == {"started": True, "mode": "fill"}
    assert calls[0]["mode"] == "fill"


def test_an_unrecognised_mode_is_refused(client, fake_doc_pages):
    """
    Intent: A typo must not fall through to the mode that deletes pages. Rejecting it at the
        boundary means no background task starts at all.
    Success: An unknown mode is a validation error.
    Feature: Documentation corpus — the mode is validated.
    """
    assert client.post(API + "/refresh", params={"mode": "wipe"}).status_code == 422


def test_the_screen_offers_the_recovery_alongside_the_full_refresh(client, fake_doc_pages):
    """
    Intent: A crawl refused part way through leaves the corpus incomplete, and the only
        remedy available was a twelve-minute full re-crawl that invites another refusal. The
        recovery has to be a control on the screen, next to the thing that failed.
    Success: The screen offers both a fill button and the full refresh.
    Feature: Documentation corpus — recovery is one click.
    """
    body = client.get(PAGE).text
    assert 'id="fill-btn"' in body
    assert 'id="refresh-btn"' in body
    assert "mode=" in body


def test_a_blocked_run_is_explained_on_the_screen(client, fake_doc_pages):
    """
    Intent: A crawl that stopped because it was refused looks, in the numbers alone, like a
        crawl that found very little. The reader needs to be told it was turned away, and
        what to do about it.
    Success: The blocked reason is rendered.
    Feature: Documentation corpus — a refused crawl says so.
    """
    api_module._run_state["last_result"] = {
        "source": "docs-refresh", "mode": "replace", "sources_done": 3,
        "inserted": 400, "updated": 0, "unchanged": 0, "removed": 0,
        "skipped_stubs": 0, "failure_count": 25, "failures": [],
        "blocked": True,
        "block_reason": "25 consecutive requests were refused, so the crawl stopped.",
    }
    body = client.get(PAGE).text
    assert 'data-blocked="true"' in body
    assert "consecutive requests were refused" in body


def test_a_fill_run_reports_what_it_left_alone(client, fake_doc_pages):
    """
    Intent: A resume that fetched 200 pages out of 7,000 planned looks like a failure unless
        the screen says the other 6,800 were already present and deliberately skipped.
    Success: The fill summary reports the already-present count.
    Feature: Documentation corpus — a resume explains its small numbers.
    """
    api_module._run_state["last_result"] = {
        "source": "docs-refresh", "mode": "fill", "sources_done": 74,
        "inserted": 200, "updated": 0, "unchanged": 0, "removed": 0,
        "skipped_stubs": 0, "failure_count": 0, "failures": [],
        "already_present": 6800, "blocked": False,
    }
    body = client.get(PAGE).text
    assert 'data-fill-summary="true"' in body
    assert "6800 page(s) already present" in body
