"""Admin: how much documentation each badge still has to draw on.

Its own module because it spans both halves of the program — the question
collection and the documentation corpus — and belongs to neither. It is admin
work: it answers "is this badge about to run out of material", which is a
question about the corpus rather than about any question in it.
"""

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.templating import Jinja2Templates
from pymongo.errors import PyMongoError

from app.repositories import questions as questions_repo
from app.repositories import skill_badges
from app.services import material

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

router = APIRouter(tags=["admin"])

DIFFICULTIES = ("foundational", "intermediate", "advanced")


@router.get("/api/admin/material")
def badge_material(
    skill_badge: str | None = None,
    contains: str | None = Query(default=None, max_length=200),
    category: str | None = None,
    difficulty: Literal["foundational", "intermediate", "advanced"] | None = None,
) -> list[dict]:
    """A row per badge: questions written, and the material left to write from.

    Fetched by the screen rather than rendered with it: resolving one badge's section
    set is dozens of vector searches, and all of them is tens of seconds. Narrowing to
    one badge is therefore also the way to make this fast, which is worth saying on
    the screen.
    """
    return material.badge_material(
        skill_badge=skill_badge,
        contains=contains,
        category=category,
        difficulty=difficulty,
    )


@router.get("/admin/material")
def material_page(request: Request):
    """The screen: a badge's material, with filters over both sides of it."""
    badges: list[dict] = []
    categories: list[str] = []
    storage_error: str | None = None
    try:
        badges = skill_badges.list_badges()
        categories = questions_repo.categories_in_use()
    except PyMongoError as exc:
        storage_error = str(exc)

    return templates.TemplateResponse(
        request,
        "admin/material.html",
        {
            "active_page": "material",
            "badges": badges,
            "categories": categories,
            "difficulties": DIFFICULTIES,
            "storage_error": storage_error,
        },
    )
