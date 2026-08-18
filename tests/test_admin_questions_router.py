"""Tests for app/routers/admin_questions.py — the JSON API under /api/admin.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.question import GeneratedQuestion, QuestionOption
from app.repositories import questions
from app.routers import admin_questions as api_module

API = "/api/admin/questions"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
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


def test_the_questions_api_is_mounted(client):
    """
    Intent: A router that is written but never included fails only in production, on a
        screen that then does nothing when its buttons are pressed.
    Success: The questions endpoints appear in the application's routes.
    Feature: Application wiring — questions API is reachable.
    """
    paths = client.get("/openapi.json").json()["paths"]
    assert API in paths
    assert API + "/generate" in paths
