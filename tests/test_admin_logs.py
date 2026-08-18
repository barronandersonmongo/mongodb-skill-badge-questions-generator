"""Tests for the log viewer — app/routers/admin_logs.py and admin/logs.html.

Assertions on the page are on markup, never on a bare word: the template ships
JavaScript containing the same labels it renders.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest
from fastapi.testclient import TestClient

from app import logging_config
from app.main import app

API = "/api/admin/logs"
PAGE = "/admin/logs"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    """Serve a temporary log file, never the developer's real one."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    return tmp_path


def test_the_viewer_returns_the_tail_of_the_active_log(client, log_dir):
    """
    Intent: The whole point of the screen is reading what the service just logged
        without shelling into the box.
    Success: The endpoint returns the log's lines and names the file they came from.
    Feature: Log viewer — reads the current log file.
    """
    (log_dir / "app.log").write_text("first line\nsecond line\n")
    body = client.get(API).json()
    assert body["lines"] == ["first line", "second line"]
    assert body["file"].endswith("app.log")


def test_the_number_of_lines_can_be_chosen(client, log_dir):
    """
    Intent: A quick look wants the last hundred lines; diagnosing a failed run wants
        thousands. One fixed size serves neither.
    Success: The lines parameter limits how many lines come back.
    Feature: Log viewer — adjustable tail length.
    """
    (log_dir / "app.log").write_text("".join(f"line {i}\n" for i in range(50)))
    assert len(client.get(API, params={"lines": 5}).json()["lines"]) == 5


@pytest.mark.parametrize("lines", [0, -1, 10**9], ids=["zero", "negative", "enormous"])
def test_an_out_of_range_line_count_is_refused(client, log_dir, lines):
    """
    Intent: The line count arrives from the browser. Zero or negative is meaningless,
        and an unbounded value would pull the entire 100 MB file through the response.
    Success: A line count outside the allowed range is a validation error.
    Feature: Log viewer — bounded request.
    """
    (log_dir / "app.log").write_text("a line\n")
    assert client.get(API, params={"lines": lines}).status_code == 422


def test_an_empty_log_is_reported_as_empty_not_as_an_error(client, log_dir):
    """
    Intent: On a fresh install there is no log file. A 500 there would look like a
        broken viewer rather than a service that has not logged anything yet.
    Success: The endpoint returns 200 with no lines.
    Feature: Log viewer — first-run state.
    """
    response = client.get(API)
    assert response.status_code == 200
    assert response.json()["lines"] == []


def test_the_viewer_will_not_serve_an_arbitrary_file(client, log_dir):
    """
    Intent: There are no authorizations in this program, so an endpoint that accepted a
        path would let anyone who can reach the service read any file the process can —
        including the connection string in the environment file. It must expose no path
        parameter at all.
    Success: A path-like parameter is ignored, and the response is still the log file.
    Feature: Log viewer — no arbitrary file access.
    """
    (log_dir / "app.log").write_text("the real log\n")
    body = client.get(API, params={"file": "/etc/passwd", "path": "../../etc/passwd"}).json()
    assert body["file"].endswith("app.log")
    assert body["lines"] == ["the real log"]


def test_the_rotated_history_is_reported(client, log_dir):
    """
    Intent: Only the active file is shown, so an operator needs to know whether the entry
        they are hunting has already rotated out — otherwise an absent message reads as
        an event that never happened.
    Success: Existing rotated files are listed with their sizes.
    Feature: Log viewer — reports rotated history.
    """
    (log_dir / "app.log").write_text("current\n")
    (log_dir / "app.log.1").write_text("older\n")
    assert client.get(API).json()["rotated"] == [{"name": "app.log.1", "bytes": 6}]


# --- the page ---


