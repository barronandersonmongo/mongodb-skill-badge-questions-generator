"""Tests for app/routers/questions.py — the JSON API under /api/questions.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.question import GeneratedQuestion, QuestionOption
from app.repositories import questions
from app.routers import questions as api_module

API = "/api/questions"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def reset_run_state():
    reset = dict(
        running=False,
        last_result=None,
        last_error=None,
        last_traceback=None,
        started_at=None,
        finished_at=None,
    )
    api_module._run_state.update(reset)
    yield
    api_module._run_state.update(reset)


def make(stem: str = "Which stage filters documents?", **overrides) -> GeneratedQuestion:
    return GeneratedQuestion(
        **{
            "stem": stem,
            "options": [
                QuestionOption(text="$match", is_correct=True, rationale="filters"),
                QuestionOption(text="$project", is_correct=False, rationale="reshapes"),
                QuestionOption(text="$sort", is_correct=False, rationale="orders"),
                QuestionOption(text="$limit", is_correct=False, rationale="truncates"),
            ],
            "explanation": "$match filters.",
            "difficulty": "foundational",
            "categories": ["aggregation"],
            "skill_badges": ["atlas-search"],
            **overrides,
        }
    )


def test_generation_runs_in_the_background(client, monkeypatch, fake_questions):
    """
    Intent: A generation run takes minutes. Doing it inside the request would hold the
        browser open past its timeout and lose the run.
    Success: POST /generate returns immediately with started, before the work runs.
    Feature: Question generation — long runs do not block the request.
    """
    calls: list[tuple] = []
    monkeypatch.setattr(
        api_module, "generate_for_badge", lambda *a, **k: calls.append((a, k)) or {}
    )
    response = client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "max_pages": 2})
    assert response.status_code == 200
    assert response.json() == {"started": True}
    assert calls  # the background task ran when the TestClient closed the request


def test_the_selected_badges_and_walk_size_reach_the_generator(client, monkeypatch, fake_questions):
    """
    Intent: Replaces a test that passed a question count. A run is now sized in pages —
        the badge's documentation is walked a page at a time and each page is worth
        several questions — so pages and questions-per-page are the author's controls,
        and a dropped one would silently walk a different amount of material.
    Success: Each selected badge is walked, with the page cap, questions per page and
        instructions arriving as given.
    Feature: Question generation — the author's selection is honoured.
    """
    seen: list = []

    def record(slug, **kwargs):
        seen.append((slug, kwargs))
        return {"skill_badge": slug, "inserted": 0, "pages_done": 0, "pages_available": 0}

    monkeypatch.setattr(api_module, "generate_for_badge", record)
    client.post(
        API + "/generate",
        json={
            "skill_badges": ["atlas-search", "aggregation"],
            "max_pages": 4,
            "questions_per_page": 2,
            "extra_instructions": "advanced only",
        },
    )
    assert [slug for slug, _ in seen] == ["atlas-search", "aggregation"]
    assert all(
        kwargs["max_pages"] == 4
        and kwargs["questions_per_page"] == 2
        and kwargs["extra_instructions"] == "advanced only"
        for _, kwargs in seen
    )


def test_a_run_without_a_badge_is_rejected_before_it_starts(client, fake_questions):
    """
    Intent: Badges scope the questions, so an empty selection has no syllabus. Catching
        it at the boundary means no background task is started and no tokens spent.
    Success: POST /generate with an empty badge list is a validation error.
    Feature: Question generation — a badge scope is required.
    """
    response = client.post(API + "/generate", json={"skill_badges": [], "max_pages": 3})
    assert response.status_code == 422


@pytest.mark.parametrize("max_pages", [0, 201], ids=["none", "too-many"])
def test_an_unreasonable_walk_size_is_rejected(client, fake_questions, max_pages):
    """
    Intent: Replaces a test bounding a question count at 25. A run is now bounded by
        pages, and the old ceiling would cap a badge at 25 questions when a badge needs
        hundreds. Zero pages is still a pointless run, and an unbounded walk is still an
        unbounded bill and a run that will not finish.
    Success: A page count outside 1–200 is a validation error.
    Feature: Question generation — bounded walk size.
    """
    response = client.post(
        API + "/generate", json={"skill_badges": ["atlas-search"], "max_pages": max_pages}
    )
    assert response.status_code == 422


def test_a_second_run_is_refused_while_one_is_in_progress(client, fake_questions):
    """
    Intent: Two concurrent runs would overwrite each other's reported state, so the
        author would see one run's result attributed to the other.
    Success: POST /generate returns 409 while a run is marked running.
    Feature: Question generation — one run at a time.
    """
    api_module._run_state["running"] = True
    response = client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "max_pages": 1})
    assert response.status_code == 409


def test_a_failed_run_is_reported_with_its_traceback(client, monkeypatch, fake_questions):
    """
    Intent: A background failure has nowhere to surface. Swallowed, it looks like a run
        that produced nothing; the author needs the message and the trace without
        reading server logs.
    Success: After a raising run, the status reports the error and a traceback, and is
        no longer running.
    Feature: Question generation — background failures are surfaced.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr(api_module, "generate_for_badge", explode)
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "max_pages": 1})
    state = client.get(API + "/generate/status").json()
    assert state["running"] is False
    assert "no credentials" in state["last_error"]
    assert "RuntimeError" in state["last_traceback"]


