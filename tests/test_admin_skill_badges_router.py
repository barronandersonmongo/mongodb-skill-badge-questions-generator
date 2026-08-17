"""Tests for app/routers/admin_skill_badges.py and app/main.py — admin API.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.skill_badge import DiscoveredBadge
from app.repositories import skill_badges
from app.routers import admin_skill_badges as router_module
from app.services import badge_discovery, badge_matching

BADGE = DiscoveredBadge(
    slug="atlas-search",
    name="Atlas Search",
    description="Covers Atlas Search.",
    confidence="high",
    source_urls=["https://learn.mongodb.com/skills/atlas-search"],
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_run_state():
    router_module._run_state.update(
        running=False, last_result=None, last_error=None, last_traceback=None
    )
    yield
    router_module._run_state.update(
        running=False, last_result=None, last_error=None, last_traceback=None
    )


@pytest.fixture
def stub_discovery(monkeypatch):
    """Stub only the Claude calls; the real sync and MongoDB writes still run."""
    calls: list[str | None] = []

    def install(badges=(BADGE,), notes="notes", error: Exception | None = None):
        def fake_discover(*, extra_instructions=None, settings=None):
            calls.append(extra_instructions)
            if error is not None:
                raise error
            return list(badges), notes

        monkeypatch.setattr(badge_discovery, "discover_badges", fake_discover)
        monkeypatch.setattr(
            badge_matching, "match_discovered_to_existing", lambda *a, **k: {}
        )
        return calls

    return install


# --- routing ---


def test_admin_routes_are_mounted(client):
    """
    Intent: The discovery module is only usable if its endpoints are actually wired
        into the app the admin area calls.
    Success: All four admin skill-badge paths appear in the OpenAPI schema.
    Feature: Admin area — skill badge endpoints.
    """
    paths = client.get("/openapi.json").json()["paths"]
    assert {
        "/api/admin/skill-badges",
        "/api/admin/skill-badges/discover",
        "/api/admin/skill-badges/discover/status",
        "/api/admin/skill-badges/{slug}/status",
    } <= set(paths)


def test_healthz(client):
    """
    Intent: A liveness endpoint lets a developer or deploy check confirm the app
        booted without exercising Atlas or Claude.
    Success: GET /healthz returns {"ok": True}.
    Feature: Application — health check.
    """
    assert client.get("/healthz").json() == {"ok": True}


# --- POST /discover ---


def test_discover_runs_and_writes_the_badges(client, fake_collection, stub_discovery):
    """
    Intent: Triggering discovery from the admin area must run the pipeline, persist
        the badges, and leave a result the page can poll and display.
    Success: 200 with {"started": True}; the badge is stored; status reports not
        running, no error, 1 inserted, and the research notes.
    Feature: Admin area — trigger a discovery run.
    """
    stub_discovery()
    response = client.post("/api/admin/skill-badges/discover", json={})

    assert response.status_code == 200
    assert response.json() == {"started": True}
    # TestClient runs background tasks before returning.
    assert fake_collection.count_documents({}) == 1

    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert status["running"] is False
    assert status["last_error"] is None
    assert status["last_result"]["inserted"] == 1
    assert status["last_result"]["notes"] == "notes"


def test_discover_forwards_extra_instructions(client, fake_collection, stub_discovery):
    """
    Intent: An admin narrowing a run from the UI must have that instruction reach
        the discovery service, not be dropped at the HTTP boundary.
    Success: The service receives exactly the submitted instruction text.
    Feature: Admin area — operator-steered discovery.
    """
    calls = stub_discovery()
    client.post(
        "/api/admin/skill-badges/discover", json={"extra_instructions": "Atlas only."}
    )
    assert calls == ["Atlas only."]


def test_discover_accepts_an_empty_body(client, fake_collection, stub_discovery):
    """
    Intent: The common case is an unsteered "find everything" run, so instructions
        must be optional.
    Success: 200 with an empty body, and the service is called with no instructions.
    Feature: Admin area — trigger a discovery run.
    """
    calls = stub_discovery()
    assert client.post("/api/admin/skill-badges/discover", json={}).status_code == 200
    assert calls == [None]


def test_discovery_failure_is_surfaced_not_swallowed(
    client, fake_collection, stub_discovery
):
    """
    Intent: A failed background run must be visible to the admin — a silent failure
        looks identical to "MongoDB has no badges", which would mislead a reviewer.
    Success: The status endpoint reports the error text, and nothing was written.
    Feature: Admin area — discovery run error reporting.
    """
    stub_discovery(error=RuntimeError("Claude declined the research request"))
    client.post("/api/admin/skill-badges/discover", json={})

    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert status["running"] is False
    assert "declined" in status["last_error"]
    assert fake_collection.count_documents({}) == 0


def test_a_failed_run_clears_the_previous_error(client, fake_collection, stub_discovery):
    """
    Intent: A stale error must not persist after a later run succeeds, or the admin
        page would keep showing a failure that no longer applies.
    Success: After a failed run followed by a successful one, last_error is None.
    Feature: Admin area — discovery run error reporting.
    """
    stub_discovery(error=RuntimeError("boom"))
    client.post("/api/admin/skill-badges/discover", json={})
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    assert client.get("/api/admin/skill-badges/discover/status").json()["last_error"] is None


def test_concurrent_discovery_is_rejected(client, stub_discovery):
    """
    Intent: Discovery is expensive in tokens and time; a double-clicked button must
        not launch a second concurrent run.
    Success: 409 with a message saying a run is already in progress.
    Feature: Admin area — single-run guard.
    """
    router_module._run_state["running"] = True
    response = client.post("/api/admin/skill-badges/discover", json={})
    assert response.status_code == 409
    assert "already in progress" in response.json()["detail"]


def test_discover_releases_the_lock_after_a_failure(
    client, fake_collection, stub_discovery
):
    """
    Intent: A crashed run must not leave the single-run guard stuck, which would
        block discovery until the process restarted.
    Success: running is False after the failure and a new run is accepted with 200.
    Feature: Admin area — single-run guard.
    """
    stub_discovery(error=RuntimeError("boom"))
    client.post("/api/admin/skill-badges/discover", json={})
    assert router_module._run_state["running"] is False
    stub_discovery()
    assert client.post("/api/admin/skill-badges/discover", json={}).status_code == 200


# --- GET / ---


def test_list_returns_stored_badges(client, fake_collection, stub_discovery):
    """
    Intent: The admin review table reads from this endpoint, so discovered badges
        must be listed with their review status.
    Success: The discovered badge is returned with status "candidate".
    Feature: Admin area — badge review table.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    body = client.get("/api/admin/skill-badges").json()
    assert [b["slug"] for b in body] == ["atlas-search"]
    assert body[0]["status"] == "candidate"


