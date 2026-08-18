"""JSON endpoints backing the questions screen.

Mounted under /api/questions, not /api/admin: writing and reviewing questions is
the authoring surface this tool exists for, while /admin holds the functions that
curate the badge catalog behind it. Nothing enforces that boundary — there are no
authorizations — it separates the two kinds of work.

Generation is a long-running Claude call (web search, then authoring — minutes
not seconds), so it runs in a background task and the page polls for status.

Run state is separate from the badge run state: generating questions and syncing
badges are unrelated jobs, and one must not report the other's result.
"""

import logging
import time
import traceback
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.repositories import questions
from app.services.question_generation import generate_questions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/questions", tags=["questions"])

MAX_QUESTIONS_PER_RUN = 25

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
    skill_badges: list[str] = Field(min_length=1, max_length=25)
    count: int = Field(default=5, ge=1, le=MAX_QUESTIONS_PER_RUN)
    source_material: str | None = None
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
        "Generation run started: %d question(s) for %s",
        request.count,
        ", ".join(request.skill_badges),
    )
    try:
        _run_state["last_result"] = generate_questions(
            request.skill_badges,
            request.count,
            source_material=request.source_material,
            extra_instructions=request.extra_instructions,
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
