"""Tests for app/services/discover_cli.py — shell entry point.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import json

import pytest

from app.models.skill_badge import DiscoveredBadge
from app.services import badge_discovery, badge_matching, discover_cli

BADGE = DiscoveredBadge(
    slug="atlas-search",
    name="Atlas Search",
    description="Covers Atlas Search.",
    confidence="high",
    source_urls=["https://learn.mongodb.com/skills/atlas-search"],
)


@pytest.fixture
def stub_discovery(monkeypatch):
    """Stub only the Claude calls; the real sync and MongoDB writes still run."""

    def install(badges=(BADGE,), notes="raw notes"):
        stub = lambda **kwargs: (list(badges), notes)  # noqa: E731
        monkeypatch.setattr(discover_cli, "discover_badges", stub)
        monkeypatch.setattr(badge_discovery, "discover_badges", stub)
        monkeypatch.setattr(
            badge_matching, "match_discovered_to_existing", lambda *a, **k: {}
        )

    return install


def run(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr("sys.argv", ["discover_cli", *argv])
    return discover_cli.main()


def test_cli_writes_badges_and_reports_the_run(
    monkeypatch, capsys, fake_collection, stub_discovery
):
    """
    Intent: The CLI is how the collection gets populated the first time, without
        booting the web app, and it must report what it wrote.
    Success: Exit code 0, the badge JSON on stdout, "1 inserted" on stderr, and the
        badge persisted.
    Feature: Badge discovery — command-line population.
    """
    stub_discovery()
    assert run(monkeypatch, []) == 0

    captured = capsys.readouterr()
    assert json.loads(captured.out)[0]["slug"] == "atlas-search"
    assert "1 inserted" in captured.err
    assert fake_collection.count_documents({}) == 1


def test_dry_run_writes_nothing_but_shows_the_notes(
    monkeypatch, capsys, fake_collection, stub_discovery
):
    """
    Intent: An operator must be able to inspect what a run would produce — including
        Claude's raw research notes — before letting it touch the database.
    Success: Exit code 0, badges printed, notes shown on stderr, and the collection
        left empty.
    Feature: Badge discovery — dry-run inspection.
    """
    stub_discovery()
    assert run(monkeypatch, ["--dry-run"]) == 0

    captured = capsys.readouterr()
    assert "raw notes" in captured.err
    assert json.loads(captured.out)[0]["slug"] == "atlas-search"
    assert fake_collection.count_documents({}) == 0


def test_cli_forwards_instructions(monkeypatch, capsys, fake_collection):
    """
    Intent: The --instructions flag must steer the research pass, giving the CLI the
        same narrowing ability as the admin UI.
    Success: The discovery service receives exactly the supplied instruction text.
    Feature: Badge discovery — operator-steered runs.
    """
    seen: list[str | None] = []

    def fake_discover(**kwargs):
        seen.append(kwargs.get("extra_instructions"))
        return [BADGE], "n"

    monkeypatch.setattr(discover_cli, "discover_badges", fake_discover)
    monkeypatch.setattr(badge_discovery, "discover_badges", fake_discover)
    monkeypatch.setattr(
        badge_matching, "match_discovered_to_existing", lambda *a, **k: {}
    )
    run(monkeypatch, ["--instructions", "Atlas only."])
    assert seen == ["Atlas only."]


def test_cli_handles_an_empty_result(
    monkeypatch, capsys, fake_collection, stub_discovery
):
    """
    Intent: A run that finds nothing must exit cleanly and say so, rather than
        crashing or printing a misleading summary.
    Success: Exit code 0, an empty JSON array on stdout, "Found 0 badge(s)" on
        stderr.
    Feature: Badge discovery — safe handling of an empty result.
    """
    stub_discovery(badges=())
    assert run(monkeypatch, []) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert "Found 0 badge(s)" in captured.err


