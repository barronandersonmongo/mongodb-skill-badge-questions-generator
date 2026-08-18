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
from typing import Any, Callable, Iterable
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


def _get(url: str, settings: Settings) -> str:
    response = httpx.get(
        url, timeout=settings.docs_request_timeout, follow_redirects=True
    )
    response.raise_for_status()
    return response.text


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
    on_page: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[str]]:
    """Fetch pages concurrently. Returns the pages, the failures, and the stubs skipped.

    One unreachable page must not lose the thousands that fetched cleanly, so
    failures are collected and reported rather than raised. Stubs are returned
    separately: skipping a navigation page is the intended outcome, not a failure, and
    counting it as one would make the failure figure meaningless.
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

    with ThreadPoolExecutor(max_workers=settings.docs_fetch_concurrency) as pool:
        for url, text, error in pool.map(fetch, urls):
            if on_page:
                on_page()
            if error:
                # Logged at ERROR the moment it happens: a run summary that only counts
                # failures tells a reader that something broke but not what, and the
                # whole point of tailing the log during a crawl is to see which pages
                # are failing while it is still running.
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
    settings: Settings | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Replace the stored corpus with a fresh crawl of the whole documentation set.

    Around 10,000 pages. Pages are stamped with this run and anything left from an
    earlier one is deleted at the end, so the corpus ends up as exactly what the
    published index currently names — without a window in which it is empty.

    Progress is reported continuously: which phase, how many pages of how many, the
    rate, and the time left at that rate. A crawl this long is unreadable without it.
    """
    settings = settings or get_settings()
    run_id = uuid4().hex
    started = time.monotonic()

    summary: dict[str, Any] = {
        "source": "docs-refresh",
        "run_id": run_id,
        "phase": "planning",
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
    summary["pages_total"] = len(plan)
    summary["phase"] = "fetching"
    report()

    # Grouped by source so each write batch belongs to one source, which keeps the
    # per-source figures meaningful and the stored `source` field correct.
    by_source: dict[str, list[str]] = {}
    for index_url, url in plan:
        by_source.setdefault(index_url, []).append(url)

    for index_url, urls in by_source.items():
        stats = {"source": index_url, "pages": len(urls), "inserted": 0,
                 "updated": 0, "unchanged": 0, "skipped_stubs": 0, "failed": 0}

        for start in range(0, len(urls), WRITE_BATCH):
            batch = urls[start : start + WRITE_BATCH]
            pages, failures, stubs = fetch_pages(
                batch, index_url, settings=settings,
                on_page=lambda: (
                    summary.__setitem__("pages_seen", summary["pages_seen"] + 1),
                    report(),
                ),
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
    if stored_anything:
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