def test_list_filters_by_status(client, fake_collection, stub_discovery):
    """
    Intent: Reviewers need to see just the pending queue or just the approved set,
        so the listing must accept a status filter over HTTP.
    Success: ?status=approved is empty; ?status=candidate returns the one badge.
    Feature: Admin area — badge review filtering.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    assert client.get("/api/admin/skill-badges", params={"status": "approved"}).json() == []
    assert (
        len(client.get("/api/admin/skill-badges", params={"status": "candidate"}).json()) == 1
    )


def test_list_is_empty_before_any_run(client, fake_collection):
    """
    Intent: On a fresh install the admin page must render an empty table rather than
        error out.
    Success: An empty JSON array is returned.
    Feature: Admin area — badge review table.
    """
    assert client.get("/api/admin/skill-badges").json() == []


# --- POST /{slug}/status ---


def test_status_update_succeeds(client, fake_collection, stub_discovery):
    """
    Intent: Promotion is the human decision the whole candidate flow exists to
        capture, and it must persist.
    Success: The response echoes the new status and the stored badge is "approved".
    Feature: Admin area — promote / retire a badge.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    response = client.post(
        "/api/admin/skill-badges/atlas-search/status", json={"status": "approved"}
    )
    assert response.json() == {"slug": "atlas-search", "status": "approved"}
    assert fake_collection.find_one({"slug": "atlas-search"})["status"] == "approved"


