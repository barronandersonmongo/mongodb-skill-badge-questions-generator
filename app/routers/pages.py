"""Server-rendered author pages.

The questions screen is the site root: writing and reviewing questions is the
work this tool exists for. The functions that curate the badge catalog behind it
live under /admin (see admin_pages.py). Nothing enforces that split — there are
no authorizations — it separates the two kinds of work so each screen has one
audience.

The list is rendered server-side so it is readable without JavaScript; JS only
drives the generate button's polling, the review buttons and the filter menus.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.repositories import questions as questions_repo
from app.repositories import skill_badges
from app.routers.questions import run_state

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

router = APIRouter(tags=["ui"])

STATUS_TABS = [
    ("", "All"),
    ("draft", "Drafts"),
    ("approved", "Approved"),
    ("rejected", "Rejected"),
]
STATUS_STYLES = {"draft": "warning", "approved": "success", "rejected": "secondary"}
DIFFICULTY_STYLES = {
    "foundational": "info",
    "intermediate": "primary",
    "advanced": "dark",
}


@router.get("/")
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
        stored = questions_repo.list_questions(status, skill_badge, category)
        # Counts describe the current badge/category filter, so switching status
        # tab does not appear to change how many questions exist.
        in_scope = questions_repo.list_questions(None, skill_badge, category)
        counts = {"": len(in_scope)}
        for tab, _ in STATUS_TABS[1:]:
            counts[tab] = sum(1 for q in in_scope if q.get("status") == tab)
        categories = questions_repo.categories_in_use()
        badges = skill_badges.list_badges()
    except PyMongoError as exc:
        # A wrong or unreachable connection string is the likeliest setup mistake;
        # say so on the page instead of returning a stack trace.
        storage_error = str(exc)

    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "questions.html",
        {
            "active_page": "questions",
            "questions": stored,
            "counts": counts,
            "tabs": STATUS_TABS,
            "status_filter": status or "",
            "badge_filter": skill_badge or "",
            "category_filter": category or "",
            "badges": badges,
            "categories": categories,
            "status_styles": STATUS_STYLES,
            "difficulty_styles": DIFFICULTY_STYLES,
            "database": settings.database,
            "collection": settings.questions_collection,
            "storage_error": storage_error,
            "running": run_state()["running"],
            "last_result": run_state()["last_result"],
            "last_error": run_state()["last_error"],
            "last_traceback": run_state()["last_traceback"],
        },
    )
