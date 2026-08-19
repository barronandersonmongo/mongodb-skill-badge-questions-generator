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
from uuid import uuid4
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from pymongo.errors import PyMongoError

from app.repositories import questions
from app.repositories import runs as runs_repo
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
    # Written as the walk proceeds, so the screen shows where it is rather than only
    # what it produced. Separate from last_result, which is the finished run.
    "progress": None,
    # Set by the stop endpoint and read between pages. A flag rather than a signal
    # because the walk must finish the page it is on and keep what it wrote — killing
    # it mid-call would pay for a page and store nothing.
    "stop_requested": False,
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
    # The scale every question already carries. None means "let the material decide",
    # which spreads a run across the three levels rather than pitching them all alike.
    difficulty: Literal["foundational", "intermediate", "advanced"] | None = None
    extra_instructions: str | None = None


def _run_generation(request: GenerateRequest) -> None:
    _run_state.update(
        running=True,
        last_error=None,
        last_traceback=None,
        # Recorded on the server so the elapsed timer survives a page reload or a
        # trip to another screen; the browser cannot remember it across either.
        started_at=time.time(),
        finished_at=None,
        progress=None,
        stop_requested=False,
    )
    logger.info(
        "Generation run started: up to %d page(s) each for %s",
        request.max_pages,
        ", ".join(request.skill_badges),
    )

    # A multi-badge run is several walks in sequence, each with its own 0-100%. Without
    # this the bar reaches the end and starts again with nothing to say why, which reads
    # as the run having restarted.
    badge_count = len(request.skill_badges)
    position = {"index": 0}

    def progress(state: dict) -> None:
        # Written straight onto the run state, so the polling endpoint reports the
        # walk as it happens rather than only when it ends.
        _run_state["progress"] = {
            **state,
            "badge_index": position["index"] + 1,
            "badge_count": badge_count,
            "badges_done": position["index"],
        }

    try:
        results = []
        for index, slug in enumerate(request.skill_badges):
            position["index"] = index
            results.append(
                generate_for_badge(
                    slug,
                    max_pages=request.max_pages,
                    questions_per_page=request.questions_per_page,
                    difficulty=request.difficulty,
                    extra_instructions=request.extra_instructions,
                    progress=progress,
                    stop=lambda: _run_state["stop_requested"],
                )
            )
            if _run_state["stop_requested"]:
                # A stop applies to the request, not just the badge being walked:
                # the author asked for the spending to end.
                break
        _run_state["last_result"] = (
            results[0] if len(results) == 1 else _combine(results, request)
        )
        logger.info(
            "Generation run finished: %s stored, %s discarded",
            _run_state["last_result"].get("inserted"),
            len(_run_state["last_result"].get("rejected") or []),
        )
        _record(_run_state["last_result"])
    except Exception as exc:  # surfaced to the page, not swallowed
        _run_state["last_error"] = str(exc)
        # Keep the trace so the page can offer it without the author having to go
        # read server logs.
        _run_state["last_traceback"] = traceback.format_exc()
        logger.exception("Generation run failed: %s", exc)
        # A failed run is worth recording too: "we tried this badge and it broke" is
        # exactly the thing that gets forgotten and retried.
        _record(
            {
                **(_run_state.get("progress") or {}),
                "source": "badge-page-walk",
                "skill_badges": request.skill_badges,
                "phase": "failed",
                "error": str(exc),
            }
        )
    finally:
        _run_state["running"] = False
        _run_state["finished_at"] = time.time()


def _record(summary: dict | None) -> None:
    """Persist a finished run, stamped with the clock times the run state holds.

    Timings live on the run state rather than the summary because they are wall-clock
    epochs owned by the request, not by the walk — and they are the part that is gone
    for good once the process restarts.
    """
    if not summary:
        return
    started = _run_state.get("started_at")
    finished = time.time()
    runs_repo.record_run(
        {
            **summary,
            "run_id": summary.get("run_id") or uuid4().hex,
            "started_at": started,
            "finished_at": finished,
            "elapsed_seconds": (
                summary.get("elapsed_seconds")
                if summary.get("elapsed_seconds") is not None
                else (round(finished - started, 1) if started else None)
            ),
        }
    )


