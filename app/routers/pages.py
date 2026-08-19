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

# No status tabs: there is no review workflow, so every stored question is a
# question in use. The only count worth a heading is how many are in scope.
# How many matches a search considers before the screen's filters narrow it.
SEARCH_LIMIT = 50
DIFFICULTY_STYLES = {
    "foundational": "info",
    "intermediate": "primary",
    "advanced": "dark",
}


@router.get("/")
def questions_page(
    request: Request,
    skill_badge: str | None = None,
    category: str | None = None,
    q: str | None = None,
):
    """The main screen: review stored questions and generate new ones.

    With `q` the list becomes a semantic search result, ranked by similarity, and
    the other filters narrow that result rather than the whole collection — an
    author searching within one badge means "of the matches, show me these".

    The badge list is loaded because generation is scoped by badge — with no
    badges there is nothing to generate against, and the template says so rather
    than offering an empty picker.
    """
    stored: list[dict] = []
    counts: dict[str, int] = {}
    badges: list[dict] = []
    categories: list[str] = []
    storage_error: str | None = None
    search_error: str | None = None
    query = (q or "").strip()

    try:
        if query:
            stored, search_error = _search(query, skill_badge, category)
            counts = {"": len(stored)}
            categories = questions_repo.categories_in_use()
            badges = skill_badges.list_badges()
            return _render(
                request,
                stored=stored,
                counts=counts,
                badges=badges,
                categories=categories,
                skill_badge=skill_badge,
                category=category,
                query=query,
                storage_error=None,
                search_error=search_error,
            )
        stored = questions_repo.list_questions(skill_badge, category)
        counts = {"": len(stored)}
        categories = questions_repo.categories_in_use()
        badges = skill_badges.list_badges()
    except PyMongoError as exc:
        # A wrong or unreachable connection string is the likeliest setup mistake;
        # say so on the page instead of returning a stack trace.
        storage_error = str(exc)

    return _render(
        request,
        stored=stored,
        counts=counts,
        badges=badges,
        categories=categories,
        skill_badge=skill_badge,
        category=category,
        query=query,
        storage_error=storage_error,
        search_error=None,
    )


def _search(
    query: str, skill_badge: str | None, category: str | None
) -> tuple[list[dict], str | None]:
    """Rank questions by meaning, then narrow the result with the screen's filters.

    Filtering after the search rather than inside it keeps the ranking intact: a
    vector search cannot be told "only this badge" without changing which matches it
    considers, and an author expects the same matches however they then narrow them.
    """
    settings = get_settings()
    try:
        matches = questions_repo.similar_by_embedding_text(
            query, settings.questions_vector_index_name, limit=SEARCH_LIMIT
        )
    except PyMongoError as exc:
        # An index that is still building is the likeliest cause, and it resolves
        # itself; say so rather than showing an empty result that reads as "we have
        # nothing on that".
        return [], str(exc)

    def keeps(item: dict) -> bool:
        return (
            (not skill_badge or skill_badge in (item.get("skill_badges") or []))
            and (not category or category in (item.get("categories") or []))
        )

    return [m for m in matches if keeps(m)], None


def _render(
    request: Request,
    *,
    stored: list[dict],
    counts: dict[str, int],
    badges: list[dict],
    categories: list[str],
    skill_badge: str | None,
    category: str | None,
    query: str,
    storage_error: str | None,
    search_error: str | None,
):
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "questions.html",
        {
            "active_page": "questions",
            "questions": stored,
            "counts": counts,
            "badge_filter": skill_badge or "",
            "category_filter": category or "",
            "query": query,
            "badges": badges,
            "categories": categories,
            "difficulty_styles": DIFFICULTY_STYLES,
            "database": settings.database,
            "collection": settings.questions_collection,
            "storage_error": storage_error,
            "search_error": search_error,
            "running": run_state()["running"],
            "last_result": run_state()["last_result"],
            "last_error": run_state()["last_error"],
            "last_traceback": run_state()["last_traceback"],
        },
    )