def test_status_update_on_unknown_slug_is_404(client, fake_collection):
    """
    Intent: A stale admin page or typo'd slug must get a clear 404 naming the slug,
        not a silent success that implies work happened.
    Success: 404 with the slug in the detail message.
    Feature: Admin area — promote / retire a badge.
    """
    response = client.post(
        "/api/admin/skill-badges/nope/status", json={"status": "approved"}
    )
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_status_update_rejects_an_unknown_status(client, fake_collection, stub_discovery):
    """
    Intent: The lifecycle has exactly three states; the API must reject anything
        else at the boundary rather than writing an unrecognized status to MongoDB.
    Success: 422 is returned for a status outside candidate/approved/retired.
    Feature: Admin area — badge lifecycle validation.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    response = client.post(
        "/api/admin/skill-badges/atlas-search/status", json={"status": "published"}
    )
    assert response.status_code == 422


def test_status_exposes_the_traceback_of_a_failed_run(
    client, fake_collection, stub_discovery
):
    """
    Intent: When a run fails, the operator needs the trace to diagnose it without
        having access to the server's log stream — the admin page offers it as an
        expandable detail.
    Success: The status payload carries a traceback naming the raised exception.
    Feature: Admin area — discovery run error reporting.
    """
    stub_discovery(error=RuntimeError("boom"))
    client.post("/api/admin/skill-badges/discover", json={})

    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert "Traceback" in status["last_traceback"]
    assert "RuntimeError: boom" in status["last_traceback"]


def test_a_successful_run_clears_a_previous_traceback(
    client, fake_collection, stub_discovery
):
    """
    Intent: A stale trace must not linger after a later run succeeds, or the page
        would offer a stack trace for a failure that no longer applies.
    Success: last_traceback is None after a successful run follows a failed one.
    Feature: Admin area — discovery run error reporting.
    """
    stub_discovery(error=RuntimeError("boom"))
    client.post("/api/admin/skill-badges/discover", json={})
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert status["last_traceback"] is None


# --- POST /{slug}/name ---


def test_renaming_a_badge_locks_the_title(client, fake_collection, stub_discovery):
    """
    Intent: Correcting a wrong title is a review action, and the response must confirm
        the correction is protected so the UI can show the reviewer it will survive
        the next run.
    Success: The response echoes the new name with name_locked true, and the stored
        document matches.
    Feature: Admin area — editing a badge title.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    response = client.post(
        "/api/admin/skill-badges/atlas-search/name",
        json={"name": "Atlas Search Fundamentals"},
    )
    assert response.json() == {
        "slug": "atlas-search",
        "name": "Atlas Search Fundamentals",
        "name_locked": True,
    }
    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert doc["name"] == "Atlas Search Fundamentals"


def test_renaming_trims_surrounding_whitespace(client, fake_collection, stub_discovery):
    """
    Intent: Titles typed into a browser prompt often carry stray spaces, which would
        otherwise be stored and then sort oddly in the review table.
    Success: The stored name has no leading or trailing whitespace.
    Feature: Admin area — editing a badge title.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    client.post(
        "/api/admin/skill-badges/atlas-search/name", json={"name": "  Trimmed  "}
    )
    assert fake_collection.find_one({"slug": "atlas-search"})["name"] == "Trimmed"


def test_renaming_an_unknown_badge_is_404(client, fake_collection):
    """
    Intent: A stale page or a typo'd slug must get a clear 404 naming the slug rather
        than a silent success implying the correction was saved.
    Success: 404 with the slug in the detail message.
    Feature: Admin area — editing a badge title.
    """
    response = client.post(
        "/api/admin/skill-badges/nope/name", json={"name": "Whatever"}
    )
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_an_empty_title_is_rejected(client, fake_collection, stub_discovery):
    """
    Intent: A badge with a blank title is unreadable in the review table and unusable
        downstream, so the API must reject it at the boundary rather than storing it.
    Success: 422 is returned and the stored name is unchanged.
    Feature: Admin area — editing a badge title.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    response = client.post(
        "/api/admin/skill-badges/atlas-search/name", json={"name": ""}
    )
    assert response.status_code == 422
    assert fake_collection.find_one({"slug": "atlas-search"})["name"] == "Atlas Search"