def test_the_status_endpoint_reports_a_finished_runs_result(client, monkeypatch, fake_questions):
    """
    Intent: The page polls this endpoint to know when to reload and what to say. A
        result it could not read would leave the run looking unfinished forever.
    Success: A completed run's summary is returned with no error.
    Feature: Question generation — run status polling.
    """
    monkeypatch.setattr(api_module, "generate_for_badge", lambda *a, **k: {"inserted": 3})
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "max_pages": 3})
    state = client.get(API + "/generate/status").json()
    assert state["last_result"] == {"inserted": 3}
    assert state["last_error"] is None


def test_questions_are_returned_as_plain_json(client, fake_questions):
    """
    Intent: Export is a stated requirement and the output must be easy to paste
        elsewhere, so the listing endpoint is the export — plain JSON, no envelope, no
        Mongo internals.
    Success: GET returns a JSON list of the stored questions, without _id.
    Feature: Question export — JSON output.
    """
    questions.insert_questions([make()])
    body = client.get(API).json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["stem"] == "Which stage filters documents?"
    assert "_id" not in body[0]


def test_the_export_honours_the_screens_filters(client, fake_questions):
    """
    Intent: The author exports what they are looking at. If the endpoint ignored the
        filters, the exported file would contain questions they had deliberately
        excluded — silently, since it is downloaded not displayed.
    Success: Filtering by badge, category and status each narrow the response.
    Feature: Question export — filtered by badge, category and status.
    """
    questions.insert_questions(
        [
            make("Search one?", skill_badges=["atlas-search"], categories=["search"]),
            make("Agg one?", skill_badges=["aggregation"], categories=["aggregation"]),
        ]
    )
    assert len(client.get(API, params={"skill_badge": "atlas-search"}).json()) == 1
    assert len(client.get(API, params={"category": "aggregation"}).json()) == 1
    assert client.get(API, params={"status": "approved"}).json() == []


def test_a_question_can_be_approved(client, fake_questions):
    """
    Intent: Approval is the decision the tool exists to record; the screen's button
        must actually persist it.
    Success: POST /{id}/status stores the new status and reports it.
    Feature: Question lifecycle — approve and reject.
    """
    question_id = questions.insert_questions([make()])["question_ids"][0]
    response = client.post(f"{API}/{question_id}/status", json={"status": "approved"})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert questions.list_questions()[0]["status"] == "approved"


def test_an_unrecognised_status_is_rejected(client, fake_questions):
    """
    Intent: The status vocabulary is what the review tabs and filters are built on. An
        arbitrary value would create a question that appears under no tab at all.
    Success: A status outside draft/approved/rejected is a validation error.
    Feature: Question lifecycle — controlled status vocabulary.
    """
    question_id = questions.insert_questions([make()])["question_ids"][0]
    response = client.post(f"{API}/{question_id}/status", json={"status": "published"})
    assert response.status_code == 422


def test_acting_on_an_unknown_question_is_a_404(client, fake_questions):
    """
    Intent: A stale page must be told its question is gone rather than getting a
        success for a write that hit nothing.
    Success: Status change and delete both return 404 for an unknown id.
    Feature: Question lifecycle — unknown question is reported.
    """
    assert client.post(f"{API}/nope/status", json={"status": "approved"}).status_code == 404
    assert client.delete(f"{API}/nope").status_code == 404


