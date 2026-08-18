"""JSON endpoints backing the skill badge admin screens.

Mounted under /api/admin so the server-rendered pages can own /admin. Discovery
is a long-running Claude call (web search, minutes not seconds), so it runs in a
background task and the admin page polls for status.
"""

import logging
import time
import traceback
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from app.repositories import skill_badges
from app.services.badge_discovery import synchronize_badges, synchronize_from_catalog

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/skill-badges", tags=["admin"])

# Single-process run state. Good enough for one internal authoring tool; if this
# ever runs multi-worker, move it into Mongo.
_run_state: dict = {
    "running": False,
    "last_result": None,
    "last_error": None,
    "last_traceback": None,
    # Wall-clock epoch seconds, so the elapsed timer survives a page reload.
    "started_at": None,
    "finished_at": None,
}


def run_state() -> dict:
    """Current discovery-run state, for the page renderer to show on load."""
    return _run_state


def _begin_run(what: str) -> None:
    """Mark a run as started and stamp it, so the page can time it."""
    _run_state.update(
        running=True,
        last_error=None,
        last_traceback=None,
        started_at=time.time(),
        finished_at=None,
    )
    logger.info("%s started", what)


def _finish_run(what: str) -> None:
    _run_state["running"] = False
    _run_state["finished_at"] = time.time()
    logger.info("%s finished", what)


class DiscoverRequest(BaseModel):
    extra_instructions: str | None = None


class StatusRequest(BaseModel):
    status: Literal["candidate", "approved", "retired"]


class NameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)


class SourcesRequest(BaseModel):
    source_urls: list[str] = Field(max_length=25)

    @field_validator("source_urls")
    @classmethod
    def must_be_http_urls(cls, urls: list[str]) -> list[str]:
        cleaned = [u.strip() for u in urls if u.strip()]
        bad = [u for u in cleaned if not u.startswith(("http://", "https://"))]
        if bad:
            raise ValueError(f"not http(s) URLs: {', '.join(bad)}")
        return cleaned


def _run_catalog_sync() -> None:
    _begin_run("Catalog sync")
    try:
        _run_state["last_result"] = synchronize_from_catalog()
    except Exception as exc:  # surfaced to the admin page, not swallowed
        _run_state["last_error"] = str(exc)
        _run_state["last_traceback"] = traceback.format_exc()
        logger.exception("Catalog sync failed: %s", exc)
    finally:
        _finish_run("Catalog sync")


def _run_discovery(extra_instructions: str | None) -> None:
    _begin_run("Badge discovery")
    try:
        _run_state["last_result"] = synchronize_badges(
            extra_instructions=extra_instructions
        )
    except Exception as exc:  # surfaced to the admin page, not swallowed
        _run_state["last_error"] = str(exc)
        # Keep the trace so the page can offer it without the operator
        # having to go read server logs.
        _run_state["last_traceback"] = traceback.format_exc()
        logger.exception("Badge discovery failed: %s", exc)
    finally:
        _finish_run("Badge discovery")


@router.post("/discover")
def start_discovery(request: DiscoverRequest, background: BackgroundTasks) -> dict:
    if _run_state["running"]:
        raise HTTPException(409, "A discovery run is already in progress.")
    background.add_task(_run_discovery, request.extra_instructions)
    return {"started": True}


@router.post("/sync-catalog")
def start_catalog_sync(background: BackgroundTasks) -> dict:
    """Sync from the published badge collection — the authoritative badge set."""
    if _run_state["running"]:
        raise HTTPException(409, "A discovery run is already in progress.")
    background.add_task(_run_catalog_sync)
    return {"started": True}


@router.get("/discover/status")
def discovery_status() -> dict:
    """Run state, plus the server's clock, which the page times the run against."""
    return {**_run_state, "server_time": time.time()}


@router.get("")
def list_badges(status: str | None = None) -> list[dict]:
    return skill_badges.list_badges(status)


@router.post("/{slug}/status")
def update_status(slug: str, request: StatusRequest) -> dict:
    if not skill_badges.set_status(slug, request.status):
        raise HTTPException(404, f"No skill badge with slug {slug!r}.")
    return {"slug": slug, "status": request.status}


@router.post("/{slug}/name")
def update_name(slug: str, request: NameRequest) -> dict:
    """Correct a badge's title. The correction survives later discovery runs."""
    if not skill_badges.set_name(slug, request.name.strip()):
        raise HTTPException(404, f"No skill badge with slug {slug!r}.")
    return {"slug": slug, "name": request.name.strip(), "name_locked": True}


@router.post("/{slug}/sources")
def update_sources(slug: str, request: SourcesRequest) -> dict:
    """Curate a badge's reference links. The edit survives later discovery runs."""
    if not skill_badges.set_source_urls(slug, request.source_urls):
        raise HTTPException(404, f"No skill badge with slug {slug!r}.")
    return {
        "slug": slug,
        "source_urls": request.source_urls,
        "sources_locked": True,
    }


@router.delete("/{slug}")
def delete_badge(slug: str) -> dict:
    """Permanently delete a retired badge. Retire it first; this is not reversible."""
    if skill_badges.delete_badge(slug):
        return {"slug": slug, "deleted": True}
    if skill_badges.list_badges() and any(
        b["slug"] == slug for b in skill_badges.list_badges()
    ):
        raise HTTPException(409, f"{slug!r} must be retired before it can be deleted.")
    raise HTTPException(404, f"No skill badge with slug {slug!r}.")


class MergeRequest(BaseModel):
    keep: str
    drop: str


@router.post("/duplicates/scan")
def scan_duplicates(background: BackgroundTasks) -> dict:
    """Search descriptions for duplicate badges and merge the confident ones."""
    if _run_state["running"]:
        raise HTTPException(409, "A discovery run is already in progress.")
    background.add_task(_run_duplicate_scan)
    return {"started": True}


def _run_duplicate_scan() -> None:
    from app.services.duplicates import merge_confident_duplicates

    _begin_run("Duplicate scan")
    try:
        _run_state["last_result"] = {
            **merge_confident_duplicates(),
            "source": "duplicate-scan",
        }
    except Exception as exc:  # surfaced to the admin page, not swallowed
        _run_state["last_error"] = str(exc)
        _run_state["last_traceback"] = traceback.format_exc()
        logger.exception("Duplicate scan failed: %s", exc)
    finally:
        _finish_run("Duplicate scan")


@router.post("/merge")
def merge(request: MergeRequest) -> dict:
    """Merge one badge into another, keeping the curated fields of the survivor."""
    if request.keep == request.drop:
        raise HTTPException(422, "keep and drop must be different badges.")
    if not skill_badges.merge_badges(request.drop, request.keep):
        raise HTTPException(404, f"Could not merge {request.drop!r} into {request.keep!r}.")
    return {"keep": request.keep, "dropped": request.drop, "merged": True}


@router.get("/{slug}/image")
def badge_image(slug: str) -> Response:
    """Serve the badge artwork stored on the document."""
    found = skill_badges.get_image(slug)
    if not found:
        raise HTTPException(404, f"No stored artwork for {slug!r}.")
    data, content_type = found
    # Artwork only changes when the badge does, so let the browser keep it.
    return Response(
        content=data,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.post("/normalise-slugs")
def normalise_slugs() -> dict:
    """Move every badge to the slug derived from its artwork title."""
    return skill_badges.normalise_slugs()
