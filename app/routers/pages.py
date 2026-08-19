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

# How many questions one screen renders. The bank is meant to hold thousands, and
# rendering all of them is a document the browser is slow to lay out and scroll.
# The choices span "read a handful closely" to "scan a lot at once"; 50 is the
# default because it fills a screen or two without the page becoming a burden.
PAGE_SIZES = (5, 10, 25, 50, 100, 200, 500)
DEFAULT_PAGE_SIZE = 50
DIFFICULTY_STYLES = {
    "foundational": "info",
    "intermediate": "primary",
    "advanced": "dark",
}


def _page_size(requested: int | None) -> int:
    """The page size to use. An unoffered one falls back to the default.

    Validated rather than trusted: the size reaches the database as a limit, and a
    hand-edited URL asking for a hundred thousand would render the page this exists
    to prevent.
    """
    return requested if requested in PAGE_SIZES else DEFAULT_PAGE_SIZE


def _pagination(total: int, page: int, per_page: int) -> dict:
    """Where this page sits in the whole result.

    The page number is clamped rather than rejected: deleting the last question on
    the last page, or narrowing a filter, leaves a URL pointing past the end, and an
    error there would be a dead end where showing the last page is what was meant.
    """
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    first = 0 if total == 0 else (page - 1) * per_page + 1
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "sizes": PAGE_SIZES,
        "total": total,
        "first": first,
        "last": min(page * per_page, total),
        "skip": (page - 1) * per_page,
        "has_previous": page > 1,
        "has_next": page < pages,
    }


@router.get("/")
def questions_page(
    request: Request,
    skill_badge: str | None = None,
    category: str | None = None,
    q: str | None = None,
    page: int = 1,
    per_page: int | None = None,
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

    size = _page_size(per_page)
    pagination = _pagination(0, page, size)

    try:
        if query:
            stored, search_error = _search(query, skill_badge, category)
            # A search is already capped at what it considered, so its matches are in
            # hand and paged here rather than by the database. Paging them at all is
            # for consistency: a result that behaved differently from the list would
            # be a second set of rules to learn.
            pagination = _pagination(len(stored), page, size)
            counts = {"": len(stored)}
            stored = stored[pagination["skip"]:pagination["skip"] + size]
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
                pagination=pagination,
                storage_error=None,
                search_error=search_error,
            )
        # Counted before it is read: the count is what the pager needs, and it is
        # cheaper than fetching every match to measure it.
        total = questions_repo.count_questions(skill_badge, category)
        pagination = _pagination(total, page, size)
        stored = questions_repo.list_questions(
            skill_badge, category, skip=pagination["skip"], limit=size
        )
        counts = {"": total}
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
        pagination=pagination,
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
    # An identifier is looked up exactly rather than embedded and compared: a hex string
    # has no meaning to embed, so a semantic search for one returns whatever happens to
    # be nearest, which reads as "that question does not exist".
    if questions_repo.looks_like_an_identifier(query):
        found = questions_repo.find_by_identifier(query)
        return found, None if found else "no-such-identifier"

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
    pagination: dict,
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
            "pagination": pagination,
            "is_identifier_query": questions_repo.looks_like_an_identifier(query),
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