def test_the_log_page_renders_in_the_admin_area(client, log_dir):
    """
    Intent: Reading logs is an operator function, not authoring, so it belongs behind
        /admin with the other curation screens — and must be reachable from the nav
        rather than by typing a URL.
    Success: The page renders under /admin with its nav link marked as admin.
    Feature: Log viewer — an admin screen.
    """
    response = client.get(PAGE)
    assert response.status_code == 200
    assert 'data-log-body="true"' in response.text
    assert 'href="/admin/logs"' in response.text
    assert 'data-admin-area="true"' in response.text


def test_the_page_names_the_file_and_the_rotation_budget(client, log_dir):
    """
    Intent: An operator looking at a truncated log needs to know it rotates, at what size
        and how many files are kept, or they will conclude that history was lost to a
        bug.
    Success: The page shows the log path, the 100 MB size and the ten-file budget.
    Feature: Log viewer — explains the rotation policy.
    """
    body = client.get(PAGE).text
    assert 'data-log-file="true"' in body
    assert "app.log" in body
    assert "100 MB" in body
    assert "10 files" in body


def test_the_log_contents_are_not_rendered_into_the_page(client, log_dir):
    """
    Intent: Log lines are arbitrary text and may contain markup. Interpolating them into
        the HTML would let a logged string alter the page; fetching them and setting
        textContent cannot. It also lets the view refresh without a reload.
    Success: A logged marker string is absent from the initial HTML.
    Feature: Log viewer — contents are fetched, not templated.
    """
    (log_dir / "app.log").write_text("<script>marker()</script>\n")
    assert "marker()" not in client.get(PAGE).text


def test_the_logs_api_is_mounted(client):
    """
    Intent: A router that is written but never included fails only in production, on a
        screen whose contents then never load.
    Success: The log endpoint appears in the served API schema.
    Feature: Application wiring — log viewer API is reachable.
    """
    assert API in client.get("/openapi.json").json()["paths"]


def test_no_log_handler_writes_to_the_real_log_file():
    """
    Intent: pytest imports app.main during collection, which configures logging against
        the real directory before any fixture runs — so the suite appended to the
        operator's logs/app.log, polluting the log they read and eventually rotating real
        entries out. An earlier version of this test checked the environment-derived path
        instead of the handler, and passed while the handler still wrote to the real file.
        This asserts on the handler itself, which is the thing that writes.
    Success: No file handler on the root logger points at the repository's logs/app.log.
    Feature: Test suite — hermetic logging.
    """
    import logging
    from pathlib import Path

    real_log = (Path(__file__).parent.parent / "logs" / "app.log").resolve()
    attached = [
        Path(h.baseFilename).resolve()
        for h in logging.getLogger().handlers
        if hasattr(h, "baseFilename")
    ]
    assert attached, "logging is not configured, so this test would pass vacuously"
    assert real_log not in attached


def test_a_line_logged_during_a_test_lands_in_the_temporary_log(log_dir):
    """
    Intent: The check above proves the handler is not pointed at the real file; this
        proves it is pointed at somewhere useful, so the isolation cannot be satisfied by
        accidentally disabling logging altogether — which would hide log-related
        regressions rather than isolate them.
    Success: A line logged inside a test appears in the temporary log directory.
    Feature: Test suite — hermetic logging.
    """
    import logging

    logging.getLogger("app.isolation.check").warning("written during a test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "written during a test" in (log_dir / "logs" / "app.log").read_text()


def test_importing_the_application_does_not_log_a_start(log_dir):
    """
    Intent: A start recorded at import time is recorded every time anything imports the
        application — every run of this suite included, which is how the operator's log
        came to be full of test noise. A start belongs to actually running the service.
    Success: Importing app.main writes nothing; entering the app's lifespan logs the start.
    Feature: Logging — a start is recorded when the service runs, not when it is imported.
    """
    import importlib
    import logging

    from app import main

    importlib.reload(main)
    for handler in logging.getLogger().handlers:
        handler.flush()
    log = log_dir / "logs" / "app.log"
    assert not log.exists() or "Application starting" not in log.read_text()

    with TestClient(main.app):
        pass
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "Application starting" in log.read_text()