def test_a_question_can_be_deleted(client, fake_questions):
    """
    Intent: An author must be able to remove a question outright, not only reject it.
    Success: DELETE removes the question and reports it.
    Feature: Question lifecycle — delete.
    """
    question_id = questions.insert_questions([make()])["question_ids"][0]
    assert client.delete(f"{API}/{question_id}").json()["deleted"] is True
    assert questions.list_questions() == []


def test_the_questions_api_is_mounted_outside_the_admin_area(client):
    """
    Intent: A router that is written but never included fails only in production, on a
        screen that then does nothing when its buttons are pressed. And the questions
        API belongs to the authoring surface, not to badge curation — leaving a copy
        under /api/admin would be a second URL to keep working and would blur the
        boundary the two areas exist to draw.
    Success: The questions endpoints are served under /api/questions, and no questions
        endpoint is served under /api/admin.
    Feature: Application wiring — questions API is reachable, outside /api/admin.
    """
    paths = client.get("/openapi.json").json()["paths"]
    assert API in paths
    assert API + "/generate" in paths
    assert not [p for p in paths if p.startswith("/api/admin") and "question" in p]


# --- the elapsed timer must survive leaving the page ---


def test_a_running_run_reports_when_it_started(client, monkeypatch, fake_questions):
    """
    Intent: The browser cannot remember when a run started: navigating to another screen
        and back, or reloading, loses it and the elapsed timer restarts at zero while the
        run is still going — which reads as a run that has made no progress. The start
        time therefore has to come from the server.
    Success: While a run is in progress the status reports the epoch second it started.
    Feature: Question generation — elapsed time survives leaving the page.
    """
    started: dict = {}

    def slow(*args, **kwargs):
        # Observed from inside the run, which is the only moment it is "running".
        started["at"] = api_module._run_state["started_at"]
        started["running"] = api_module._run_state["running"]
        return {}

    monkeypatch.setattr(api_module, "generate_for_badge", slow)
    before = time.time()
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "max_pages": 1})
    assert started["running"] is True
    assert before <= started["at"] <= time.time()


def test_the_status_reports_the_servers_clock(client, fake_questions):
    """
    Intent: The page measures elapsed time as "now minus started_at". If "now" came from
        the browser, a machine whose clock is off — or in another time zone — would show
        a wildly wrong or negative duration. Both ends must be read from one clock.
    Success: The status response carries the server's current time.
    Feature: Question generation — elapsed time measured against one clock.
    """
    body = client.get(API + "/generate/status").json()
    assert abs(body["server_time"] - time.time()) < 5


def test_a_finished_run_reports_when_it_finished(client, monkeypatch, fake_questions):
    """
    Intent: After a reload the page has no record of the run at all, so "took 4m 12s" can
        only be shown if the server recorded both ends. Without it, a completed run
        reports a duration measured from whenever the page happened to open.
    Success: A completed run reports finished_at at or after started_at, and is no longer
        running.
    Feature: Question generation — a finished run reports its real duration.
    """
    monkeypatch.setattr(api_module, "generate_for_badge", lambda *a, **k: {"inserted": 1})
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "max_pages": 1})
    state = client.get(API + "/generate/status").json()
    assert state["running"] is False
    assert state["finished_at"] >= state["started_at"]


def test_a_new_run_does_not_inherit_the_previous_runs_start_time(
    client, monkeypatch, fake_questions
):
    """
    Intent: started_at is what the timer counts from. If a second run reused the first
        one's stamp, its timer would open at the first run's total duration and keep
        climbing — the mirror image of the bug being fixed here.
    Success: The second run's start time is later than the first run's.
    Feature: Question generation — each run is timed from its own start.
    """
    monkeypatch.setattr(api_module, "generate_for_badge", lambda *a, **k: {"inserted": 1})
    payload = {"skill_badges": ["atlas-search"], "max_pages": 1}
    client.post(API + "/generate", json=payload)
    first = client.get(API + "/generate/status").json()["started_at"]
    client.post(API + "/generate", json=payload)
    second = client.get(API + "/generate/status").json()["started_at"]
    assert second >= first
    assert client.get(API + "/generate/status").json()["finished_at"] >= second


