"""Fetch MongoDB's documentation and keep a copy in the database.

Why a local copy at all: question authoring currently searches and fetches the web
mid-run, which is where most of a run's wall-clock time goes, and it makes two runs
on the same badge read different source text. Reading from a stored corpus removes
the waiting and makes a run repeatable.

Why a crawl rather than the MCP server's `search-knowledge`: that tool answers a
query with its best few chunks. It is the right tool at authoring time, but it
cannot be asked for everything, so it cannot build a corpus. MongoDB publishes an
agent-oriented index (`llms.txt`) that names its documentation pages, and serves
each page as Markdown — that is the enumerable route.

The crawl is concurrent but bounded, and every request is against a site this
program does not own: failures are collected per page rather than aborting the run.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from app.config import Settings, get_settings
from app.repositories import doc_pages

logger = logging.getLogger(__name__)

# Links in an llms.txt index are Markdown links; only .md pages and nested indexes
# matter to the crawl.
LINK_PATTERN = re.compile(r"\((https://[^)\s]+)\)")
# The first Markdown heading of a page, used as its title when it has one.
TITLE_PATTERN = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)

# A page far larger than this is a generated API dump rather than prose, and would
# crowd out the documentation that a question can actually be written from.
MAX_PAGE_BYTES = 2 * 1024 * 1024


# Statuses worth another attempt. 403 is included because CloudFront answers with it
# when refusing a client for asking too fast — not because the page is forbidden — and a
# pause usually clears it.
RETRYABLE_STATUS = frozenset({403, 408, 425, 429, 500, 502, 503, 504})
# The statuses that mean "you are being refused", as opposed to "this page is broken".
BLOCKING_STATUS = frozenset({403, 429, 503})


def _get(url: str, settings: Settings) -> str:
    """Fetch a page, retrying a refusal with a growing pause.

    `Retry-After` is honoured when the server sends one: it is the server saying how
    long it wants to be left alone, and ignoring it is how a short block becomes a long
    one.
    """
    attempts = max(1, settings.docs_retry_attempts)
    response = None
    for attempt in range(1, attempts + 1):
        response = httpx.get(
            url, timeout=settings.docs_request_timeout, follow_redirects=True
        )
        if response.status_code not in RETRYABLE_STATUS or attempt == attempts:
            break
        time.sleep(_retry_pause(response, attempt, settings))
    response.raise_for_status()
    return response.text


def _retry_pause(response: httpx.Response, attempt: int, settings: Settings) -> float:
    """How long to wait before trying again.

    A `Retry-After` header wins: it is the server saying how long it wants to be left
    alone, and ignoring it is how a short block becomes a long one. A header we cannot
    parse is treated as absent rather than as zero, which would mean no pause at all.
    """
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    return settings.docs_retry_backoff_seconds * attempt


def is_blocked(error: str) -> bool:
    """Does this failure mean the crawl is being refused rather than the page missing?"""
    return any(f"'{status} " in error or f" {status} " in error for status in BLOCKING_STATUS)


def index_links(text: str) -> tuple[list[str], list[str]]:
    """Split an llms.txt index into the pages it names and the indexes it points to."""
    pages, indexes = [], []
    for url in dict.fromkeys(LINK_PATTERN.findall(text)):
        if url.endswith(".md"):
            pages.append(url)
        elif url.endswith("llms.txt"):
            indexes.append(url)
    return pages, indexes


def discover_sources(*, settings: Settings | None = None) -> list[str]:
    """Every documentation index the root index names.

    Listed on the screen so it is visible what the corpus is made of, but the crawl
    always takes all of them: one refresh, the whole corpus.
    """
    settings = settings or get_settings()
    root = _get(settings.docs_index_url, settings)
    _, indexes = index_links(root)
    # The root itself names a handful of pages, so it is a source in its own right.
    return [settings.docs_index_url, *indexes]


def source_pages(index_url: str, *, settings: Settings | None = None) -> list[str]:
    """Every documentation page one index names."""
    settings = settings or get_settings()
    pages, _ = index_links(_get(index_url, settings))
    return pages


def is_docs_url(url: str, settings: Settings | None = None) -> bool:
    """Whether this URL is a MongoDB documentation page this program may fetch.

    The rendered-source view fetches a URL server-side on a visitor's request, so the
    host has to be pinned. Left open it is a server-side request forgery hole: a crafted
    URL would reach anything the server can reach — an internal service, a cloud
    metadata endpoint — and return the response to whoever asked.

    Checked on the parsed host rather than with a prefix match, because
    `https://www.mongodb.com.evil.example/` starts with the right string and is not the
    right host.
    """
    settings = settings or get_settings()
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == settings.docs_domain


def fetch_live_page(url: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """Fetch one documentation page from MongoDB now, for rendering.

    Separate from the crawl: no stub filtering, no storing, no run bookkeeping. This is
    the canonical page as it stands this minute, which is the point — the stored copy is
    a snapshot and can have drifted since a question was written from it.

    Raises ValueError for a URL outside the documentation host, so a caller cannot
    forget the check.
    """
    settings = settings or get_settings()
    if not is_docs_url(url, settings):
        raise ValueError(
            f"Refusing to fetch {url!r}: only https pages on "
            f"{settings.docs_domain} can be rendered."
        )
    text = _get(url, settings)
    return {
        "url": url,
        "title": page_title(text, url),
        "text": text,
        "bytes": len(text.encode("utf-8")),
        "fetched_at": datetime.now(timezone.utc),
    }


def page_title(text: str, url: str) -> str:
    """A page's own heading, falling back to its filename."""
    found = TITLE_PATTERN.search(text)
    if found:
        return found.group(1).strip()
    return url.rsplit("/", 1)[-1].removesuffix(".md")


