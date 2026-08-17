"""Discover the full set of MongoDB skill badges using Claude.

Two passes, deliberately separated:

1. Research — Claude searches the web (server-side tool) and writes up every
   skill badge it can find, with sources. Streaming, because a research turn
   can run for minutes.
2. Extraction — the research notes are turned into validated structured output.

Splitting them keeps the schema out of the research turn (where tool use and
structured output interact awkwardly) and makes the raw notes auditable.
"""

import anthropic

from app.config import Settings, get_settings
from app.models.skill_badge import DiscoveredBadge, DiscoveredBadges

RESEARCH_SYSTEM = """\
You research MongoDB's official skill badge catalog on behalf of the MongoDB \
team that authors the badge quizzes.

The published catalog is the ONLY authority on which skill badges exist. Start \
by fetching the catalog URL given in the request. It lists each badge as a card \
linking to that badge's own page on learn.mongodb.com.

Rules, in order of importance:

1. A badge counts only if it is in that catalog. Credly pages, blog posts, \
   press releases, conference talks and course pages are NOT evidence that a \
   skill badge exists — several such pages describe badges that were retired or \
   that were never skill badges at all. Do not report a badge you cannot place \
   in the catalog.
2. Use the badge's own catalog page for its canonical title. Do not paraphrase \
   a title, expand an abbreviation, or reformat capitalisation.
3. The catalog paginates in the browser, so one fetch shows only the first \
   page. Keep working until you have accounted for every badge the catalog says \
   it contains — the page states the total. Fetch the badge links the catalog \
   exposes, and use site-restricted search against learn.mongodb.com to reach \
   badges whose cards you could not load. Say so explicitly if you cannot reach \
   the full total.
4. Every badge you report must carry at least one learn.mongodb.com URL as \
   evidence.

For each badge record: its canonical title, what it covers, the topic areas it \
exercises, and its supporting learn.mongodb.com URLs. Mark a badge as uncertain \
when you could not confirm it in the catalog, rather than presenting it as \
confirmed or dropping it silently.

Report the badges as a plain list. Do not write a preamble or a methodology \
section."""

EXTRACT_SYSTEM = """\
Convert the research notes into structured records. Include every badge the \
notes mention, carrying over its stated confidence — do not upgrade a \
tentative badge to high confidence, and do not invent badges, categories, or \
URLs that the notes do not contain. Use kebab-case for slugs."""


MISSING_CREDENTIALS_MESSAGE = (
    "No Anthropic credentials found. Set ANTHROPIC_API_KEY in the environment the "
    "server was started from (or run `ant auth login`), then restart the server. "
    "Note that ~/.profile is only read by login shells, so a variable exported "
    "there is not visible to a service started another way."
)


def _client(settings: Settings | None = None) -> anthropic.Anthropic:
    settings = settings or get_settings()
    if settings.uses_gateway:
        # The gateway authenticates with its own header, but the SDK refuses to
        # build a request until one of its own auth methods resolves, and it only
        # honours an omission passed per-request (not via default_headers). Supply
        # the same key both ways: as api_key to satisfy the SDK, and in the
        # gateway's header, which is what the gateway actually reads. Both go to
        # the gateway host only, so the secret is not sent anywhere new.
        return anthropic.Anthropic(
            base_url=settings.gateway_base_url,
            api_key=settings.gateway_key,
            default_headers={settings.gateway_key_header: settings.gateway_key},
        )
    # Otherwise resolve ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
    # `ant auth login` profile — do not pass a key explicitly.
    return anthropic.Anthropic()


def _translate_auth_error(exc: Exception) -> None:
    """Re-raise a missing-credentials failure as something a reader can act on.

    The SDK resolves credentials lazily at request time and raises a bare
    TypeError naming its own constructor arguments, which tells an operator
    nothing about which variable to set.
    """
    if isinstance(exc, TypeError) and "Could not resolve authentication" in str(exc):
        raise RuntimeError(MISSING_CREDENTIALS_MESSAGE) from exc