def test_the_backfill_endpoint_composes_missing_embedding_text(client, fake_questions):
    """
    Intent: The questions stored before this field existed need it, and asking someone to
        open a Mongo shell to run a one-off script is how a step like that gets skipped —
        leaving a vector index that quietly indexes nothing.
    Success: POST /backfill-embedding-text writes the field and reports the counts.
    Feature: Question embedding text — backfill is reachable from the API.
    """
    fake_questions.docs.append(
        {"question_id": "old1", "stem": "An older question?", "explanation": "Because."}
    )
    response = client.post(API + "/backfill-embedding-text")
    assert response.status_code == 200
    assert response.json() == {"written": 1, "already_correct": 0}
    assert fake_questions.docs[0]["embedding_text"].startswith("Question: An older question?")


def test_the_backfill_is_safe_to_run_twice(client, fake_questions):
    """
    Intent: An operator will press this more than once, and it must not rewrite every
        document each time — with autoEmbed that would re-embed unchanged text and cost
        money for no change.
    Success: A second call reports nothing written.
    Feature: Question embedding text — backfill is idempotent.
    """
    questions.insert_questions([make()])
    client.post(API + "/backfill-embedding-text")
    assert client.post(API + "/backfill-embedding-text").json()["written"] == 0


# --- semantic search ---


def test_search_returns_questions_ranked_by_meaning(client, fake_questions):
    """
    Intent: An author asking "what do we already have about joining collections?" should
        find the $lookup questions whether or not they used that word — that is the point
        of a vector index over a text filter.
    Success: The search endpoint returns matching questions with their scores.
    Feature: Question search — semantic search over stored questions.
    """
    questions.insert_questions([make("Which stage filters documents?")])
    body = client.get(API + "/search", params={"q": "stage filters documents"}).json()
    assert body and body[0]["stem"] == "Which stage filters documents?"
    assert body[0]["score"] > 0


@pytest.mark.parametrize(
    "params",
    [{"q": "a"}, {"q": ""}, {"q": "ok", "limit": 0}, {"q": "ok", "limit": 500}],
    ids=["too-short", "empty", "no-limit", "limit-too-high"],
)
def test_a_malformed_search_is_refused(client, fake_questions, params):
    """
    Intent: A one-character query cannot mean anything to an embedding model, and an
        unbounded limit makes the response size and any downstream cost unpredictable.
        Both are caught at the boundary rather than sent to Atlas.
    Success: Each malformed search is a validation error.
    Feature: Question search — bounded request.
    """
    assert client.get(API + "/search", params=params).status_code == 422


def test_an_unbuilt_index_is_reported_as_unavailable_not_as_a_bug(
    client, fake_questions, monkeypatch
):
    """
    Intent: A newly created Atlas index is not queryable for a few minutes, and can be
        renamed or dropped later. That is a state to report — a 500 would send someone
        looking for a bug in this program, and an empty list would read as "we have
        nothing on that".
    Success: A storage failure during search returns 503 naming the index as the suspect.
    Feature: Question search — an unavailable index is explained.
    """
    from pymongo.errors import OperationFailure

    def explode(*args, **kwargs):
        raise OperationFailure("index not found: questions_embedding_text_vector")

    monkeypatch.setattr(questions, "similar_by_embedding_text", explode)
    response = client.get(API + "/search", params={"q": "anything"})
    assert response.status_code == 503
    assert "index" in response.json()["detail"].lower()


# --- the duplicate sweep ---


