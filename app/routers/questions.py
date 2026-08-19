"""JSON endpoints backing the questions screen.

Mounted under /api/questions, not /api/admin: writing and reviewing questions is
the authoring surface this tool exists for, while /admin holds the functions that
curate the badge catalog behind it. Nothing enforces that boundary — there are no
authorizations — it separates the two kinds of work.

Generation walks the documentation pages a badge is about, one Claude call per
page — many minutes, not seconds — so it runs in a background task and the page
polls for status. Progress is reported per page, as the documentation refresh
does, because a walk is unreadable without it.

Run state is separate from the badge run state: generating questions and syncing
badges are unrelated jobs, and one must not report the other's result.
"""

import logging
import time
import traceback
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from pymongo.errors import PyMongoError

from app.repositories import questions
from app.services.question_generation import generate_for_badge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/questions", tags=["questions"])

# A walk is bounded by pages, not questions: one page is worth several questions and
# the cap is really "how long am I prepared to wait for this badge".
MAX_PAGES_PER_RUN = 200

# Single-process run state, as for badge discovery. Good enough for one internal
# authoring tool; if this ever runs multi-worker, move it into Mongo.
_run_state: dict = {
    "running": False,
    "last_result": None,
    "last_error": None,
    "last_traceback": None,
    # Wall-clock epoch seconds. The page derives its elapsed timer from these
    # rather than from when the browser happened to start watching.
    "started_at": None,
    "finished_at": None,
}


def run_state() -> dict:
    """Current generation-run state, for the page renderer to show on load."""
    return _run_state


class GenerateRequest(BaseModel):
    """A request for more questions on one or more badges.

    Sized in pages rather than questions: a run walks the badge's documentation and
    each page is worth several questions, so "how many pages" is the thing an author
    can trade against how long they are willing to wait.
    """

    skill_badges: list[str] = Field(min_length=1, max_length=25)
    max_pages: int = Field(default=25, ge=1, le=MAX_PAGES_PER_RUN)
    questions_per_page: int = Field(default=3, ge=1, le=10)
    extra_instructions: str | None = None


class StatusRequest(BaseModel):
    status: Literal["draft", "approved", "rejected"]


def _run_generation(request: GenerateRequest) -> None:
    _run_state.update(
        running=True,
        last_error=None,
        last_traceback=None,
        # Recorded on the server so the elapsed timer survives a page reload or a
        # trip to another screen; the browser cannot remember it across either.
        started_at=time.time(),
        finished_at=None,
    )
    logger.info(
        "Generation run started: up to %d page(s) each for %s",
        request.max_pages,
        ", ".join(request.skill_badges),
    )

    def progress(state: dict) -> None:
        # Written straight onto the run state, so the polling endpoint reports the
        # walk as it happens rather than only when it ends.
        _run_state["last_result"] = state

    try:
        results = []
        for slug in request.skill_badges:
            results.append(
                generate_for_badge(
                    slug,
                    max_pages=request.max_pages,
                    questions_per_page=request.questions_per_page,
                    extra_instructions=request.extra_instructions,
                    progress=progress,
                )
            )
        _run_state["last_result"] = (
            results[0] if len(results) == 1 else _combine(results, request)
        )
        logger.info(
            "Generation run finished: %s stored, %s discarded",
            _run_state["last_result"].get("inserted"),
            len(_run_state["last_result"].get("rejected") or []),
        )
    except Exception as exc:  # surfaced to the page, not swallowed
        _run_state["last_error"] = str(exc)
        # Keep the trace so the page can offer it without the author having to go
        # read server logs.
        _run_state["last_traceback"] = traceback.format_exc()
        logger.exception("Generation run failed: %s", exc)
    finally:
        _run_state["running"] = False
        _run_state["finished_at"] = time.time()


def _combine(results: list[dict], request: GenerateRequest) -> dict:
    """One summary for a run that walked several badges.

    Reported as a single run because that is what the author asked for; the per-badge
    numbers are kept so a badge that produced nothing is still visible as such.
    """
    combined: dict = {
        "source": "badge-page-walk",
        "skill_badges": [r["skill_badge"] for r in results],
        "per_badge": [
            {
                "skill_badge": r["skill_badge"],
                "badge_name": r.get("badge_name"),
                "inserted": r["inserted"],
                "pages_done": r["pages_done"],
                "pages_available": r["pages_available"],
            }
            for r in results
        ],
        "questions_per_page": request.questions_per_page,
    }
    for field in ("pages_total", "pages_done", "inserted", "generated"):
        combined[field] = sum(r.get(field) or 0 for r in results)
    for field in ("rejected", "failures", "source_pages", "question_ids"):
        combined[field] = [item for r in results for item in (r.get(field) or [])]
    combined["failure_count"] = len(combined["failures"])
    return combined


