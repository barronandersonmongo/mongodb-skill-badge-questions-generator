import pytest

from app.config import Settings
from app.repositories import questions, skill_badges
from tests.fakes import FakeCollection


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No test may depend on the developer's real credentials."""
    for var in (
        "PTM_HACKATHON_CONNECTION_STRING",
        "MONGODB_URI",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_MODEL",
        "GROVE_PRIMARY_KEY",
        "GROVE_SECONDARY_KEY",
        "WEB_SEARCH_TOOL_TYPE",
        "WEB_FETCH_TOOL_TYPE",
        "SKILL_BADGE_CATALOG_URL",
        "CREDLY_COLLECTION_URL",
        "VECTOR_INDEX_NAME",
        "GROVE_ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def isolate_log_file(tmp_path, monkeypatch):
    """No test may write to the operator's real log file.

    Importing the application configures logging, so without this the suite appends
    to logs/app.log on every run — polluting the log an operator reads and, over
    enough runs, rotating real history out of existence.
    """
    from app import logging_config

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(logging_config, "_configured", False, raising=False)


@pytest.fixture(autouse=True)
def clear_settings_cache():
    """get_settings() is not cached, but get_client() is — drop it between tests."""
    from app.db import get_client

    get_client.cache_clear()
    yield
    get_client.cache_clear()


@pytest.fixture
def settings() -> Settings:
    return Settings(mongodb_uri="mongodb://test")


@pytest.fixture
def fake_collection(monkeypatch) -> FakeCollection:
    """Point the skill_badges repository at an in-memory collection."""
    collection = FakeCollection()
    monkeypatch.setattr(skill_badges, "collection", lambda: collection)
    return collection


@pytest.fixture
def fake_questions(monkeypatch) -> FakeCollection:
    """Point the questions repository at an in-memory collection."""
    collection = FakeCollection()
    monkeypatch.setattr(questions, "collection", lambda: collection)
    return collection


class _StubGateway:
    """A local stand-in for an Anthropic-compatible API gateway."""

    def __init__(self, url: str, headers: dict[str, str]):
        self.url = url
        self.headers = headers


@pytest.fixture
def stub_gateway():
    """Serve one canned Messages response and record the request headers."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.update({k.lower(): v for k, v in self.headers.items()})
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.dumps(
                {
                    "id": "msg_stub",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [{"type": "text", "text": "pong"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # keep test output clean
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _StubGateway(f"http://127.0.0.1:{server.server_port}", received)
    finally:
        server.shutdown()
        server.server_close()
