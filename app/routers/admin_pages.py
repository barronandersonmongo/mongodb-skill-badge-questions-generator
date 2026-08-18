"""Server-rendered admin pages.

HTML lives under /admin; the JSON the pages call lives under /api/admin. The
table is rendered server-side so the review queue is readable without
JavaScript; JS only drives the discover button's polling and the status buttons.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.repositories import questions, skill_badges
from app.routers.admin_questions import run_state as questions_run_state
from app.routers.admin_skill_badges import run_state

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

router = APIRouter(prefix="/admin", tags=["admin-ui"])

STATUS_TABS = [
    ("", "All"),
    ("candidate", "Candidates"),
    ("approved", "Approved"),
    ("retired", "Retired"),
]
STATUS_STYLES = {"candidate": "warning", "approved": "success", "retired": "secondary"}
QUESTION_STATUS_TABS = [
    ("", "All"),
    ("draft", "Drafts"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
]
QUESTION_STATUS_STYLES = {
    "draft": "warning",
    "approved": "success",
    "rejected": "secondary",
}
DIFFICULTY_STYLES = {
    "foundational": "info",
    "intermediate": "primary",
    "advanced": "dark",
}
CONFIDENCE_STYLES = {"high": "success", "medium": "warning", "low": "danger"}


@router.get("")
def admin_home() -> RedirectResponse:
    """Questions are the point of the tool, so that is the screen people land on."""
    return RedirectResponse("/admin/questions", status_code=307)


@router.get("/questions")
def questions_page(
    request: Request,
    status: str | None = None,
    skill_badge: str | None = None,
    category: str | None = None,
):
    """The main screen: review stored questions and generate new ones.

    The badge list is loaded because generation is scoped by badge — with no
    badges there is nothing to generate against, and the template says so rather
    than offering an empty picker.
    """
    stored: list[dict] = []
    counts: dict[str, int] = {}
    badges: list[dict] = []
    categories: list[str] = []
    storage_error: str | None = None

    try:
        stored = questions.list_questions(status, skill_badge, category)
        # Counts describe the current badge/category filter, so switching status
        # tab does not appear to change how many questions exist.
        in_scope = questions.list_questions(None, skill_badge, category)
        counts = {"": len(in_scope)}
        for tab, _ in QUESTION_STATUS_TABS[1:]:
            counts[tab] = sum(1 for q in in_scope if q.get("status") == tab)
        categories = questions.categories_in_use()
        badges = skill_badges.list_badges()
    except PyMongoError as exc:
        # A wrong or unreachable connection string is the likeliest setup mistake;
        # say so on the page instead of returning a stack trace.
        storage_error = str(exc)

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "admin/questions.html",
        {
            "active_page": "questions",
            "questions": stored,
            "counts": counts,
            "tabs": QUESTION_STATUS_TABS,
            "status_filter": status or "",
            "badge_filter": skill_badge or "",
            "category_filter": category or "",
            "badges": badges,
            "categories": categories,
            "status_styles": QUESTION_STATUS_STYLES,
            "difficulty_styles": DIFFICULTY_STYLES,
            "database": settings.database,
            "collection": settings.questions_collection,
            "storage_error": storage_error,
            "running": questions_run_state()["running"],
            "last_result": questions_run_state()["last_result"],
            "last_error": questions_run_state()["last_error"],
            "last_traceback": questions_run_state()["last_traceback"],
        },
    )


@router.get("/skill-badges")
def skill_badges_page(request: Request, status: str | None = None):
    badges: list[dict] = []
    counts: dict[str, int] = {}
    storage_error: str | None = None

    try:
        badges = skill_badges.list_badges(status)
        all_badges = badges if not status else skill_badges.list_badges()
        counts = {"": len(all_badges)}
        for tab, _ in STATUS_TABS[1:]:
            counts[tab] = sum(1 for b in all_badges if b.get("status") == tab)
    except PyMongoError as exc:
        # A wrong or unreachable connection string is the likeliest setup mistake;
        # say so on the page instead of returning a stack trace.
        storage_error = str(exc)

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "admin/skill_badges.html",
        {
            "active_page": "skill_badges",
            "badges": badges,
            "counts": counts,
            "tabs": STATUS_TABS,
            "status_filter": status or "",
            "status_styles": STATUS_STYLES,
            "confidence_styles": CONFIDENCE_STYLES,
            "database": settings.database,
            "collection": settings.skill_badges_collection,
            "catalog_url": settings.catalog_url,
            "catalog_domain": settings.catalog_domain,
            "storage_error": storage_error,
            "running": run_state()["running"],
            "last_result": run_state()["last_result"],
            "last_error": run_state()["last_error"],
            "last_traceback": run_state()["last_traceback"],
        },
    )
