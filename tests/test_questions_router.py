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
        api_module, "generate_questions", lambda *a, **k: calls.append((a, k)) or {}
    )
    response = client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "count": 2})
    assert response.status_code == 200
    assert response.json() == {"started": True}
    assert calls  # the background task ran when the TestClient closed the request


def test_the_selected_badges_and_count_reach_the_generator(client, monkeypatch, fake_questions):
    """
    Intent: The picker and the count are the author's only control over a run. If the
        endpoint dropped them the run would silently generate something else.
    Success: The badge slugs, count, material and instructions all arrive as given.
    Feature: Question generation — the author's selection is honoured.
    """
    seen: dict = {}

    def record(slugs, count, **kwargs):
        seen.update(slugs=slugs, count=count, **kwargs)
        return {}

    monkeypatch.setattr(api_module, "generate_questions", record)
    client.post(
        API + "/generate",
        json={
            "skill_badges": ["atlas-search", "aggregation"],
            "count": 4,
            "source_material": "lesson text",
            "extra_instructions": "advanced only",
        },
    )
    assert seen == {
        "slugs": ["atlas-search", "aggregation"],
        "count": 4,
        "source_material": "lesson text",
        "extra_instructions": "advanced only",
    }


def test_a_run_without_a_badge_is_rejected_before_it_starts(client, fake_questions):
    """
    Intent: Badges scope the questions, so an empty selection has no syllabus. Catching
        it at the boundary means no background task is started and no tokens spent.
    Success: POST /generate with an empty badge list is a validation error.
    Feature: Question generation — a badge scope is required.
    """
    response = client.post(API + "/generate", json={"skill_badges": [], "count": 3})
    assert response.status_code == 422


@pytest.mark.parametrize("count", [0, 26], ids=["none", "too-many"])
def test_an_unreasonable_batch_size_is_rejected(client, fake_questions, count):
    """
    Intent: Zero questions is a pointless run, and an unbounded count is an unbounded
        bill and a turn that will not finish. Both must be refused at the boundary.
    Success: A count outside 1–25 is a validation error.
    Feature: Question generation — bounded batch size.
    """
    response = client.post(
        API + "/generate", json={"skill_badges": ["atlas-search"], "count": count}
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
    response = client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "count": 1})
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

    monkeypatch.setattr(api_module, "generate_questions", explode)
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "count": 1})
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
    monkeypatch.setattr(api_module, "generate_questions", lambda *a, **k: {"inserted": 3})
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "count": 3})
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

    monkeypatch.setattr(api_module, "generate_questions", slow)
    before = time.time()
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "count": 1})
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
    monkeypatch.setattr(api_module, "generate_questions", lambda *a, **k: {"inserted": 1})
    client.post(API + "/generate", json={"skill_badges": ["atlas-search"], "count": 1})
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
    monkeypatch.setattr(api_module, "generate_questions", lambda *a, **k: {"inserted": 1})
    payload = {"skill_badges": ["atlas-search"], "count": 1}
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