def is_stub(text: str, settings: Settings) -> bool:
    """Is this a navigation page rather than documentation?

    A stub is a title and a list of links to the real pages — nothing a question can be
    written from. Judged on size because that is what separates them cleanly in the
    published corpus, and because judging on link density would also discard genuine
    reference pages that are mostly tables of links.
    """
    return len(text.encode("utf-8")) < settings.docs_min_page_bytes


def fetch_pages(
    urls: Iterable[str],
    source: str,
    *,
    settings: Settings | None = None,
    on_page: Callable[[str | None], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Fetch pages concurrently. Returns the pages, the failures, and the stubs skipped.

    One unreachable page must not lose the thousands that fetched cleanly, so
    failures are collected and reported rather than raised. Stubs are returned
    separately: skipping a navigation page is the intended outcome, not a failure, and
    counting it as one would make the failure figure meaningless.

    `on_page` is called for each result with the error text, or None on success, so the
    caller can count progress and notice a run of refusals. `stop` is consulted between
    chunks: work is submitted a chunk at a time rather than all at once specifically so a
    crawl that is being refused can be abandoned within a few requests instead of after
    every remaining page has been attempted.
    """
    settings = settings or get_settings()
    urls = list(urls)
    pages: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    stubs: list[str] = []

    def fetch(url: str):
        try:
            text = _get(url, settings)
        except Exception as exc:
            return url, None, f"{type(exc).__name__}: {exc}"
        if len(text.encode("utf-8")) > MAX_PAGE_BYTES:
            return url, None, f"skipped: {len(text)} bytes exceeds the page limit"
        return url, text, None

    chunk_size = max(1, settings.docs_fetch_concurrency)
    with ThreadPoolExecutor(max_workers=chunk_size) as pool:
        for start in range(0, len(urls), chunk_size):
            if stop and stop():
                break
            for url, text, error in pool.map(fetch, urls[start : start + chunk_size]):
                if on_page:
                    on_page(error)
                if error:
                    # Logged at ERROR the moment it happens: a run summary that only
                    # counts failures tells a reader that something broke but not what,
                    # and the whole point of tailing the log during a crawl is to see
                    # which pages are failing while it is still running.
                    logger.error("Docs page failed: %s — %s", url, error)
                    failures.append({"url": url, "error": error})
                    continue
                if is_stub(text, settings):
                    stubs.append(url)
                    continue
                pages.append(
                    {
                        "url": url,
                        "source": source,
                        "title": page_title(text, url),
                        "text": text,
                    }
                )
    return pages, failures, stubs


# Pages are written in batches rather than all at once, so a long crawl makes
# visible progress and a failure part-way through does not discard everything
# fetched before it.
WRITE_BATCH = 200


def plan_pages(
    sources: list[str],
    *,
    settings: Settings | None = None,
    on_source: Callable[[int, int], None] | None = None,
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    """Read every index first, so the crawl knows how much work it has.

    Without this the progress report can only count what it has already done, which
    cannot express "how far through" or "how much longer" — the two things a person
    watching a ten-minute crawl actually wants. Reading the indexes costs one request
    each, against thousands for the pages themselves.
    """
    settings = settings or get_settings()
    plan: list[tuple[str, str]] = []
    failures: list[dict[str, str]] = []
    seen: set[str] = set()

    def read(index_url: str):
        try:
            return index_url, source_pages(index_url, settings=settings), None
        except Exception as exc:
            return index_url, [], f"index unreadable: {exc}"

    def record(index_url: str, error: str) -> None:
        logger.error("Docs index failed: %s — %s", index_url, error)

    with ThreadPoolExecutor(max_workers=settings.docs_fetch_concurrency) as pool:
        for done, (index_url, urls, error) in enumerate(pool.map(read, sources), 1):
            if error:
                record(index_url, error)
                failures.append({"url": index_url, "error": error})
            for url in urls:
                # The same page is named by more than one index; fetching it twice
                # would inflate both the cost and the totals shown on screen.
                if url in seen:
                    continue
                seen.add(url)
                plan.append((index_url, url))
            if on_source:
                on_source(done, len(sources))
    return plan, failures


def refresh(
    *,
    mode: str = "replace",
    settings: Settings | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Crawl the documentation. Two modes, because a blocked crawl needs recovering.

    `replace` — the full crawl. Pages are stamped with this run and anything left from an
    earlier one is deleted at the end, so the corpus ends up as exactly what the
    published index currently names, without a window in which it is empty.

    `fill` — fetch only pages the corpus does not already have, and never sweep. This is
    how a partial load is recovered: the docs are served through CloudFront, which starts
    refusing requests when a crawl asks for too much, and re-fetching seven thousand
    pages to recover the few hundred that were refused wastes an hour and invites another
    block.

    Progress is reported continuously: which phase, how many pages of how many, the
    rate, and the time left at that rate. A crawl this long is unreadable without it.
    """
    settings = settings or get_settings()
    if mode not in ("replace", "fill"):
        raise ValueError(f"Unknown refresh mode {mode!r}; expected 'replace' or 'fill'.")

    run_id = uuid4().hex
    started = time.monotonic()

    summary: dict[str, Any] = {
        "source": "docs-refresh",
        "run_id": run_id,
        "mode": mode,
        "phase": "planning",
        "already_present": 0,
        "blocked": False,
        "sources_total": 0,
        "sources_done": 0,
        "pages_total": 0,
        "pages_seen": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_stubs": 0,
        "failed": 0,
        "removed": 0,
        "sweep_skipped": None,
        "failures": [],
        "per_source": [],
    }

    def snapshot() -> dict[str, Any]:
        """Add the derived numbers a watcher needs: rate, share done, time left."""
        elapsed = time.monotonic() - started
        done = summary["pages_seen"]
        total = summary["pages_total"]
        rate = done / elapsed if elapsed > 0 and done else 0.0
        state = dict(summary)
        state["elapsed_seconds"] = round(elapsed, 1)
        state["pages_per_second"] = round(rate, 2)
        # Only claim a percentage and an estimate once the total is known and work has
        # actually started — a confident "0%, 0 seconds left" is worse than no estimate.
        state["percent"] = round(done / total * 100, 1) if total else None
        remaining = max(total - done, 0)
        state["eta_seconds"] = round(remaining / rate) if rate and total else None
        return state

    def report():
        if progress:
            progress(snapshot())

    report()

    try:
        sources = discover_sources(settings=settings)
    except Exception as exc:
        # Without the root index there is nothing to crawl, and sweeping now would
        # delete a corpus this run cannot replace.
        summary["phase"] = "failed"
        summary["failures"].append(
            {"url": settings.docs_index_url, "error": f"index unreadable: {exc}"}
        )
        summary["failure_count"] = 1
        logger.exception("Docs refresh could not read the root index: %s", exc)
        report()
        return summary

    summary["sources_total"] = len(sources)
    report()

    def planned(done: int, total: int) -> None:
        summary["sources_done"] = done
        report()

    plan, plan_failures = plan_pages(sources, settings=settings, on_source=planned)
    summary["failures"].extend(plan_failures)
    summary["failed"] += len(plan_failures)

    if mode == "fill":
        # Only what is missing. Deliberately not "and anything stale": a resume exists to
        # finish an interrupted crawl, and re-fetching pages that are already here is the
        # cost it is meant to avoid.
        present = doc_pages.stored_urls()
        before = len(plan)
        plan = [(source, url) for source, url in plan if url not in present]
        summary["already_present"] = before - len(plan)

    summary["pages_total"] = len(plan)
    summary["phase"] = "fetching"
    report()

    # Grouped by source so each write batch belongs to one source, which keeps the
    # per-source figures meaningful and the stored `source` field correct.
    by_source: dict[str, list[str]] = {}
    for index_url, url in plan:
        by_source.setdefault(index_url, []).append(url)

    # Consecutive refusals mean the crawl is being turned away, not that it has found bad
    # links. Carrying on would produce thousands of identical failures and prolong the
    # block, so the run stops and keeps what it has.
    consecutive_refusals = 0

    def note_result(error: str | None) -> None:
        """Count each result, and notice when the crawl is being turned away."""
        nonlocal consecutive_refusals
        summary["pages_seen"] += 1
        if error is None:
            consecutive_refusals = 0
        elif is_blocked(error):
            consecutive_refusals += 1
            if (
                consecutive_refusals >= settings.docs_block_threshold
                and not summary["blocked"]
            ):
                summary["blocked"] = True
                summary["block_reason"] = (
                    f"{consecutive_refusals} consecutive requests were refused, so the "
                    "crawl stopped rather than prolonging the block. Everything fetched "
                    "so far is stored — use Fill gaps later to finish."
                )
                logger.error("Docs refresh stopped: %s", summary["block_reason"])
        report()

    for index_url, urls in by_source.items():
        if summary["blocked"]:
            break
        stats = {"source": index_url, "pages": len(urls), "inserted": 0,
                 "updated": 0, "unchanged": 0, "skipped_stubs": 0, "failed": 0}

        for start in range(0, len(urls), WRITE_BATCH):
            if summary["blocked"]:
                break
            batch = urls[start : start + WRITE_BATCH]
            pages, failures, stubs = fetch_pages(
                batch,
                index_url,
                settings=settings,
                on_page=note_result,
                stop=lambda: summary["blocked"],
            )
            written = doc_pages.upsert_pages(pages, run_id)
            for key in ("inserted", "updated", "unchanged"):
                summary[key] += written[key]
                stats[key] += written[key]
            stats["failed"] += len(failures)
            summary["failed"] += len(failures)
            stats["skipped_stubs"] += len(stubs)
            summary["skipped_stubs"] += len(stubs)
            summary["failures"].extend(failures)
            report()

        summary["per_source"].append(stats)
        source_line = (
            "Docs refresh: %s — %d page(s), %d new, %d updated, %d unchanged, "
            "%d nav stub(s) skipped, %d failed"
        )
        source_figures = (
            index_url, stats["pages"], stats["inserted"], stats["updated"],
            stats["unchanged"], stats["skipped_stubs"], stats["failed"],
        )
        if stats["failed"]:
            logger.error(source_line, *source_figures)
        else:
            logger.info(source_line, *source_figures)
        report()

    # Only sweep once something was actually stored. A crawl that fetched nothing —
    # no network, a moved index — would otherwise delete the entire corpus and
    # report it as a successful replacement.
    summary["phase"] = "sweeping"
    report()
    stored_anything = summary["inserted"] + summary["updated"] + summary["unchanged"]
    if mode == "fill":
        # A resume knows nothing about the pages it deliberately skipped, so sweeping
        # would delete most of the corpus.
        summary["sweep_skipped"] = "fill mode only adds missing pages; nothing is removed"
    elif summary["blocked"]:
        # A refused crawl saw only part of the corpus. Sweeping on that basis would
        # delete every page it never got to.
        summary["sweep_skipped"] = (
            "the crawl was refused part way through, so pages it never reached were "
            "left in place rather than treated as withdrawn"
        )
    elif stored_anything:
        summary["removed"] = doc_pages.delete_not_in_run(run_id)
    else:
        # Reported on its own rather than as a failure: the crawl may have worked
        # perfectly and simply found nothing storable, and counting this as a failure
        # would make the failure figure — the thing a reader scans for — untrustworthy.
        summary["sweep_skipped"] = (
            "no pages were stored, so nothing was removed and the existing corpus was "
            "left untouched"
        )
        logger.error("Docs refresh stored no pages; corpus left untouched")

    # The whole list would be thousands of entries on a bad network; the count is
    # what matters on screen and the first few are what a person acts on.
    summary["failure_count"] = summary["failed"]
    summary["failures"] = summary["failures"][:50]
    summary["phase"] = "done"
    summary["elapsed_seconds"] = round(time.monotonic() - started, 1)
    report()

    finished = (
        "Docs refresh finished in %.1fs: %d source(s), %d of %d page(s) fetched, "
        "%d new, %d updated, %d unchanged, %d nav stub(s) skipped, %d removed, %d failed"
    )
    figures = (
        summary["elapsed_seconds"], summary["sources_total"], summary["pages_seen"],
        summary["pages_total"], summary["inserted"], summary["updated"],
        summary["unchanged"], summary["skipped_stubs"], summary["removed"],
        summary["failure_count"],
    )
    # A run with failures is reported at ERROR so it is findable by level, rather than
    # sitting among thousands of INFO lines with one number quietly non-zero.
    if summary["failure_count"]:
        logger.error(finished, *figures)
    else:
        logger.info(finished, *figures)
    return summary