def research_badges(
    *, extra_instructions: str | None = None, settings: Settings | None = None
) -> str:
    """Run the research pass. Returns Claude's raw notes."""
    settings = settings or get_settings()
    client = _client(settings)

    prompt = (
        "Find every MongoDB skill badge currently offered.\n"
        f"The authoritative catalog is: {settings.catalog_url}"
    )
    if extra_instructions:
        prompt += f"\n\nAdditional instructions from the operator:\n{extra_instructions}"

    messages: list[dict] = [{"role": "user", "content": prompt}]
    notes: list[str] = []

    # Server-side web search can end a turn with stop_reason "pause_turn";
    # re-send to resume. Cap the resumes so a loop can't run away.
    for _ in range(6):
        try:
            with client.messages.stream(
                model=settings.model,
                max_tokens=32000,
                system=RESEARCH_SYSTEM,
                output_config={"effort": settings.effort},
                tools=[
                    {"type": settings.web_search_tool, "name": "web_search"},
                    {"type": settings.web_fetch_tool, "name": "web_fetch"},
                ],
                messages=messages,
            ) as stream:
                message = stream.get_final_message()
        except Exception as exc:
            _translate_auth_error(exc)
            raise

        notes.extend(b.text for b in message.content if b.type == "text")

        if message.stop_reason == "refusal":
            raise RuntimeError(
                f"Claude declined the research request: {message.stop_details}"
            )
        if message.stop_reason != "pause_turn":
            break

        messages.append({"role": "assistant", "content": message.content})
    else:
        raise RuntimeError("Research turn still paused after 6 resumes; giving up.")

    return "\n".join(notes).strip()


def extract_badges(
    notes: str, *, settings: Settings | None = None
) -> list[DiscoveredBadge]:
    """Turn research notes into validated badge records."""
    settings = settings or get_settings()

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=16000,
            system=EXTRACT_SYSTEM,
            output_format=DiscoveredBadges,
            messages=[{"role": "user", "content": f"Research notes:\n\n{notes}"}],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise
    if response.parsed_output is None:
        raise RuntimeError(
            f"Extraction produced no structured output (stop_reason="
            f"{response.stop_reason}, details={response.stop_details})."
        )
    return response.parsed_output.badges


def discover_badges(
    *, extra_instructions: str | None = None, settings: Settings | None = None
) -> tuple[list[DiscoveredBadge], str]:
    """Full discovery: returns the badges and the notes they came from."""
    notes = research_badges(extra_instructions=extra_instructions, settings=settings)
    return extract_badges(notes, settings=settings), notes


def split_by_catalog_evidence(
    badges: list[DiscoveredBadge], domain: str
) -> tuple[list[DiscoveredBadge], list[DiscoveredBadge]]:
    """Split badges into those evidenced on the catalog domain and those not.

    Earlier runs picked up Credly pages and blog posts describing badges that
    were retired, or that were never skill badges. This is the deterministic
    backstop for that: whatever the prompt says, a badge with no catalog URL does
    not enter the collection.
    """
    kept, rejected = [], []
    for badge in badges:
        if any(domain in (url or "") for url in badge.source_urls):
            kept.append(badge)
        else:
            rejected.append(badge)
    return kept, rejected


def synchronize_badges(
    *,
    extra_instructions: str | None = None,
    settings: Settings | None = None,
) -> dict:
    """Re-pull every badge and reconcile the collection with what was found.

    Discovery is the source of new facts; matching decides identity. Badges whose
    slug is already known are updated in place. Anything with an unknown slug is
    compared against the stored badges by description, so a badge re-discovered
    under a different slug — or whose stored title a human corrected — updates the
    existing record instead of appearing twice.
    """
    from app.repositories import skill_badges
    from app.services.badge_matching import match_discovered_to_existing

    # Settings are only forwarded here; let the callees resolve them so this
    # function needs no configuration of its own.
    badges, notes = discover_badges(
        extra_instructions=extra_instructions, settings=settings
    )

    # Read the domain off the class default when no settings were supplied, so
    # this step needs no configuration of its own.
    domain = settings.catalog_domain if settings else Settings.catalog_domain
    badges, rejected = split_by_catalog_evidence(badges, domain)

    known = skill_badges.known_slugs()
    unknown = [b for b in badges if b.slug not in known]
    matches: dict[str, str] = {}
    if unknown:
        matches = match_discovered_to_existing(
            [b.model_dump() for b in unknown],
            skill_badges.list_badges(),
            settings=settings,
        )

    summary = skill_badges.upsert_badges(badges, matches)
    summary["notes"] = notes
    summary["discovered"] = len(badges)
    summary["badges"] = [b.model_dump() for b in badges]
    summary["discovered"] = len(badges) + len(rejected)
    summary["rejected"] = [
        {"slug": b.slug, "name": b.name, "source_urls": b.source_urls}
        for b in rejected
    ]
    return summary


def _apply_artwork_titles(badges, existing_titles: dict[str, str], settings) -> int:
    """Replace each badge's title with the one printed on its artwork.

    The artwork is the authority: the text titles on Credly and learn.mongodb.com
    disagree with each other and with the badge itself. Titles already read for an
    unchanged image are reused, so a re-sync does not pay for vision again.
    """
    from app.services.badge_titles import read_title_from_image

    read = 0
    for badge in badges:
        if not badge.image_url:
            continue
        cached = existing_titles.get(badge.image_url)
        title = cached or read_title_from_image(badge.image_url, settings=settings)
        if not cached:
            read += 1
        if title:
            badge.image_title = title
            badge.name = title
    return read


