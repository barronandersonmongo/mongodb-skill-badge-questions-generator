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
from app.repositories import skill_badges
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
CONFIDENCE_STYLES = {"high": "success", "medium": "warning", "low": "danger"}


@router.get("")
def admin_home() -> RedirectResponse:
    """The admin area currently has one screen; send people straight to it."""
    return RedirectResponse("/admin/skill-badges", status_code=307)


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
