"""JSON endpoints backing the questions screen.

Mounted under /api/admin so the server-rendered pages can own /admin. Generation
is a long-running Claude call (web search, then authoring — minutes not seconds),
so it runs in a background task and the page polls for status.

Run state is separate from the badge run state: generating questions and syncing
badges are unrelated jobs, and one must not report the other's result.
"""

import traceback
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.repositories import questions
from app.services.question_generation import generate_questions

router = APIRouter(prefix="/api/admin/questions", tags=["admin"])

MAX_QUESTIONS_PER_RUN = 25

# Single-process run state, as for badge discovery. Good enough for one internal
# authoring tool; if this ever runs multi-worker, move it into Mongo.
_run_state: dict = {
    "running": False,
    "last_result": None,
    "last_error": None,
    "last_traceback": None,
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
    _run_state.update(running=True, last_error=None, last_traceback=None)
    try:
        _run_state["last_result"] = generate_questions(
            request.skill_badges,
            request.count,
            source_material=request.source_material,
            extra_instructions=request.extra_instructions,
        )
    except Exception as exc:  # surfaced to the admin page, not swallowed
        _run_state["last_error"] = str(exc)
        # Keep the trace so the page can offer it without the author having to go
        # read server logs.
        _run_state["last_traceback"] = traceback.format_exc()
    finally:
        _run_state["running"] = False


@router.post("/generate")
def start_generation(request: GenerateRequest, background: BackgroundTasks) -> dict:
    if _run_state["running"]:
        raise HTTPException(409, "A generation run is already in progress.")
    background.add_task(_run_generation, request)
    return {"started": True}


@router.get("/generate/status")
def generation_status() -> dict:
    return _run_state


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
