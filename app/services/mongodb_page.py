"""Find the title MongoDB gives a badge on learn.mongodb.com.

The course pages are rendered in the browser: three independent fetchers (raw
HTTP, the model's server-side fetch, and this app's own) see only the generic
site title, so the page cannot be read directly. Search engines have the
rendered title indexed against the same URL, so the badge's own learn.mongodb.com
URL is used as the lookup key and the indexed title is taken from the result that
matches it.

That makes this the least direct of the three title sources — it is an index of
the page rather than the page — so it is stored in its own field and never
overwrites a reviewed title.
"""

from pydantic import BaseModel, Field

from app.config import Settings, get_settings

TITLE_SYSTEM = """\
You find the published title of a MongoDB skill badge page.

You are given a badge's learn.mongodb.com URL and, usually, the name it is known \
by. Search for its page and report the title the search results show for that \
exact URL.

Searching for the URL itself ranks poorly. Search for the badge name instead — \
adding "skill badge" helps — and then pick the result whose URL is the one you \
were given.

Rules:
- The URL decides which result is the right one; the name is only how you find \
  it. A result for a different course, a learning path, or a lesson inside a \
  course is not the badge page.
- Report the title verbatim. Strip only a trailing site-name suffix such as \
  " | MongoDB University".
- If no result matches the URL, say so rather than reporting the closest title \
  you found. A missing title is fine; a wrong one is not.
- The site's own document title ("MongoDB Courses and Trainings | MongoDB \
  University") is not a badge name. If that is all a result shows, report that no \
  title was found."""


# The search index falls back to the site's own document title for pages it could
# not render. That is not a badge name, and storing it would show every reviewer
# the same meaningless string.
GENERIC_TITLES = (
    "mongodb courses and trainings",
    "mongodb university",
    "mongodb courses",
)


def is_generic(title: str) -> bool:
    cleaned = title.strip().lower().rstrip(".")
    return any(cleaned.startswith(generic) for generic in GENERIC_TITLES)


class MongoDBTitle(BaseModel):
    found: bool = Field(description="True only when a result matched the given URL")
    title: str = Field(
        description="The indexed page title, verbatim, or an empty string when not found"
    )
    matched_url: str = Field(
        description="The URL of the result the title came from, or an empty string"
    )


def _slug_of(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].lower()


def fetch_indexed_title(
    mongodb_url: str, name: str | None = None, *, settings: Settings | None = None
) -> tuple[str, str] | None:
    """Return (title, matched url) for a badge's learn.mongodb.com page, or None.

    `name` is the badge's known title, used only to find the page — searching for
    the URL alone ranks poorly. The result is still accepted only when its URL
    carries the same page slug, so a better-ranked but different course cannot be
    mistaken for this badge.
    """
    settings = settings or get_settings()
    from app.services.badge_discovery import _client, _translate_auth_error

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=8000,
            system=TITLE_SYSTEM,
            output_format=MongoDBTitle,
            tools=[
                {
                    "type": settings.web_search_tool,
                    "name": "web_search",
                    "max_uses": 3,
                    "allowed_domains": ["learn.mongodb.com"],
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"What title does this page carry?\nURL: {mongodb_url}"
                        + (f'\nKnown as: "{name}"' if name else "")
                    ),
                }
            ],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise

    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError(
            f"Looking up the MongoDB title produced no structured output (stop_reason="
            f"{response.stop_reason}) for {mongodb_url}."
        )
    if not parsed.found or not parsed.title.strip():
        return None
    title = parsed.title.strip()
    if is_generic(title):
        return None

    matched_url = parsed.matched_url.strip()
    if _slug_of(matched_url) != _slug_of(mongodb_url):
        # A different page, however well it ranked, is not this badge's title.
        return None
    return title, matched_url