def test_a_run_reports_which_badges_were_recognised_as_existing(
    client, fake_collection, monkeypatch
):
    """
    Intent: When a run merges a re-discovered badge into an existing record, the
        reviewer needs to see that it happened — a merge silently changes which record
        a badge's data landed on.
    Success: The run summary reports the merge mapping.
    Feature: Admin area — discovery run feedback.
    """
    stored = DiscoveredBadge(
        slug="atlas-search-fundamentals",
        name="Atlas Search Fundamentals",
        description="Covers Atlas Search.",
        confidence="high",
        source_urls=["https://learn.mongodb.com/skills/atlas-search"],
    )
    skill_badges.upsert_badges([stored])

    rediscovered = DiscoveredBadge(
        slug="atlas-search-basics",
        name="Atlas Search Basics",
        description="Covers Atlas Search.",
        confidence="high",
        source_urls=["https://learn.mongodb.com/skills/atlas-search"],
    )
    monkeypatch.setattr(
        badge_discovery, "discover_badges", lambda **k: ([rediscovered], "notes")
    )
    monkeypatch.setattr(
        badge_matching,
        "match_discovered_to_existing",
        lambda *a, **k: {"atlas-search-basics": "atlas-search-fundamentals"},
    )

    client.post("/api/admin/skill-badges/discover", json={})

    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert status["last_result"]["merged"] == {
        "atlas-search-basics": "atlas-search-fundamentals"
    }
    assert fake_collection.count_documents({}) == 1


# --- POST /{slug}/sources ---


def test_curating_links_locks_them(client, fake_collection, stub_discovery):
    """
    Intent: Editing links is a review action, and the response must confirm the edit is
        protected so the UI can show the reviewer it will survive the next run.
    Success: The response echoes the new list with sources_locked true and the stored
        document matches.
    Feature: Admin area — managing badge reference links.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    response = client.post(
        "/api/admin/skill-badges/atlas-search/sources",
        json={"source_urls": ["https://learn.mongodb.com/skills/atlas-search"]},
    )
    assert response.json() == {
        "slug": "atlas-search",
        "source_urls": ["https://learn.mongodb.com/skills/atlas-search"],
        "sources_locked": True,
    }
    doc = fake_collection.find_one({"slug": "atlas-search"})
    assert doc["source_urls"] == ["https://learn.mongodb.com/skills/atlas-search"]


def test_curating_links_drops_blank_lines(client, fake_collection, stub_discovery):
    """
    Intent: The editor is a free-text list, so blank lines and stray whitespace are
        normal input and must not be stored as empty links.
    Success: Blank entries are removed and remaining URLs are trimmed.
    Feature: Admin area — managing badge reference links.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    response = client.post(
        "/api/admin/skill-badges/atlas-search/sources",
        json={"source_urls": ["  https://learn.mongodb.com/a  ", "", "   "]},
    )
    assert response.json()["source_urls"] == ["https://learn.mongodb.com/a"]


def test_non_http_links_are_rejected(client, fake_collection, stub_discovery):
    """
    Intent: A source link exists so a reviewer can click through to the evidence.
        Anything that is not an http(s) URL cannot serve that purpose, and a
        javascript: value would be an injection risk in the rendered table.
    Success: 422 is returned and the stored links are unchanged.
    Feature: Admin area — managing badge reference links.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    response = client.post(
        "/api/admin/skill-badges/atlas-search/sources",
        json={"source_urls": ["javascript:alert(1)"]},
    )
    assert response.status_code == 422
    assert fake_collection.find_one({"slug": "atlas-search"})["source_urls"] == [
        "https://learn.mongodb.com/skills/atlas-search"
    ]


def test_curating_links_on_an_unknown_badge_is_404(client, fake_collection):
    """
    Intent: A stale page must get a clear 404 naming the slug rather than a silent
        success implying the curation was saved.
    Success: 404 with the slug in the detail message.
    Feature: Admin area — managing badge reference links.
    """
    response = client.post(
        "/api/admin/skill-badges/nope/sources", json={"source_urls": []}
    )
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


# --- DELETE /{slug} ---


def test_deleting_a_retired_badge_succeeds(client, fake_collection, stub_discovery):
    """
    Intent: A reviewer clearing out the retired list needs the delete to actually
        remove the record, and to be told it happened.
    Success: The response reports the deletion and the badge is gone.
    Feature: Admin area — deleting a retired badge.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    client.post(
        "/api/admin/skill-badges/atlas-search/status", json={"status": "retired"}
    )

    response = client.delete("/api/admin/skill-badges/atlas-search")
    assert response.json() == {"slug": "atlas-search", "deleted": True}
    assert fake_collection.count_documents({}) == 0