@router.get("/coverage")
def coverage() -> list[dict]:
    """Per-badge question counts, plus how much documentation each badge still has.

    This is the screen that makes a thin badge actionable: a badge with 17 questions
    and 300 unused pages needs another walk, while one with 17 questions and no unused
    pages has exhausted its material and needs the corpus widened instead.
    """
    from app.repositories import skill_badges
    from app.services import doc_retrieval

    counts = questions.counts_by_badge()
    rows = []
    for badge in skill_badges.list_badges():
        slug = badge["slug"]
        row = {
            "skill_badge": slug,
            "name": badge.get("name"),
            **counts.get(slug, {"draft": 0, "approved": 0, "rejected": 0, "total": 0}),
        }
        try:
            used = questions.source_urls_for_badge(slug)
            row["pages_used"] = len(used)
            row["pages_available"] = len(
                doc_retrieval.page_set_for_badge(badge, exclude_urls=used)
            )
        except PyMongoError as exc:
            # The page set needs the Atlas index. Counts are still worth showing
            # without it, so this reports "unknown" rather than failing the screen.
            logger.warning("Coverage could not resolve pages for %s: %s", slug, exc)
            row["pages_used"] = None
            row["pages_available"] = None
        rows.append(row)
    return sorted(rows, key=lambda r: (r["total"], r["name"] or ""))


@router.post("/generate")
def start_generation(request: GenerateRequest, background: BackgroundTasks) -> dict:
    if _run_state["running"]:
        raise HTTPException(409, "A generation run is already in progress.")
    background.add_task(_run_generation, request)
    return {"started": True}


@router.get("/generate/status")
def generation_status() -> dict:
    """Run state, plus the server's clock.

    `server_time` lets the page measure elapsed time against the same clock the
    run was stamped with, so a browser whose clock is off — or in a different time
    zone — still shows the real duration.
    """
    return {**_run_state, "server_time": time.time()}


@router.get("")
def list_questions(
    status: str | None = None,
    skill_badge: str | None = None,
    category: str | None = None,
) -> list[dict]:
    """Stored questions, filtered. This is also the export: it returns plain JSON."""
    return questions.list_questions(status, skill_badge, category)


@router.post("/duplicates/sweep")
def sweep_duplicates(background: BackgroundTasks, dry_run: bool = False) -> dict:
    """Find duplicate questions already stored, deleting the clear ones.

    Runs in the background like a generation run: the sweep is one vector search
    and one rerank call per question, which is fast but not instant on a large
    collection. `dry_run=true` reports what it would delete without deleting it.
    """
    if _run_state["running"]:
        raise HTTPException(409, "A run is already in progress.")
    background.add_task(_run_sweep, dry_run)
    return {"started": True}


def _run_sweep(dry_run: bool) -> None:
    from app.services.question_duplicates import sweep

    _run_state.update(
        running=True,
        last_error=None,
        last_traceback=None,
        started_at=time.time(),
        finished_at=None,
    )
    logger.info("Duplicate sweep started%s", " (dry run)" if dry_run else "")
    try:
        _run_state["last_result"] = sweep(delete=not dry_run)
    except Exception as exc:  # surfaced to the page, not swallowed
        _run_state["last_error"] = str(exc)
        _run_state["last_traceback"] = traceback.format_exc()
        logger.exception("Duplicate sweep failed: %s", exc)
    finally:
        _run_state["running"] = False
        _run_state["finished_at"] = time.time()


@router.get("/search")
def search_questions(
    q: str = Query(min_length=2, max_length=1000),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Questions closest in meaning to `q`, most similar first.

    Meaning rather than words: an author asking "questions about joining
    collections" should find the `$lookup` questions whether or not they used that
    word. Scores come back with the results so a weak match is visible as one.
    """
    from app.config import get_settings

    settings = get_settings()
    try:
        return questions.similar_by_embedding_text(
            q, settings.questions_vector_index_name, limit=limit
        )
    except PyMongoError as exc:
        # The commonest cause by far is an index still building, or renamed. That is
        # a state to report, not a bug to hide behind a 500.
        raise HTTPException(
            503, f"Vector search is unavailable ({exc}). Is the index built?"
        ) from exc


@router.post("/backfill-embedding-text")
def backfill_embedding_text() -> dict:
    """Compose the embedding field for any stored question missing it.

    Questions written before the field existed carry none, and a vector index
    skips a document whose indexed path is absent — which looks identical to a
    question that was never written. Safe to run repeatedly.
    """
    result = questions.backfill_embedding_text()
    logger.info(
        "Embedding-text backfill: %d written, %d already correct",
        result["written"],
        result["already_correct"],
    )
    return result


@router.post("/{question_id}/status")
def update_status(question_id: str, request: StatusRequest) -> dict:
    if not questions.set_status(question_id, request.status):
        raise HTTPException(404, f"No question with id {question_id!r}.")
    return {"question_id": question_id, "status": request.status}


@router.delete("/{question_id}")
def delete_question(question_id: str) -> dict:
    """Permanently delete a question. Nothing can re-derive it, so this is final."""
    if not questions.delete_question(question_id):
        raise HTTPException(404, f"No question with id {question_id!r}.")
    return {"question_id": question_id, "deleted": True}
