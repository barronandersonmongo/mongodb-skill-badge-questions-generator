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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

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
    """The documentation indexes available, from the root index.

    Returned rather than crawled immediately so the admin screen can offer them
    individually: most of the corpus is driver and CLI reference that a skill badge
    quiz never draws on, and refreshing only what matters is far cheaper.
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


def fetch_pages(
    urls: Iterable[str],
    source: str,
    *,
    settings: Settings | None = None,
    on_page: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Fetch pages concurrently. Returns the pages and the per-page failures.

    One unreachable page must not lose the thousands that fetched cleanly, so
    failures are collected and reported rather than raised.
    """
    settings = settings or get_settings()
    urls = list(urls)
    pages: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

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
                failures.append({"url": url, "error": error})
                continue
            pages.append(
                {
                    "url": url,
                    "source": source,
                    "title": page_title(text, url),
                    "text": text,
                }
            )
    return pages, failures


# Pages are written in batches rather than all at once, so a long crawl makes
# visible progress and a failure part-way through does not discard everything
# fetched before it.
WRITE_BATCH = 200


def refresh(
    sources: list[str] | None = None,
    *,
    settings: Settings | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Crawl the documentation and store it. Returns a summary for the admin screen.

    With no `sources`, every index the root names is crawled — the whole corpus,
    around 10,000 pages. Naming sources refreshes only those.
    """
    settings = settings or get_settings()
    targets = sources or discover_sources(settings=settings)

    summary: dict[str, Any] = {
        "source": "docs-refresh",
        "sources_requested": len(targets),
        "sources_done": 0,
        "pages_seen": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "failures": [],
        "per_source": [],
    }

    def report():
        if progress:
            progress(dict(summary))

    for index_url in targets:
        try:
            urls = source_pages(index_url, settings=settings)
        except Exception as exc:
            summary["failures"].append(
                {"url": index_url, "error": f"index unreadable: {exc}"}
            )
            summary["sources_done"] += 1
            report()
            continue

        source_stats = {"source": index_url, "pages": len(urls), "inserted": 0,
                        "updated": 0, "unchanged": 0, "failed": 0}

        for start in range(0, len(urls), WRITE_BATCH):
            batch = urls[start : start + WRITE_BATCH]
            pages, failures = fetch_pages(
                batch, index_url, settings=settings,
                on_page=lambda: summary.__setitem__("pages_seen", summary["pages_seen"] + 1),
            )
            written = doc_pages.upsert_pages(pages)
            for key in ("inserted", "updated", "unchanged"):
                summary[key] += written[key]
                source_stats[key] += written[key]
            source_stats["failed"] += len(failures)
            summary["failures"].extend(failures)
            report()

        summary["per_source"].append(source_stats)
        summary["sources_done"] += 1
        logger.info(
            "Docs refresh: %s — %d page(s), %d new, %d updated, %d unchanged, %d failed",
            index_url,
            source_stats["pages"],
            source_stats["inserted"],
            source_stats["updated"],
            source_stats["unchanged"],
            source_stats["failed"],
        )
        report()

    # The whole list would be thousands of entries on a bad network; the count is
    # what matters on screen and the first few are what a person acts on.
    summary["failure_count"] = len(summary["failures"])
    summary["failures"] = summary["failures"][:50]
    logger.info(
        "Docs refresh finished: %d source(s), %d page(s) seen, %d new, %d updated, "
        "%d unchanged, %d failed",
        summary["sources_done"],
        summary["pages_seen"],
        summary["inserted"],
        summary["updated"],
        summary["unchanged"],
        summary["failure_count"],
    )
    return summary