def test_deleting_a_badge_that_is_not_retired_is_rejected(
    client, fake_collection, stub_discovery
):
    """
    Intent: Deletion is irreversible, so the API must refuse it for a badge still in
        review and say what to do first — rather than silently destroying a candidate.
    Success: 409 naming the retirement requirement, and the badge survives.
    Feature: Admin area — deletion requires retirement first.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    response = client.delete("/api/admin/skill-badges/atlas-search")
    assert response.status_code == 409
    assert "retired" in response.json()["detail"]
    assert fake_collection.count_documents({}) == 1


def test_deleting_an_unknown_badge_is_404(client, fake_collection):
    """
    Intent: A stale page must get a clear 404 rather than a success that implies a
        record was destroyed.
    Success: 404 with the slug in the detail message.
    Feature: Admin area — deleting a retired badge.
    """
    response = client.delete("/api/admin/skill-badges/nope")
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


# --- POST /sync-catalog ---


def test_catalog_sync_can_be_started_from_the_admin_area(
    client, fake_collection, monkeypatch
):
    """
    Intent: Syncing from the published collection is the routine, authoritative action,
        so it must be triggerable from the screen and report its result the same way a
        research run does.
    Success: 200 with {"started": True}, the badge stored, and the status reporting the
        collection as the source.
    Feature: Admin area — sync from the published collection.
    """
    from app.services import credly_catalog

    published = DiscoveredBadge(
        slug="crud-operations-in-mongodb",
        name="CRUD Operations in MongoDB",
        description="d",
        confidence="high",
        source_urls=["https://www.credly.com/org/mongodb/badge/crud"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [published])
    monkeypatch.setattr(
        badge_matching, "match_discovered_to_existing", lambda *a, **k: {}
    )

    response = client.post("/api/admin/skill-badges/sync-catalog")
    assert response.json() == {"started": True}

    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert status["last_result"]["source"] == "credly-collection"
    assert fake_collection.count_documents({}) == 1


def test_a_failed_catalog_sync_is_surfaced(client, fake_collection, monkeypatch):
    """
    Intent: If the collection cannot be read, the reviewer must see why — an empty or
        unchanged table would otherwise look like the catalog had not changed.
    Success: The status endpoint reports the error and nothing is written.
    Feature: Admin area — sync error reporting.
    """
    from app.services import credly_catalog

    def unreachable(**kwargs):
        raise RuntimeError("collection returned no usable badges")

    monkeypatch.setattr(credly_catalog, "fetch_catalog", unreachable)

    client.post("/api/admin/skill-badges/sync-catalog")

    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert "no usable badges" in status["last_error"]
    assert fake_collection.count_documents({}) == 0


def test_a_catalog_sync_is_refused_while_another_run_is_going(client, fake_collection):
    """
    Intent: The sync and a research run write the same records, so they must not run
        concurrently and interleave their writes.
    Success: 409 while a run is in progress.
    Feature: Admin area — single-run guard.
    """
    router_module._run_state["running"] = True
    assert client.post("/api/admin/skill-badges/sync-catalog").status_code == 409


# --- duplicates and merging ---


def test_a_duplicate_scan_merges_the_confident_ones(client, fake_collection, monkeypatch):
    """
    Intent: The reviewer needs a one-click way to resolve duplicates, with the program
        applying the ones it is sure about and reporting the rest — that is the whole
        point of detecting them.
    Success: The run reports the merged pair and the pair needing review, tagged as a
        duplicate scan.
    Feature: Admin area — duplicate scan.
    """
    from app.services import duplicates

    monkeypatch.setattr(
        duplicates,
        "merge_confident_duplicates",
        lambda **k: {
            "merged": [{"keep": "a", "drop": "b"}],
            "needs_review": [{"keep": "c", "drop": "d", "reason": "unsure"}],
        },
    )

    assert client.post("/api/admin/skill-badges/duplicates/scan").json() == {
        "started": True
    }
    result = client.get("/api/admin/skill-badges/discover/status").json()["last_result"]
    assert result["source"] == "duplicate-scan"
    assert len(result["merged"]) == 1
    assert len(result["needs_review"]) == 1


def test_a_scan_failure_is_surfaced(client, fake_collection, monkeypatch):
    """
    Intent: If duplicate detection cannot run — no embedding provider, missing index — the
        reviewer must be told, because an empty result otherwise reads as "no duplicates".
    Success: The status endpoint reports the error.
    Feature: Admin area — duplicate scan error reporting.
    """
    from app.services import duplicates

    def unavailable(**kwargs):
        raise RuntimeError("No embedding provider configured.")

    monkeypatch.setattr(duplicates, "merge_confident_duplicates", unavailable)

    client.post("/api/admin/skill-badges/duplicates/scan")
    status = client.get("/api/admin/skill-badges/discover/status").json()
    assert "No embedding provider" in status["last_error"]


def test_a_reviewer_can_merge_a_pair_by_hand(client, fake_collection, stub_discovery):
    """
    Intent: Pairs the program is unsure about are resolved by a person, so the merge must
        be callable directly — with the kept badge surviving and the other removed.
    Success: The response confirms the merge and only the kept badge remains.
    Feature: Admin area — merging duplicates by hand.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                slug="atlas-search-duplicate",
                name="Atlas Search Duplicate",
                description="Covers Atlas Search.",
                confidence="high",
                source_urls=["https://learn.mongodb.com/skills"],
            )
        ]
    )

    response = client.post(
        "/api/admin/skill-badges/merge",
        json={"keep": "atlas-search", "drop": "atlas-search-duplicate"},
    )
    assert response.json() == {
        "keep": "atlas-search",
        "dropped": "atlas-search-duplicate",
        "merged": True,
    }
    assert fake_collection.count_documents({}) == 1