def _combine(results: list[dict], request: GenerateRequest) -> dict:
    """One summary for a run that walked several badges.

    Reported as a single run because that is what the author asked for; the per-badge
    numbers are kept so a badge that produced nothing is still visible as such.
    """
    combined: dict = {
        "source": "badge-page-walk",
        "run_id": uuid4().hex,
        "per_badge_run_ids": [r.get("run_id") for r in results],
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


@router.post("/generate/dismiss")
def dismiss_last_result() -> dict:
    """Clear the last run's result from the screen.

    Safe to lose: every finished run is recorded in `generation_runs`, so dismissing
    hides a notice rather than discarding the record. Without this the green summary is
    permanent until the next run, which makes the screen unreadable for anyone who is
    not currently generating.
    """
    _run_state["last_result"] = None
    _run_state["last_error"] = None
    _run_state["last_traceback"] = None
    _run_state["progress"] = None
    return {"dismissed": True}


@router.get("/runs")
def list_runs(skill_badge: str | None = None, limit: int = Query(default=50, ge=1, le=500)) -> dict:
    """Recorded runs, newest first, with the cumulative totals.

    The history is what makes a prompt change assessable: per-run cost is small enough
    to ignore and large enough to matter in aggregate, and neither the token counts nor
    the wall clock survive a restart of the process.
    """
    return {
        "runs": runs_repo.list_runs(skill_badge=skill_badge, limit=limit),
        "totals": runs_repo.totals(),
    }


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    """One recorded run in full, including the pages it read."""
    run = runs_repo.get_run(run_id)
    if run is None:
        raise HTTPException(404, f"No recorded run with id {run_id!r}.")
    return run


@router.get("/count")
def question_count(skill_badge: str | None = None, category: str | None = None) -> dict:
    """How many questions are stored. Cheap enough to poll.

    The screen polls this while a run is going: a walk stores section by section over many
    minutes, so a list left alone goes stale while its reader watches it being filled.
    A count rather than the questions themselves, because the answer is usually "no
    change" and fetching every question to discover that would be wasteful.
    """
    return {"count": questions.count_questions(skill_badge, category)}


@router.get("/coverage")
def coverage() -> list[dict]:
    """Per-badge question counts, plus how much documentation each badge still has.

    This is the screen that makes a thin badge actionable: a badge with 17 questions
    and 300 unused sections needs another walk, while one with 17 questions and no
    unused sections has exhausted its material and needs the corpus widened instead.
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
            "total": counts.get(slug, 0),
        }
        try:
            used = questions.source_chunk_ids_for_badge(slug)
            row["pages_used"] = len(used)
            row["pages_available"] = len(
                doc_retrieval.chunk_set_for_badge(badge, exclude_chunk_ids=used)
            )
        except PyMongoError as exc:
            # The chunk set needs the Atlas index. Counts are still worth showing
            # without it, so this reports "unknown" rather than failing the screen.
            logger.warning("Coverage could not resolve sections for %s: %s", slug, exc)
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


@router.post("/generate/stop")
def stop_generation() -> dict:
    """Ask the running walk to stop after the page it is on.

    Not a cancellation: the page in flight is already paid for, so it is allowed to
    finish and its questions are kept. Everything after it is skipped, which is the
    point — the reason to stop a walk is to stop it spending.
    """
    if not _run_state["running"]:
        raise HTTPException(409, "No generation run is in progress.")
    _run_state["stop_requested"] = True
    logger.info("Generation run asked to stop after the current page")
    return {"stopping": True}


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
    skill_badge: str | None = None,
    category: str | None = None,
) -> list[dict]:
    """Stored questions, filtered. This is also the export: it returns plain JSON."""
    return questions.list_questions(skill_badge, category)


@router.post("/duplicates/sweep")
def sweep_duplicates(background: BackgroundTasks) -> dict:
    """Find duplicate questions already stored. Deletes nothing.

    Runs in the background like a generation run: the sweep is one vector search
    and one rerank call per question, which is fast but not instant on a large
    collection.

    There is no delete mode. It made the operator choose before seeing the
    collection, and since reporting is strictly more informative nobody should have
    run the other one first — so deleting is now a separate act, on the list this
    returns, through `/duplicates/delete`.
    """
    if _run_state["running"]:
        raise HTTPException(409, "A run is already in progress.")
    background.add_task(_run_sweep)
    return {"started": True}


class DeleteDuplicatesRequest(BaseModel):
    """The questions an operator has chosen to delete from a sweep's report."""

    question_ids: list[str] = Field(min_length=1, max_length=1000)


@router.post("/duplicates/delete")
def delete_duplicates(request: DeleteDuplicatesRequest) -> dict:
    """Delete questions chosen from a duplicate report.

    Explicit ids rather than "delete everything flagged": the screen sends what the
    operator actually ticked, so a pair they judged to be two different questions
    stays put, and a report that has gone stale cannot delete something that was not
    on it.
    """
    deleted = questions.delete_questions(request.question_ids)
    logger.info(
        "Deleted %d of %d question(s) chosen from a duplicate report",
        deleted,
        len(request.question_ids),
    )
    return {"requested": len(request.question_ids), "deleted": deleted}


def _run_sweep() -> None:
    from app.services.question_duplicates import report

    _run_state.update(
        running=True,
        last_error=None,
        last_traceback=None,
        started_at=time.time(),
        finished_at=None,
        progress=None,
        stop_requested=False,
    )
    logger.info("Duplicate sweep started")
    try:
        _run_state["last_result"] = report()
        _record(_run_state["last_result"])
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


@router.post("/shuffle-options")
def shuffle_options(seed: int | None = None) -> dict:
    """Re-order the options of every stored question, and report the result.

    For questions written before the order was randomised: the first 125 this program
    produced all had the correct answer in position A, which makes them useless as a
    quiz — a candidate always answering A scores 100%.
    """
    result = questions.shuffle_stored_options(seed)
    result["positions"] = questions.correct_answer_positions()
    logger.info(
        "Shuffled stored options: %d changed, %d unchanged; positions now %s",
        result["changed"],
        result["unchanged"],
        result["positions"],
    )
    return result


@router.get("/answer-positions")
def answer_positions() -> dict:
    """Where the correct answer sits across the collection.

    The check that catches this failing again: an even spread is healthy, and anything
    approaching a single position means the shuffle is not running.
    """
    return questions.correct_answer_positions()


@router.post("/drop-status")
def drop_status() -> dict:
    """Strip `status` from questions stored before the review workflow was dropped.

    A leftover `"status": "draft"` rides along in the JSON export and tells whoever
    consumes it that the question is unfinished, when no such state exists any more.
    Safe to run repeatedly.
    """
    changed = questions.drop_status_field()
    logger.info("Dropped the status field from %d question(s)", changed)
    return {"changed": changed}


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


@router.delete("/{question_id}")
def delete_question(question_id: str) -> dict:
    """Permanently delete a question. Nothing can re-derive it, so this is final."""
    if not questions.delete_question(question_id):
        raise HTTPException(404, f"No question with id {question_id!r}.")
    return {"question_id": question_id, "deleted": True}
