"""Tests for app/routers/admin_pages.py — the server-rendered admin screens.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.skill_badge import DiscoveredBadge
from app.repositories import skill_badges
from app.routers import admin_skill_badges as api_module


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    """The page footer names the storage target, so settings must resolve."""
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def reset_run_state():
    api_module._run_state.update(
        running=False, last_result=None, last_error=None, last_traceback=None
    )
    yield
    api_module._run_state.update(
        running=False, last_result=None, last_error=None, last_traceback=None
    )


def seed(**overrides) -> None:
    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                **{
                    "slug": "atlas-search",
                    "name": "Atlas Search",
                    "description": "Covers Atlas Search indexes and queries.",
                    "confidence": "high",
                    "categories": ["search"],
                    "source_urls": ["https://learn.mongodb.com/atlas-search"],
                    **overrides,
                }
            )
        ]
    )


# --- navigation ---


def test_admin_home_opens_the_badge_catalog(client, fake_collection):
    """
    Intent: /admin is the curation area — the functions that maintain the badge
        catalog. Someone typing it should land on that area's screen rather than a
        404, and not be bounced out to the authoring surface at the site root.
    Success: GET /admin follows through to the skill badges page and returns HTML.
    Feature: Admin area — navigation entry point.
    """
    response = client.get("/admin")
    assert response.status_code == 200
    assert response.url.path == "/admin/skill-badges"
    assert "text/html" in response.headers["content-type"]


def test_page_renders_the_shared_shell(client, fake_collection):
    """
    Intent: The badge screen is the first of several planned admin screens, so it
        must render inside the shared navigation shell rather than as a standalone
        page.
    Success: The response contains the app brand, the Skill badges nav link, and the
        placeholder for the future Questions screen.
    Feature: Admin area — shared navigation shell.
    """
    body = client.get("/admin/skill-badges").text
    assert "Skill Badge Questions" in body
    assert 'href="/admin/skill-badges"' in body
    assert "Questions" in body


# --- badge table ---


def test_page_lists_discovered_badges_with_review_detail(client, fake_collection):
    """
    Intent: The table is where a human decides whether to trust a badge, so it must
        show the identifying fields plus the evidence — categories, confidence, and
        the source URLs Claude cited.
    Success: Name, slug, description, category, confidence, source URL, and status
        all appear in the rendered page.
    Feature: Admin area — badge review table.
    """
    seed()
    body = client.get("/admin/skill-badges").text
    assert "Atlas Search" in body
    assert "atlas-search" in body
    assert "Covers Atlas Search indexes and queries." in body
    assert "search" in body
    assert "high" in body
    assert "https://learn.mongodb.com/atlas-search" in body
    assert "candidate" in body


def test_empty_state_tells_the_user_how_to_populate_the_collection(
    client, fake_collection
):
    """
    Intent: On a fresh install the screen must explain the next action rather than
        showing a bare empty table that looks broken.
    Success: The page points the user at the Discover badges action.
    Feature: Admin area — empty state guidance.
    """
    body = client.get("/admin/skill-badges").text
    assert "No skill badges yet" in body
    assert "Discover badges" in body


def test_page_names_the_storage_target(client, fake_collection):
    """
    Intent: An operator needs to know which database and collection the screen is
        writing to before approving anything, especially with several Atlas clusters
        in play.
    Success: The configured database and collection names appear on the page.
    Feature: Admin area — storage transparency.
    """
    body = client.get("/admin/skill-badges").text
    assert "skill-badge-questions" in body
    assert "skill_badges" in body


# --- filtering ---


def test_status_filter_narrows_the_table(client, fake_collection):
    """
    Intent: Reviewers work a queue, so the screen must be able to show only the
        badges in one review state.
    Success: ?status=approved excludes the candidate badge; ?status=candidate
        includes it.
    Feature: Admin area — badge review filtering.
    """
    seed()
    approved_only = client.get("/admin/skill-badges", params={"status": "approved"}).text
    candidates = client.get("/admin/skill-badges", params={"status": "candidate"}).text
    assert "Atlas Search" not in approved_only
    assert "Atlas Search" in candidates


def test_filtered_empty_state_names_the_filter(client, fake_collection):
    """
    Intent: An empty filtered view must be distinguishable from an empty database, or
        a reviewer will think discovery failed.
    Success: The filtered empty view names the status rather than telling the user to
        run discovery.
    Feature: Admin area — empty state guidance.
    """
    seed()
    body = client.get("/admin/skill-badges", params={"status": "retired"}).text
    assert "retired" in body
    assert "No skill badges yet" not in body


def test_tab_counts_reflect_each_review_state(client, fake_collection):
    """
    Intent: The tab counts are how a reviewer sees how much work is pending, so they
        must count all badges per state — not just the ones in the current view.
    Success: With one approved and one candidate badge, the counts shown are all=2,
        candidate=1, approved=1 while filtered to candidates.
    Feature: Admin area — review queue counts.
    """
    seed()
    seed(slug="data-modeling", name="Data Modeling")
    skill_badges.set_status("data-modeling", "approved")

    body = client.get("/admin/skill-badges", params={"status": "candidate"}).text
    counts = dict(
        (label, int(count))
        for label, count in re.findall(
            r'(All|Candidates|Approved|Retired) <span class="badge [^"]*">(\d+)</span>',
            body,
        )
    )
    assert counts == {"All": 2, "Candidates": 1, "Approved": 1, "Retired": 0}
    # The filtered view itself still shows only the candidate.
    assert "Atlas Search" in body
    assert "Data Modeling" not in body


# --- action wiring ---


def test_page_offers_the_review_actions_for_the_current_state(client, fake_collection):
    """
    Intent: The actions must match the badge's state — offering "Approve" on an
        already-approved badge is noise, and the reviewer needs a way back to
        candidate after a mistake.
    Success: A candidate row offers Approve and Retire but not Re-open; an approved
        row offers Retire and Re-open but not Approve.
    Feature: Admin area — promote / retire actions.
    """
    def actions(body: str) -> set[str]:
        return set(re.findall(r'js-status" data-status="(\w+)"', body))

    seed()
    assert actions(client.get("/admin/skill-badges").text) == {"approved", "retired"}

    skill_badges.set_status("atlas-search", "approved")
    assert actions(client.get("/admin/skill-badges").text) == {"retired", "candidate"}


def test_page_targets_the_json_api_it_depends_on(client, fake_collection):
    """
    Intent: The buttons call the JSON API by URL from the browser. If the API prefix
        moves and the template is not updated, every action silently 404s, so the
        page must reference the API path the app actually serves.
    Success: The rendered page references /api/admin/skill-badges, and that path is
        present in the app's OpenAPI schema.
    Feature: Admin area — page-to-API wiring.
    """
    body = client.get("/admin/skill-badges").text
    assert "/api/admin/skill-badges" in body
    assert "/api/admin/skill-badges/discover" in set(
        client.get("/openapi.json").json()["paths"]
    )


def test_page_shows_the_last_run_summary(client, fake_collection):
    """
    Intent: After a run finishes the reviewer needs to know what it changed, and a
        reload must not lose that summary.
    Success: The inserted and updated counts from the last run appear on the page.
    Feature: Admin area — discovery run feedback.
    """
    api_module._run_state["last_result"] = {
        "run_id": "abc123def456",
        "inserted": 3,
        "modified": 1,
        "matched": 1,
        "slugs": [],
    }
    body = client.get("/admin/skill-badges").text
    assert "3 new" in body
    assert "1 updated" in body


def test_page_shows_a_failed_run_error(client, fake_collection):
    """
    Intent: A failed run must be visible on the screen itself — otherwise an empty
        table reads as "MongoDB has no badges" instead of "the run broke".
    Success: The error text from the last run is rendered on the page.
    Feature: Admin area — discovery run error reporting.
    """
    api_module._run_state["last_error"] = "Claude declined the research request"
    body = client.get("/admin/skill-badges").text
    assert "Claude declined the research request" in body


def test_page_resumes_polling_when_a_run_is_already_going(client, fake_collection):
    """
    Intent: A run outlives the request that started it, so a reload mid-run must
        resume polling rather than looking idle and inviting a second run.
    Success: The rendered page enables the poll-on-load branch.
    Feature: Admin area — discovery run feedback.
    """
    api_module._run_state["running"] = True
    body = client.get("/admin/skill-badges").text
    assert "if (true) { pollStatus(); }" in body


def test_page_escapes_values_from_claude(client, fake_collection):
    """
    Intent: Badge names and descriptions come from a model reading arbitrary web
        pages. They are untrusted input and must never be rendered as live markup.
    Success: An injected script tag appears escaped, not as an executable tag.
    Feature: Admin area — output escaping of model-supplied text.
    """
    seed(name="<script>alert('x')</script>")
    body = client.get("/admin/skill-badges").text
    assert "<script>alert('x')</script>" not in body
    assert "&lt;script&gt;" in body


def test_page_reports_an_unreachable_database_instead_of_a_stack_trace(
    client, monkeypatch
):
    """
    Intent: A wrong connection string or a missing Atlas access-list entry is the
        likeliest setup mistake. The screen must name that cause and stay usable
        rather than returning a 500 the user has to read server logs to understand.
    Success: The page returns 200 and names both the failure and the environment
        variable to check.
    Feature: Admin area — storage connectivity diagnostics.
    """
    from pymongo.errors import ServerSelectionTimeoutError

    def unreachable(*args, **kwargs):
        raise ServerSelectionTimeoutError("no replica set members available")

    monkeypatch.setattr(skill_badges, "list_badges", unreachable)

    response = client.get("/admin/skill-badges")
    assert response.status_code == 200
    assert "Cannot reach MongoDB" in response.text
    assert "PTM_HACKATHON_CONNECTION_STRING" in response.text
    assert "no replica set members available" in response.text


def test_page_offers_the_stack_trace_of_a_failed_run(client, fake_collection):
    """
    Intent: The error message alone is often not enough to diagnose a failed run, so
        the page must offer the trace inline — collapsed by default so it does not
        bury the actionable message.
    Success: The page renders a collapsible "Show stack trace" detail containing the
        trace text.
    Feature: Admin area — discovery run error reporting.
    """
    api_module._run_state["last_error"] = "No Anthropic credentials found."
    api_module._run_state["last_traceback"] = (
        'Traceback (most recent call last):\n  File "x.py", line 1\nTypeError: nope'
    )
    body = client.get("/admin/skill-badges").text
    assert "Show stack trace" in body
    assert "<details" in body
    assert "TypeError: nope" in body


def test_page_omits_the_trace_section_when_there_is_none(client, fake_collection):
    """
    Intent: An error recorded without a trace (or no error at all) must not render an
        empty "Show stack trace" control that expands to nothing.
    Success: With an error but no traceback, the page shows the message and no
        stack-trace control.
    Feature: Admin area — discovery run error reporting.
    """
    api_module._run_state["last_error"] = "No Anthropic credentials found."
    api_module._run_state["last_traceback"] = None
    body = client.get("/admin/skill-badges").text
    assert "No Anthropic credentials found." in body
    # The JS poll path also contains this label, so assert on the server-rendered
    # element rather than the bare string.
    assert "<details" not in body


def test_the_table_offers_a_title_edit_control(client, fake_collection):
    """
    Intent: Some researched titles are wrong, and the reviewer needs to fix them where
        they see them rather than through the API or the database.
    Success: Each badge row renders an edit control wired to the rename endpoint.
    Feature: Admin area — editing a badge title.
    """
    seed()
    body = client.get("/admin/skill-badges").text
    assert "js-edit-name" in body
    assert "/name" in body


def test_a_corrected_title_is_marked_as_protected(client, fake_collection):
    """
    Intent: A reviewer must be able to see at a glance which titles are hand-corrected
        and therefore will not be overwritten by the next run — otherwise they cannot
        tell researched data from reviewed data.
    Success: A badge with a locked name renders an "edited" marker; one without does
        not.
    Feature: Admin area — hand-corrected titles are visible.
    """
    # Assert on the marker element: the word "edited" also appears in the page's
    # JavaScript, so a bare string check would pass either way.
    seed()
    assert 'data-name-locked="true"' not in client.get("/admin/skill-badges").text

    skill_badges.set_name("atlas-search", "Atlas Search Fundamentals")
    body = client.get("/admin/skill-badges").text
    assert 'data-name-locked="true"' in body
    assert "Atlas Search Fundamentals" in body


def test_the_page_names_the_catalog_that_badges_are_verified_against(
    client, fake_collection
):
    """
    Intent: A reviewer needs to know which source the program treats as authoritative,
        so they can check a questionable badge against it themselves rather than
        trusting the row.
    Success: The page links the catalog URL and names the catalog domain.
    Feature: Admin area — provenance transparency.
    """
    body = client.get("/admin/skill-badges").text
    assert "https://learn.mongodb.com/skills?team=devrel" in body
    assert "learn.mongodb.com" in body


def test_rejected_candidates_are_shown_with_the_reason(client, fake_collection):
    """
    Intent: Silently discarding candidates would hide both real retirements and a
        broken filter. The reviewer must see what a run threw away and what it cited,
        so a wrongly rejected badge can be caught.
    Success: The rejected candidate, its slug and its off-catalog citation all appear
        on the page.
    Feature: Admin area — visibility of rejected candidates.
    """
    api_module._run_state["last_result"] = {
        "run_id": "abc123def456",
        "inserted": 1,
        "modified": 0,
        "matched": 0,
        "merged": {},
        "rejected": [
            {
                "slug": "retired-thing",
                "name": "Retired Thing",
                "source_urls": ["https://www.credly.com/org/mongodb/badge/retired"],
            }
        ],
    }
    body = client.get("/admin/skill-badges").text
    assert 'data-rejected="true"' in body
    assert "Retired Thing" in body
    assert "credly.com" in body


def test_the_table_offers_a_link_editor(client, fake_collection):
    """
    Intent: Reference links are the evidence a reviewer judges a badge by, so they must
        be editable where they are shown — a wrong link is as misleading as a wrong
        title.
    Success: Each row renders a link-editing control and carries its current links in
        an editable data attribute.
    Feature: Admin area — managing badge reference links.
    """
    seed()
    body = client.get("/admin/skill-badges").text
    assert "js-edit-sources" in body
    assert 'data-sources="https://learn.mongodb.com/atlas-search"' in body


def test_curated_links_are_marked_as_protected(client, fake_collection):
    """
    Intent: A reviewer must see which badges have curated links, so they can tell
        reviewed evidence from whatever the last run happened to cite.
    Success: A badge with locked sources renders the curated marker; one without does
        not.
    Feature: Admin area — curated links are visible.
    """
    seed()
    assert 'data-sources-locked="true"' not in client.get("/admin/skill-badges").text

    skill_badges.set_source_urls("atlas-search", ["https://learn.mongodb.com/skills"])
    assert 'data-sources-locked="true"' in client.get("/admin/skill-badges").text


def test_delete_is_offered_only_on_retired_badges(client, fake_collection):
    """
    Intent: Deletion is the only irreversible action on this screen, so it must not sit
        next to Approve in the candidate queue where a mis-click destroys a badge. It
        belongs only where a reviewer has already made the reversible decision.
    Success: A candidate row renders no delete control; the same badge once retired
        does.
    Feature: Admin area — deletion requires retirement first.
    """
    seed()
    assert 'data-delete="true"' not in client.get("/admin/skill-badges").text

    skill_badges.set_status("atlas-search", "retired")
    assert 'data-delete="true"' in client.get("/admin/skill-badges").text


def test_the_page_offers_both_the_catalog_sync_and_research(client, fake_collection):
    """
    Intent: The two actions differ in authority — the catalog sync is the source of
        truth, research is for what the catalog may not list yet — so both must be
        available and distinguishable on the screen.
    Success: Both controls render, and the page explains that the catalog is
        authoritative.
    Feature: Admin area — sync from the published collection.
    """
    body = client.get("/admin/skill-badges").text
    assert 'id="sync-catalog-btn"' in body
    assert 'id="discover-btn"' in body
    assert "authoritative" in body


def test_both_canonical_links_are_shown_on_the_row(client, fake_collection):
    """
    Intent: The truth about a badge lives on its Credly page and its learn.mongodb.com
        page, and the titles there disagree — so a reviewer needs one click to each,
        distinguishable from the researched source links.
    Success: Both canonical links render, each labelled by which site it points at.
    Feature: Admin area — canonical links.
    """
    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                slug="mongodb-overview",
                name="MongoDB Overview",
                description="d",
                confidence="high",
                credly_url="https://www.credly.com/org/mongodb/badge/overview",
                mongodb_url="https://learn.mongodb.com/courses/overview",
            )
        ]
    )
    body = client.get("/admin/skill-badges").text
    assert 'data-canonical="credly"' in body
    assert 'data-canonical="mongodb"' in body


def test_a_title_taken_from_artwork_is_flagged(client, fake_collection):
    """
    Intent: When the artwork title differs from the catalog listing, the reviewer must see
        that the shown name came from the artwork and what the catalog says — otherwise a
        name that matches neither search result looks like a bug.
    Success: The row notes the artwork title was used and quotes the catalog title.
    Feature: Admin area — title provenance.
    """
    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                slug="mongodb-overview",
                name="MongoDB Overview",
                image_title="MongoDB Overview",
                text_title="MongoDB Overview: Core Concepts and Architecture",
                description="d",
                confidence="high",
            )
        ]
    )
    body = client.get("/admin/skill-badges").text
    assert 'data-title-source="artwork"' in body
    assert "Core Concepts and Architecture" in body


def test_the_page_offers_a_duplicate_scan(client, fake_collection):
    """
    Intent: Duplicate resolution has to be reachable from the screen where the duplicates
        are visible, not only through the API.
    Success: The scan control renders.
    Feature: Admin area — duplicate scan.
    """
    assert 'id="scan-duplicates-btn"' in client.get("/admin/skill-badges").text


def test_pairs_needing_review_are_listed_with_a_merge_action(client, fake_collection):
    """
    Intent: Pairs the program is not confident about must be resolvable by a person, with
        enough context to decide — which record survives, which is dropped, and why the
        program thought they matched.
    Success: The pair, its reason, and a merge control all render.
    Feature: Admin area — merging duplicates by hand.
    """
    api_module._run_state["last_result"] = {
        "run_id": "abc123def456",
        "source": "duplicate-scan",
        "merged": [],
        "needs_review": [
            {
                "keep": "atlas-search-fundamentals",
                "drop": "search-with-mongodb",
                "keep_name": "Atlas Search Fundamentals",
                "drop_name": "Search with MongoDB",
                "reason": "Both cover $search queries.",
                "confident": False,
            }
        ],
    }
    body = client.get("/admin/skill-badges").text
    assert 'data-needs-review="true"' in body
    assert "Both cover $search queries." in body
    assert 'data-keep="atlas-search-fundamentals"' in body
    assert 'data-drop="search-with-mongodb"' in body


def test_the_artwork_is_shown_beside_the_badge_text(client, fake_collection):
    """
    Intent: The artwork identifies a badge more reliably than any of its titles, which
        disagree across sources — so it belongs beside the name where a reviewer reads it.
    Success: The row renders an image pointing at this app's own artwork endpoint.
    Feature: Admin area — badge artwork.
    """
    seed()
    skill_badges.set_image(
        "atlas-search", b"\x89PNG", "image/png", "https://images.credly.com/x"
    )

    body = client.get("/admin/skill-badges").text
    assert 'data-badge-art="true"' in body
    assert "/api/admin/skill-badges/atlas-search/image" in body


def test_a_badge_without_artwork_shows_a_placeholder(client, fake_collection):
    """
    Intent: A missing image must be visible as missing, keeping the row aligned with the
        others rather than collapsing or showing a broken-image icon.
    Success: The placeholder renders and no artwork image tag is emitted.
    Feature: Admin area — badge artwork.
    """
    seed()
    body = client.get("/admin/skill-badges").text
    assert 'data-badge-art="missing"' in body
    assert 'data-badge-art="true"' not in body


def test_a_differing_credly_page_title_is_shown(client, fake_collection):
    """
    Intent: The badge's Credly page names it differently from the title in use, and a
        reviewer checking a credential link needs to see that name without leaving the
        screen.
    Success: The Credly page title is rendered when it differs from the badge's name.
    Feature: Admin area — title provenance.
    """
    seed()
    skill_badges.set_credly_title(
        "atlas-search", "Search with MongoDB", "https://www.credly.com/org/mongodb/badge/x"
    )

    body = client.get("/admin/skill-badges").text
    assert 'data-credly-title="true"' in body
    assert "Search with MongoDB" in body


def test_a_differing_mongodb_title_is_shown(client, fake_collection):
    """
    Intent: MongoDB publishes its own name for each badge, which differs from both the
        title in use and Credly's. A reviewer comparing sources needs to see it on the row.
    Success: The MongoDB title renders when it differs from the badge's name.
    Feature: Admin area — title provenance.
    """
    seed()
    skill_badges.set_mongodb_title(
        "atlas-search",
        "Search Fundamentals Skill Badge",
        "https://learn.mongodb.com/courses/search-fundamentals",
    )
    body = client.get("/admin/skill-badges").text
    assert 'data-mongodb-title="true"' in body
    assert "Search Fundamentals Skill Badge" in body


def test_every_name_source_is_labelled(client, fake_collection):
    """
    Intent: A row previously ran four differently-worded names and the description together
        as unlabelled grey text, which a reviewer could not tell apart. Each name source
        must carry a visible label saying where it came from.
    Success: The row labels the Credly, MongoDB, artwork and catalog names and the
        description.
    Feature: Admin area — labelled name sources.
    """
    seed(text_title="Search with MongoDB Skill Badge", image_title="Atlas Search")
    skill_badges.set_credly_title(
        "atlas-search", "Search with MongoDB", "https://www.credly.com/x"
    )
    skill_badges.set_mongodb_title(
        "atlas-search", "Search Fundamentals Skill Badge", "https://learn.mongodb.com/x"
    )

    body = client.get("/admin/skill-badges").text
    for label in ("Credly name", "MongoDB name", "Artwork name", "Catalog name", "Description"):
        assert label in body, label


def test_the_mongodb_name_row_appears_even_when_unknown(client, fake_collection):
    """
    Intent: MongoDB publishes no reachable title for some badges. Hiding the row makes that
        indistinguishable from a row the reviewer simply has not scrolled to, so the label
        must always appear and say the title was not found.
    Success: The row renders with a missing-value marker when no MongoDB title is stored,
        and with the title when one is.
    Feature: Admin area — labelled name sources.
    """
    seed()
    body = client.get("/admin/skill-badges").text
    assert "MongoDB name" in body
    assert 'data-mongodb-title="missing"' in body

    skill_badges.set_mongodb_title(
        "atlas-search", "Search Fundamentals Skill Badge", "https://learn.mongodb.com/x"
    )
    body = client.get("/admin/skill-badges").text
    assert 'data-mongodb-title="true"' in body
    assert 'data-mongodb-title="missing"' not in body


def test_the_page_offers_slug_normalisation(client, fake_collection):
    """
    Intent: The action has to be reachable from the screen where slugs are visible, so a
        reviewer can apply the rule after editing titles.
    Success: The control renders.
    Feature: Admin area — normalising slugs.
    """
    assert 'id="normalise-slugs-btn"' in client.get("/admin/skill-badges").text


def test_the_badge_screens_elapsed_timer_is_driven_by_the_servers_start_time(
    client, fake_collection
):
    """
    Intent: The badge screen times its runs with the same mechanism as the questions
        screen and had the same defect: the start time lived only in page memory, so
        navigating away and back restarted the count at 00:00 while the sync continued.
        Fixing one screen and not the other would leave the same misleading timer here.
    Success: The badge screen's polling adopts the server's start time and clock, and
        does not seed the timer from the browser's own clock.
    Feature: Badge discovery — elapsed time survives leaving the page.
    """
    body = client.get("/admin/skill-badges").text
    assert "adoptServerClock(state)" in body
    assert "state.started_at" in body
    assert "startedAt = Date.now()" not in body


def test_a_badge_run_reports_when_it_started_and_finished(client, fake_collection, monkeypatch):
    """
    Intent: The badge screen's timer can only survive a reload if the run's start and end
        are recorded on the server, as they are for question generation.
    Success: A completed catalog sync reports started_at and finished_at, in that order.
    Feature: Badge discovery — runs are timestamped on the server.
    """
    monkeypatch.setattr(api_module, "synchronize_from_catalog", lambda: {"inserted": 0})
    client.post("/api/admin/skill-badges/sync-catalog")
    state = client.get("/api/admin/skill-badges/discover/status").json()
    assert state["finished_at"] >= state["started_at"]
    assert state["running"] is False


def test_each_badge_row_can_be_linked_to_directly(client, fake_collection):
    """
    Intent: A question's badge tag links to that badge's definition. Landing at the top of a
        34-row table leaves the reader to find the row themselves, which is the work the link
        was supposed to save.
    Success: Each badge row carries an id derived from its slug.
    Feature: Badge review screen — a badge row is directly linkable.
    """
    fake_collection.docs.append(
        {"slug": "atlas-search", "name": "Atlas Search", "status": "approved"}
    )
    body = client.get("/admin/skill-badges").text
    assert 'id="badge-atlas-search"' in body


# --- rendering the canonical page ---


def test_the_canonical_page_is_fetched_and_rendered(client, monkeypatch, fake_doc_pages):
    """
    Intent: MongoDB serves these pages as raw Markdown, which a browser shows as unformatted
        text — so following a question's citation lands on something hard to read. Rendering it
        with the viewer the stored copy uses makes a citation actually checkable.
    Success: The route fetches the page and renders its content.
    Feature: Documentation rendering — the canonical page is readable.
    """
    from app.services import doc_corpus

    monkeypatch.setattr(
        doc_corpus, "_get", lambda url, s: "# Replication\n\nHow replica sets work."
    )
    body = client.get(
        "/admin/docs/render",
        params={"url": "https://www.mongodb.com/docs/manual/replication.md"},
    ).text
    assert "Replication" in body
    assert "How replica sets work." in body


def test_the_live_view_says_it_is_not_the_stored_copy(client, monkeypatch, fake_doc_pages):
    """
    Intent: The stored copy is the snapshot a question was written from and the live page is
        what MongoDB publishes today; after a docs refresh they can differ. A reader checking a
        question needs to know which one they are looking at, or a divergence looks like a
        wrong question.
    Success: The live view says so, and offers the stored copy when we have one.
    Feature: Documentation rendering — live and stored are distinguished.
    """
    from app.repositories import doc_pages
    from app.services import doc_corpus

    url = "https://www.mongodb.com/docs/manual/replication.md"
    doc_pages.upsert_pages([{"url": url, "source": "ix", "title": "Replication", "text": "old"}])
    monkeypatch.setattr(doc_corpus, "_get", lambda u, s: "# Replication\n\nnew text")
    body = client.get("/admin/docs/render", params={"url": url}).text
    assert 'data-live-notice="true"' in body
    assert "/admin/docs/page?url=" in body


def test_a_page_we_never_stored_is_still_renderable(client, monkeypatch, fake_doc_pages):
    """
    Intent: A citation can name a page the corpus no longer holds — the docs were refreshed, or
        the question came from the research fallback. Refusing to render it would break the
        citation for exactly the questions hardest to check.
    Success: A page absent from the corpus renders, and says no question came from it.
    Feature: Documentation rendering — works without a stored copy.
    """
    from app.services import doc_corpus

    monkeypatch.setattr(doc_corpus, "_get", lambda u, s: "# Sharding\n\nChunks move.")
    body = client.get(
        "/admin/docs/render",
        params={"url": "https://www.mongodb.com/docs/manual/sharding.md"},
    ).text
    assert "Chunks move." in body
    assert "not in our corpus" in body


def test_rendering_a_url_off_the_documentation_host_is_refused(client, fake_doc_pages):
    """
    Intent: This route fetches whatever URL it is given, server-side. Left open it would reach
        anything the server can reach and hand the response back — an internal service, a cloud
        metadata endpoint. The refusal has to be at the route, not only in the fetcher.
    Success: A URL off the documentation host is a 400.
    Feature: Documentation rendering — the route refuses foreign hosts.
    """
    response = client.get(
        "/admin/docs/render", params={"url": "http://169.254.169.254/latest/meta-data/"}
    )
    assert response.status_code == 400


def test_a_failed_fetch_is_explained_with_a_way_out(client, monkeypatch, fake_doc_pages):
    """
    Intent: The docs sit behind CloudFront, which refuses a caller that has asked for too much
        — the same refusal the crawl has to handle. A stack trace would read as a bug in this
        program rather than a pause to wait out.
    Success: A failed fetch is explained, with links to the raw page and the stored copy.
    Feature: Documentation rendering — a refusal is reported, not crashed on.
    """
    from app.repositories import doc_pages
    from app.services import doc_corpus

    url = "https://www.mongodb.com/docs/manual/replication.md"
    doc_pages.upsert_pages([{"url": url, "source": "ix", "title": "Replication", "text": "old"}])

    def refuse(*args, **kwargs):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(doc_corpus, "_get", refuse)
    body = client.get("/admin/docs/render", params={"url": url}).text
    assert 'data-fetch-error="true"' in body
    assert "403 Forbidden" in body
    assert "/admin/docs/page?url=" in body


def test_the_corpus_screen_shows_how_it_is_chunked(client, fake_collection, fake_doc_pages):
    """
    Intent: Questions are written from sections, not whole pages, so how the corpus is cut is
        part of what the corpus is. The band is a measured judgement and the screen is where
        anyone would look to see what it produced.
    Success: The corpus screen carries a section summary and a re-chunk control.
    Feature: Documentation corpus screen — the chunking is visible and rebuildable.
    """
    body = client.get("/admin/docs").text
    assert 'data-chunk-summary="true"' in body
    assert 'id="rechunk-btn"' in body