def test_merging_a_badge_into_itself_is_rejected(client, fake_collection, stub_discovery):
    """
    Intent: A self-merge would delete the record it was meant to keep, so the API must
        refuse it rather than destroying the badge.
    Success: 422 is returned and the badge survives.
    Feature: Admin area — safe merges.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    response = client.post(
        "/api/admin/skill-badges/merge",
        json={"keep": "atlas-search", "drop": "atlas-search"},
    )
    assert response.status_code == 422
    assert fake_collection.count_documents({}) == 1


def test_merging_an_unknown_badge_is_404(client, fake_collection, stub_discovery):
    """
    Intent: A stale page must not be able to delete a record by naming a slug that no
        longer exists on the other side of the merge.
    Success: 404 is returned and nothing is deleted.
    Feature: Admin area — safe merges.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})

    response = client.post(
        "/api/admin/skill-badges/merge",
        json={"keep": "atlas-search", "drop": "never-existed"},
    )
    assert response.status_code == 404
    assert fake_collection.count_documents({}) == 1


def test_a_duplicate_scan_is_refused_while_another_run_is_going(client, fake_collection):
    """
    Intent: A scan merges records while a sync writes them, so the two must not overlap —
        a merge landing mid-sync could delete a record the sync is still updating.
    Success: 409 while a run is in progress.
    Feature: Admin area — single-run guard.
    """
    router_module._run_state["running"] = True
    assert client.post("/api/admin/skill-badges/duplicates/scan").status_code == 409


# --- badge artwork ---


