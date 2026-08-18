"""JSON endpoints behind the documentation corpus screen.

Under /api/admin because maintaining the corpus is curation work, like the badge
catalog — the questions screen consumes it, but nobody authoring a question needs
to think about it.

A full crawl is ~10,000 pages, so a refresh runs in the background with its own run
state and the page polls for progress.
"""

import logging
import time
import traceback

from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.repositories import doc_pages
from app.services import doc_corpus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/docs", tags=["admin"])

# Separate from the badge and question run state: a docs refresh takes minutes and
# must not be reported as, or blocked by, an unrelated job.
_run_state: dict = {
    "running": False,
    "last_result": None,
    "last_error": None,
    "last_traceback": None,
    "started_at": None,
    "finished_at": None,
    # Written as the crawl proceeds, so the screen can show progress rather than
    # only a spinner for several minutes.
    "progress": None,
}


def run_state() -> dict:
    return _run_state


def _run_refresh() -> None:
    _run_state.update(
        running=True,
        last_error=None,
        last_traceback=None,
        started_at=time.time(),
        finished_at=None,
        progress=None,
    )
    logger.info("Docs refresh started")
    try:
        _run_state["last_result"] = doc_corpus.refresh(
            progress=lambda snapshot: _run_state.__setitem__("progress", snapshot)
        )
    except Exception as exc:  # surfaced to the page, not swallowed
        _run_state["last_error"] = str(exc)
        _run_state["last_traceback"] = traceback.format_exc()
        logger.exception("Docs refresh failed: %s", exc)
    finally:
        _run_state["running"] = False
        _run_state["finished_at"] = time.time()


@router.post("/refresh")
def start_refresh(background: BackgroundTasks) -> dict:
    """Replace the stored corpus with a fresh crawl of the whole documentation set."""
    if _run_state["running"]:
        raise HTTPException(409, "A documentation refresh is already in progress.")
    background.add_task(_run_refresh)
    return {"started": True}


@router.get("/refresh/status")
def refresh_status() -> dict:
    """Run state plus the server clock, which the page times the run against."""
    return {**_run_state, "server_time": time.time()}


@router.get("/sources")
def list_sources() -> dict:
    """What is stored, and what the published index currently offers.

    Both are returned together so the screen can show a source that exists upstream
    but has never been crawled — otherwise it is invisible.
    """
    stored = doc_pages.sources_summary()
    try:
        available = doc_corpus.discover_sources()
        discovery_error = None
    except Exception as exc:
        # The index being unreachable must not blank out what is already stored.
        available, discovery_error = [], str(exc)
    return {
        "stored": stored,
        "available": available,
        "totals": doc_pages.totals(),
        "discovery_error": discovery_error,
    }


@router.get("/pages")
def list_pages(source: str | None = None, limit: int = 200) -> list[dict]:
    """Stored pages, without their text — enough to confirm what was captured."""
    return doc_pages.list_pages(source, limit=max(1, min(limit, 1000)))


@router.get("/page")
def get_page(url: str) -> dict:
    """One stored page, with its text."""
    page = doc_pages.get_page(url)
    if not page:
        raise HTTPException(404, f"No stored page for {url!r}.")
    return page

