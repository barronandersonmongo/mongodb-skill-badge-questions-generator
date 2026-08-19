"""Server-rendered author pages.

The questions screen is the site root: writing and reviewing questions is the
work this tool exists for. The functions that curate the badge catalog behind it
live under /admin (see admin_pages.py). Nothing enforces that split — there are
no authorizations — it separates the two kinds of work so each screen has one
audience.

The list is rendered server-side so it is readable without JavaScript; JS only
drives the generate button's polling, the review buttons and the filter menus.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
from pymongo.errors import PyMongoError

from app.config import get_settings
from app.repositories import questions as questions_repo
from app.repositories import runs as runs_repo
from app.repositories import skill_badges
from app.routers.questions import run_state

templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

router = APIRouter(tags=["ui"])


# Figures a person reads rather than a machine parses. Registered as filters because
# the alternative — formatting in the route and passing strings — makes the template
# unable to say what a number is, and every screen would format money its own way.
def _number(value: object) -> float | None:
    """The value as a number, or None if there isn't one.

    An older run predates fields a newer one records, so a template asking for one gets
    Jinja's Undefined rather than None — and formatting that raises. Anything that is
    not a number is absent, which is what an em dash says.
    """
    return float(value) if isinstance(value, (int, float)) else None


def _money(value: object) -> str:
    number = _number(value)
    return "—" if number is None else f"${number:,.2f}"


def _unit_cost(value: object) -> str:
    """Cost per question. Four places, because it is fractions of a cent."""
    number = _number(value)
    return "—" if number is None else f"${number:,.4f}"


def _duration(seconds: object) -> str:
    number = _number(seconds)
    if number is None:
        return "—"
    total = max(0, round(number))
    if total < 60:
        return f"{total}s"
    minutes, remainder = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _when(epoch_seconds: object) -> str:
    """A recorded time, in UTC. The server's zone is the only one it can know."""
    number = _number(epoch_seconds)
    if not number:
        return "—"
    return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _rate(value: object) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:.1f}"


templates.env.filters["money"] = _money
templates.env.filters["unit_cost"] = _unit_cost
templates.env.filters["duration"] = _duration
templates.env.filters["when"] = _when
templates.env.filters["rate"] = _rate

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


@router.get("/duplicates")
def duplicates_page(request: Request):
    """The duplicate sweep and its report.

    Its own screen rather than a block on the questions list. The report is a long
    list about pairs of questions, every entry of which links to a question — and
    while it sat on the questions screen those links led back to the page the report
    was on. It is also not the work that screen is for: a sweep is something done
    occasionally to the whole collection, not part of writing or reviewing.

    The report itself lives in run state, so it survives a reload of this page but
    not a restart. Nothing is stored: re-running it is minutes of round trips, which
    is why every link out of it opens in a new tab.
    """
    state = run_state()
    result = state["last_result"]
    badges: list[dict] = []
    categories: list[str] = []
    storage_error: str | None = None
    try:
        badges = skill_badges.list_badges()
        categories = questions_repo.categories_in_use()
    except PyMongoError as exc:
        # The sweep's scope pickers need these. Without them the screen still works
        # unscoped, so this is reported rather than fatal.
        storage_error = str(exc)

    return templates.TemplateResponse(
        request,
        "duplicates.html",
        {
            "active_page": "duplicates",
            "badges": badges,
            "categories": categories,
            "difficulties": ("foundational", "intermediate", "advanced"),
            # Only a sweep's result belongs here. A finished generation run leaves its
            # own result in the same slot, and it is reported on its own screen.
            "last_result": result
            if (result or {}).get("source") == "question-duplicate-sweep"
            else None,
            "last_error": state["last_error"],
            "last_traceback": state["last_traceback"],
            "running": state["running"],
            "storage_error": storage_error,
        },
    )


@router.get("/runs")
def runs_page(request: Request):
    """Every recorded generation run, newest first, with the cumulative totals.

    Its own screen rather than a dialog on the questions list. A dialog cannot be
    linked to, could not be read beside the questions a run produced, and was fetched
    by JavaScript on open — so the one lasting record of what has been spent was the
    least reachable thing in the program. Run state itself does not survive a restart;
    this collection is why the history does.

    Rendered server-side like every other list: what it costs to keep the bank growing
    is a number someone may need to paste into a message, and it should not depend on
    a script having run.
    """
    history: list[dict] = []
    totals: dict = {}
    storage_error: str | None = None
    try:
        history = runs_repo.list_runs(limit=200)
        totals = runs_repo.totals()
    except PyMongoError as exc:
        storage_error = str(exc)

    return templates.TemplateResponse(
        request,
        "runs.html",
        {
            "active_page": "runs",
            "runs": history,
            "totals": totals,
            "storage_error": storage_error,
        },
    )


@router.get("/coverage")
def coverage_page(request: Request):
    """Which badges are thin, and whether there is material left to fix that.

    Its own screen rather than a dialog on the questions list: it is the answer to
    "what should I run next", which is a question asked before looking at questions
    rather than while looking at them, and its rows lead into the list.
    """
    return templates.TemplateResponse(
        request, "coverage.html", {"active_page": "coverage"}
    )


@router.get("/export")
def export_page(
    request: Request,
    skill_badge: str | None = None,
    category: str | None = None,
):
    """The questions matching the filters, as JSON, shown and offered as a download.

    It was a link in the questions screen's toolbar, scoped to whatever that screen
    happened to be filtered to — so what you exported depended on a filter you may
    have set minutes earlier and scrolled past. Here the scope is the screen's own,
    and stated.

    The JSON is rendered on the page as well as downloadable: pasting it somewhere is
    the usual thing to do with it, and a file that has to be opened to be read is a
    step in the way.
    """
    payload = "[]"
    badges: list[dict] = []
    categories: list[str] = []
    count = 0
    storage_error: str | None = None
    try:
        # No limit: an export filtered to one page of results would be a surprising
        # thing to hand someone.
        stored = questions_repo.list_questions(skill_badge, category)
        count = len(stored)
        payload = json.dumps(stored, indent=2, default=str)
        badges = skill_badges.list_badges()
        categories = questions_repo.categories_in_use()
    except PyMongoError as exc:
        storage_error = str(exc)

    settings = get_settings()
    query = urlencode(
        {k: v for k, v in (("skill_badge", skill_badge), ("category", category)) if v}
    )
    return templates.TemplateResponse(
        request,
        "export.html",
        {
            "active_page": "export",
            "payload": payload,
            "count": count,
            "badges": badges,
            "categories": categories,
            "badge_filter": skill_badge or "",
            "category_filter": category or "",
            # The download hits the API rather than re-rendering here, so the file is
            # the same bytes any other caller of that endpoint gets.
            "api": "/api/questions" + ("?" + query if query else ""),
            "database": settings.database,
            "collection": settings.questions_collection,
            "storage_error": storage_error,
        },
    )
