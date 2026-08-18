"""Tests for app/db.py — MongoDB client wiring.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app import db


class FakeMongoClient:
    instances: list[str] = []

    def __init__(self, uri: str):
        self.uri = uri
        FakeMongoClient.instances.append(uri)
        self._databases: dict[str, dict] = {}

    def __getitem__(self, name: str) -> dict:
        return self._databases.setdefault(name, {"__name__": name})


@pytest.fixture(autouse=True)
def fake_mongo(monkeypatch):
    FakeMongoClient.instances = []
    monkeypatch.setattr(db, "MongoClient", FakeMongoClient)
    yield


def test_client_uses_the_configured_uri(monkeypatch):
    """
    Intent: The client must connect to the cluster named in configuration, so no
        environment can be silently bypassed by a hardcoded URI.
    Success: The constructed client's URI matches the configured value.
    Feature: Storage — Atlas connection.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://configured")
    assert db.get_client().uri == "mongodb://configured"


def test_client_is_created_once(monkeypatch):
    """
    Intent: pymongo pools connections per client, so the app must reuse one client
        per process rather than opening a new connection pool per request.
    Success: Repeated calls return the same object and only one client was ever
        constructed.
    Feature: Storage — connection pooling.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://configured")
    assert db.get_client() is db.get_client()
    assert FakeMongoClient.instances == ["mongodb://configured"]


def test_database_is_the_agreed_one(monkeypatch):
    """
    Intent: All collections must live in the agreed skill-badge-questions database,
        so writes cannot land in a stray database on the same cluster.
    Success: get_database() resolves the "skill-badge-questions" database.
    Feature: Storage — database target.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://configured")
    assert db.get_database()["__name__"] == "skill-badge-questions"


def test_missing_credentials_raise_before_connecting():
    """
    Intent: With no credentials configured, the app must fail before attempting a
        connection, so the error names the missing variable instead of surfacing as
        a driver timeout.
    Success: RuntimeError naming PTM_HACKATHON_CONNECTION_STRING is raised and no
        client was constructed.
    Feature: Storage — fail-fast on missing credentials.
    """
    with pytest.raises(RuntimeError, match="PTM_HACKATHON_CONNECTION_STRING"):
        db.get_client()
    assert FakeMongoClient.instances == []