def test_the_sweep_runs_in_the_background(client, monkeypatch, fake_questions):
    """
    Intent: A sweep is a vector search and a rerank call per question, which is fast but not
        instant on a large collection. Holding the request open would risk a browser timeout
        mid-delete.
    Success: POST /duplicates/sweep returns started immediately, and the work runs.
    Feature: Question duplicate sweep — does not block the request.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.services.question_duplicates.sweep",
        lambda **kwargs: calls.append(kwargs) or {},
    )
    response = client.post(API + "/duplicates/sweep")
    assert response.json() == {"started": True}
    assert calls == [{"delete": True}]


def test_a_dry_run_is_passed_through_as_one(client, monkeypatch, fake_questions):
    """
    Intent: The dry run is the safeguard for an unmeasured delete threshold. If the flag were
        dropped between the button and the sweep, a "dry run" would delete questions.
    Success: dry_run=true reaches the sweep as delete=False.
    Feature: Question duplicate sweep — a dry run never deletes.
    """
    calls: list[dict] = []
    monkeypatch.setattr(
        "app.services.question_duplicates.sweep",
        lambda **kwargs: calls.append(kwargs) or {},
    )
    client.post(API + "/duplicates/sweep", params={"dry_run": True})
    assert calls == [{"delete": False}]


def test_a_sweep_is_refused_while_another_run_is_going(client, fake_questions):
    """
    Intent: A sweep and a generation run share the same reported state, and a sweep deleting
        questions while a run inserts them would interleave unpredictably.
    Success: The sweep returns 409 while a run is in progress.
    Feature: Question duplicate sweep — one run at a time.
    """
    api_module._run_state["running"] = True
    assert client.post(API + "/duplicates/sweep").status_code == 409


def test_a_failed_sweep_is_reported_with_its_traceback(client, monkeypatch, fake_questions):
    """
    Intent: A missing Voyage key is the expected first failure, and it happens in a
        background task with nowhere to surface. Swallowed, it would look like a collection
        with no duplicates.
    Success: The status reports the error and a traceback, and is no longer running.
    Feature: Question duplicate sweep — failures are surfaced.
    """
    def explode(**kwargs):
        raise RuntimeError("No Voyage API key found")

    monkeypatch.setattr("app.services.question_duplicates.sweep", explode)
    client.post(API + "/duplicates/sweep")
    state = client.get(API + "/generate/status").json()
    assert state["running"] is False
    assert "No Voyage API key found" in state["last_error"]
    assert "RuntimeError" in state["last_traceback"]


def test_the_sweep_is_timed_like_any_other_run(client, monkeypatch, fake_questions):
    """
    Intent: The sweep shares the run state and the screen's elapsed timer. Without its own
        timestamps the timer would show the previous run's duration, or none.
    Success: A completed sweep reports started_at and finished_at in order.
    Feature: Question duplicate sweep — timed on the server.
    """
    monkeypatch.setattr("app.services.question_duplicates.sweep", lambda **k: {"compared": 0})
    client.post(API + "/duplicates/sweep")
    state = client.get(API + "/generate/status").json()
    assert state["finished_at"] >= state["started_at"]


# --- coverage ---


def test_coverage_reports_every_badge(client, fake_collection, fake_questions, fake_doc_pages):
    """
    Intent: A badge with no questions is exactly the one an author needs to find, and it
        is the one that would be missing if coverage were built from the questions rather
        than the catalog.
    Success: A badge with no questions appears with zero counts.
    Feature: Question coverage — every badge is listed, including the empty ones.
    """
    fake_collection.docs.append(
        {"slug": "atlas-search", "name": "Atlas Search", "status": "approved"}
    )
    rows = client.get(API + "/coverage").json()
    assert [r["skill_badge"] for r in rows] == ["atlas-search"]
    assert rows[0]["total"] == 0 and rows[0]["draft"] == 0


def test_coverage_lists_the_thinnest_badge_first(client, fake_collection, fake_questions, fake_doc_pages):
    """
    Intent: The screen exists to answer "where do I spend the next run". Sorted by name
        it would answer "what is alphabetically first", and the author would have to scan
        34 rows to find the thin ones.
    Success: Badges come back in ascending order of total questions.
    Feature: Question coverage — thinnest badges first.
    """
    fake_collection.docs.extend([
        {"slug": "busy", "name": "Busy", "status": "approved"},
        {"slug": "thin", "name": "Thin", "status": "approved"},
    ])
    fake_questions.docs.extend([
        {"skill_badges": ["busy"], "status": "draft"},
        {"skill_badges": ["busy"], "status": "draft"},
        {"skill_badges": ["thin"], "status": "draft"},
    ])
    rows = client.get(API + "/coverage").json()
    assert [r["skill_badge"] for r in rows] == ["thin", "busy"]


def test_coverage_says_how_much_material_a_badge_has_left(
    client, fake_collection, fake_questions, fake_doc_pages
):
    """
    Intent: "Few questions" is not actionable on its own — a badge with 300 unused pages
        needs another run, while one with none has exhausted its material and needs the
        corpus widened instead. The two look identical without this number.
    Success: Coverage reports the pages a badge has used and how many remain unused.
    Feature: Question coverage — remaining documentation per badge.
    """
    from app.repositories import doc_pages

    fake_collection.docs.append(
        {
            "slug": "atlas-search",
            "name": "Atlas Search",
            "status": "approved",
            "categories": ["indexes"],
        }
    )
    doc_pages.upsert_pages([
        {"url": "https://x/a.md", "source": "ix", "title": "Atlas Search indexes",
         "text": "Atlas Search indexes."},
    ])
    rows = client.get(API + "/coverage").json()
    assert rows[0]["pages_used"] == 0
    assert rows[0]["pages_available"] is not None