def synchronize_from_catalog(*, settings: Settings | None = None) -> dict:
    """Sync the collection with the published Credly badge collection.

    The badge set and its canonical titles come from the collection itself. Claude
    is used only to recognise badges already stored under a different slug, so a
    hand-corrected record is updated rather than duplicated.
    """
    from app.repositories import skill_badges
    from app.services.badge_matching import match_discovered_to_existing
    from app.services.credly_catalog import fetch_catalog

    badges = fetch_catalog(settings=settings)

    # Titles come from the badge artwork, not the catalog's text.
    existing_titles = {
        doc["image_url"]: doc["image_title"]
        for doc in skill_badges.list_badges()
        if doc.get("image_url") and doc.get("image_title")
    }
    titles_read = _apply_artwork_titles(badges, existing_titles, settings)

    # Identity follows the artwork title, not the catalog's wording, so the slug is
    # derived after the artwork has been read.
    for badge in badges:
        if badge.image_title:
            badge.slug = skill_badges.slug_from_name(badge.image_title) or badge.slug

    known = skill_badges.known_slugs()
    unknown = [b for b in badges if b.slug not in known]
    matches: dict[str, str] = {}
    if unknown:
        matches = match_discovered_to_existing(
            [b.model_dump() for b in unknown],
            skill_badges.list_badges(),
            settings=settings,
        )

    summary = skill_badges.upsert_badges(badges, matches)
    summary["discovered"] = len(badges)
    summary["badges"] = [b.model_dump() for b in badges]
    summary["rejected"] = []
    summary["source"] = "credly-collection"
    summary["artwork_titles_read"] = titles_read
    summary.update(refresh_artwork())
    summary.update(verify_credly_titles())
    summary.update(verify_mongodb_titles())
    return summary


def refresh_artwork() -> dict:
    """Store the badge artwork for any badge whose image is missing or moved.

    Failures are collected rather than raised: artwork is an aid to recognising a
    badge, so one unreachable image must not fail a sync that otherwise succeeded.
    """
    from app.repositories import skill_badges
    from app.services.badge_art import fetch_image

    stored, failures = 0, []
    for doc in skill_badges.badges_needing_art():
        try:
            data, content_type = fetch_image(doc["image_url"])
        except Exception as exc:
            failures.append({"slug": doc["slug"], "error": str(exc)})
            continue
        skill_badges.set_image(doc["slug"], data, content_type, doc["image_url"])
        stored += 1
    return {"artwork_stored": stored, "artwork_failures": failures}


def verify_credly_titles() -> dict:
    """Read each badge's title from its own Credly page and record it.

    Three sources name these badges differently — the collection API, the artwork,
    and the badge's own page — so the page title is captured separately rather than
    reconciled away. Failures are collected: an unreachable page must not fail a
    sync that otherwise succeeded.
    """
    from app.repositories import skill_badges
    from app.services.credly_page import fetch_page_title

    verified, failures = 0, []
    for doc in skill_badges.badges_needing_credly_title():
        try:
            title = fetch_page_title(doc["credly_url"])
        except Exception as exc:
            failures.append({"slug": doc["slug"], "error": str(exc)})
            continue
        skill_badges.set_credly_title(doc["slug"], title, doc["credly_url"])
        verified += 1
    return {"credly_titles_verified": verified, "credly_title_failures": failures}


def verify_mongodb_titles(*, settings: Settings | None = None) -> dict:
    """Record the title learn.mongodb.com publishes for each badge.

    Looked up by the badge's own learn.mongodb.com URL. Failures are collected so
    one unindexed page does not fail a sync.
    """
    from app.repositories import skill_badges
    from app.services.mongodb_page import fetch_indexed_title

    verified, missing, failures = 0, [], []
    for doc in skill_badges.badges_needing_mongodb_title():
        try:
            found = fetch_indexed_title(
                doc["mongodb_url"], doc.get("name"), settings=settings
            )
        except Exception as exc:
            failures.append({"slug": doc["slug"], "error": str(exc)})
            continue
        if not found:
            missing.append(doc["slug"])
            continue
        title, _ = found
        skill_badges.set_mongodb_title(doc["slug"], title, doc["mongodb_url"])
        verified += 1
    return {
        "mongodb_titles_verified": verified,
        "mongodb_titles_not_found": missing,
        "mongodb_title_failures": failures,
    }