def test_the_stored_artwork_is_served(client, fake_collection, stub_discovery):
    """
    Intent: The review table shows the artwork from this app rather than hotlinking Credly,
        so the image endpoint must serve the stored bytes with their own content type.
    Success: The bytes come back with the stored content type and a cache header.
    Feature: Admin area — badge artwork.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    skill_badges.set_image(
        "atlas-search", b"\x89PNG art", "image/png", "https://images.credly.com/x"
    )

    response = client.get("/api/admin/skill-badges/atlas-search/image")
    assert response.status_code == 200
    assert response.content == b"\x89PNG art"
    assert response.headers["content-type"] == "image/png"
    assert "max-age" in response.headers["cache-control"]


def test_a_badge_without_artwork_returns_404(client, fake_collection, stub_discovery):
    """
    Intent: A 404 lets the page fall back to its placeholder instead of rendering a broken
        image, and distinguishes "no artwork yet" from a server fault.
    Success: 404 for a badge with no stored artwork.
    Feature: Admin area — badge artwork.
    """
    stub_discovery()
    client.post("/api/admin/skill-badges/discover", json={})
    assert client.get("/api/admin/skill-badges/atlas-search/image").status_code == 404


def test_a_sync_stores_the_artwork_and_reports_failures(client, fake_collection, monkeypatch):
    """
    Intent: Artwork is fetched as part of a sync, but it is an aid to recognising a badge —
        one unreachable image must not fail a sync that otherwise worked, so failures are
        reported alongside the badges that were stored.
    Success: The reachable badge's artwork is stored, and the unreachable one is listed as
        a failure without failing the run.
    Feature: Admin area — badge artwork.
    """
    from app.services import badge_art, badge_titles, credly_catalog

    good = DiscoveredBadge(
        slug="good-art",
        name="Good Art",
        description="d",
        confidence="high",
        image_url="https://images.credly.com/good",
        source_urls=["https://www.credly.com/org/mongodb/badge/good"],
    )
    bad = DiscoveredBadge(
        slug="bad-art",
        name="Bad Art",
        description="d",
        confidence="high",
        image_url="https://images.credly.com/bad",
        source_urls=["https://www.credly.com/org/mongodb/badge/bad"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [good, bad])
    monkeypatch.setattr(
        badge_matching, "match_discovered_to_existing", lambda *a, **k: {}
    )

    def fetch(url):
        if url.endswith("bad"):
            raise RuntimeError("not badge artwork")
        return b"\x89PNG", "image/png"

    monkeypatch.setattr(badge_art, "fetch_image", fetch)
    monkeypatch.setattr(
        badge_titles, "read_title_from_image", lambda url, settings=None: None
    )

    client.post("/api/admin/skill-badges/sync-catalog")
    result = client.get("/api/admin/skill-badges/discover/status").json()["last_result"]

    assert result["artwork_stored"] == 1
    assert [f["slug"] for f in result["artwork_failures"]] == ["bad-art"]
    assert skill_badges.get_image("good-art") == (b"\x89PNG", "image/png")


def test_a_sync_verifies_credly_page_titles_and_reports_failures(
    client, fake_collection, monkeypatch
):
    """
    Intent: Verification happens as part of a sync, reading each badge's own Credly page. An
        unreachable page must be reported rather than failing the sync or leaving the
        reviewer unaware that a title is unverified.
    Success: The reachable badge's page title is stored; the unreachable one is listed as a
        failure and the run still succeeds.
    Feature: Admin area — Credly page verification.
    """
    from app.services import badge_art, badge_titles, credly_catalog, credly_page

    good = DiscoveredBadge(
        slug="good-page",
        name="Good Page",
        description="d",
        confidence="high",
        credly_url="https://www.credly.com/org/mongodb/badge/good",
        source_urls=["https://www.credly.com/org/mongodb/badge/good"],
    )
    bad = DiscoveredBadge(
        slug="bad-page",
        name="Bad Page",
        description="d",
        confidence="high",
        credly_url="https://www.credly.com/org/mongodb/badge/bad",
        source_urls=["https://www.credly.com/org/mongodb/badge/bad"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [good, bad])
    monkeypatch.setattr(
        badge_matching, "match_discovered_to_existing", lambda *a, **k: {}
    )
    monkeypatch.setattr(
        badge_titles, "read_title_from_image", lambda url, settings=None: None
    )
    monkeypatch.setattr(badge_art, "fetch_image", lambda url: (b"\x89PNG", "image/png"))

    def page_title(url):
        if url.endswith("bad"):
            raise RuntimeError("No badge title found")
        return "Building the Good Page with MongoDB"

    monkeypatch.setattr(credly_page, "fetch_page_title", page_title)

    client.post("/api/admin/skill-badges/sync-catalog")
    result = client.get("/api/admin/skill-badges/discover/status").json()["last_result"]

    assert result["credly_titles_verified"] == 1
    assert [f["slug"] for f in result["credly_title_failures"]] == ["bad-page"]
    assert fake_collection.find_one({"slug": "good-page"})["credly_title"] == (
        "Building the Good Page with MongoDB"
    )


def test_slugs_can_be_normalised_from_the_admin_area(client, fake_collection):
    """
    Intent: The slug rule must be runnable by whoever is reviewing badges, not only from a
        shell — otherwise a rebuilt collection silently keeps catalog slugs.
    Success: The endpoint renames the mismatched badge and reports what it did.
    Feature: Admin area — normalising slugs.
    """
    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                slug="catalog-vanity-slug",
                name="Vector Search Fundamentals",
                image_title="Vector Search Fundamentals",
                description="d",
                confidence="high",
                source_urls=["https://learn.mongodb.com/skills"],
            )
        ]
    )

    response = client.post("/api/admin/skill-badges/normalise-slugs")
    assert response.json()["renamed"] == [
        {"from": "catalog-vanity-slug", "to": "vector-search-fundamentals"}
    ]
    assert fake_collection.find_one({"slug": "vector-search-fundamentals"}) is not None
