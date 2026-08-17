"""Tests for app/services/badge_discovery.py — the two Claude passes.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import anthropic
import pytest

from app.config import Settings
from app.models.skill_badge import DiscoveredBadge, DiscoveredBadges
from app.services import badge_discovery
from tests.fakes import FakeAnthropic, FakeBlock, FakeMessage, FakeParsedResponse

BADGES = DiscoveredBadges(
    badges=[
        DiscoveredBadge(
            slug="atlas-search",
            name="Atlas Search",
            description="Covers Atlas Search.",
            confidence="high",
        )
    ]
)


@pytest.fixture
def fake_client(monkeypatch):
    """Install a scripted Anthropic client; returns the installer."""

    def install(**kwargs) -> FakeAnthropic:
        client = FakeAnthropic(**kwargs)
        monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)
        return client

    return install


# --- research_badges ---


def test_research_returns_the_text_of_a_completed_turn(fake_client, settings):
    """
    Intent: The research pass must hand back Claude's notes verbatim so the
        extraction pass and the audit trail both see what Claude actually found.
    Success: The returned string is the assistant text from the completed turn.
    Feature: Badge discovery — research pass.
    """
    fake_client(stream_messages=[FakeMessage("Badge: Atlas Search")])
    assert badge_discovery.research_badges(settings=settings) == "Badge: Atlas Search"


def test_research_uses_web_search_and_the_configured_model(fake_client, settings):
    """
    Intent: Badges must be grounded in current published sources, not the model's
        training data, and must run on the configured model/effort rather than
        per-call hardcoded values.
    Success: The request carries the web_search tool, the configured model, the
        configured effort, and the research system prompt.
    Feature: Badge discovery — grounded research via web search.
    """
    client = fake_client(stream_messages=[FakeMessage("notes")])
    badge_discovery.research_badges(settings=settings)

    call = client.messages.stream_calls[0]
    assert call["model"] == settings.model
    assert call["output_config"] == {"effort": settings.effort}
    assert [t["type"] for t in call["tools"]] == [
        settings.web_search_tool,
        settings.web_fetch_tool,
    ]
    assert call["system"] == badge_discovery.RESEARCH_SYSTEM


def test_research_streams_rather_than_blocking(fake_client, settings):
    """
    Intent: A web-search research turn runs for minutes; a non-streaming request
        would hit the SDK's HTTP timeout and fail the whole run.
    Success: The research pass calls the streaming API exactly once and does not
        call the parse endpoint.
    Feature: Badge discovery — long-running research turns.
    """
    client = fake_client(stream_messages=[FakeMessage("notes")])
    badge_discovery.research_badges(settings=settings)
    assert len(client.messages.stream_calls) == 1
    assert client.messages.parse_calls == []


def test_extra_instructions_reach_the_prompt(fake_client, settings):
    """
    Intent: An admin must be able to steer a run ("Atlas badges only") from the UI
        or CLI, so operator instructions have to reach the prompt.
    Success: The supplied instruction text appears in the user message.
    Feature: Badge discovery — operator-steered runs.
    """
    client = fake_client(stream_messages=[FakeMessage("notes")])
    badge_discovery.research_badges(
        extra_instructions="Only Atlas badges.", settings=settings
    )
    prompt = client.messages.stream_calls[0]["messages"][0]["content"]
    assert "Only Atlas badges." in prompt


def test_research_resumes_a_paused_turn_and_keeps_both_halves(fake_client, settings):
    """
    Intent: Server-side web search can pause a turn mid-research. The run must
        resume and keep the notes from before the pause, otherwise a long research
        session is silently truncated to a partial answer.
    Success: Notes contain both halves; a second request is made and echoes the
        paused assistant turn back so the server can resume.
    Feature: Badge discovery — resuming paused research turns.
    """
    client = fake_client(
        stream_messages=[
            FakeMessage("first half", stop_reason="pause_turn"),
            FakeMessage("second half"),
        ]
    )
    notes = badge_discovery.research_badges(settings=settings)

    assert notes == "first half\nsecond half"
    assert len(client.messages.stream_calls) == 2
    resumed = client.messages.stream_calls[1]["messages"]
    assert resumed[-1]["role"] == "assistant"


def test_research_gives_up_after_repeated_pauses(fake_client, settings):
    """
    Intent: Resuming must be bounded so a pathologically paused turn cannot loop
        indefinitely, burning tokens with no result.
    Success: RuntimeError mentioning the still-paused state is raised.
    Feature: Badge discovery — bounded resume loop.
    """
    fake_client(stream_messages=[FakeMessage("x", stop_reason="pause_turn")] * 6)
    with pytest.raises(RuntimeError, match="still paused"):
        badge_discovery.research_badges(settings=settings)


def test_research_raises_on_a_refusal_rather_than_returning_empty(fake_client, settings):
    """
    Intent: A refused request returns HTTP 200 with empty content. Treating that as
        "no badges found" would silently wipe the reviewer's expectations, so it
        must surface as an error the admin page can display.
    Success: RuntimeError mentioning the decline is raised.
    Feature: Badge discovery — refusal handling.
    """
    fake_client(
        stream_messages=[
            FakeMessage("", stop_reason="refusal", stop_details={"category": "cyber"})
        ]
    )
    with pytest.raises(RuntimeError, match="declined"):
        badge_discovery.research_badges(settings=settings)


def test_research_ignores_non_text_blocks(fake_client, settings):
    """
    Intent: A web-search turn returns tool-use and tool-result blocks alongside the
        prose. Only the prose is research notes; the rest must not corrupt them.
    Success: Only the text block's content is returned.
    Feature: Badge discovery — research pass.
    """
    fake_client(
        stream_messages=[
            FakeMessage(
                content=[
                    FakeBlock("server_tool_use"),
                    FakeBlock("web_search_tool_result"),
                    FakeBlock("text", "the notes"),
                ]
            )
        ]
    )
    assert badge_discovery.research_badges(settings=settings) == "the notes"


# --- extract_badges ---


def test_extract_returns_validated_badges(fake_client, settings):
    """
    Intent: Only schema-validated records may reach MongoDB, so extraction must
        return typed badge objects rather than prose or raw dicts.
    Success: The badge parsed from the notes is returned with its slug intact.
    Feature: Badge discovery — structured extraction pass.
    """
    fake_client(parsed=BADGES)
    badges = badge_discovery.extract_badges("some notes", settings=settings)
    assert [b.slug for b in badges] == ["atlas-search"]


def test_extract_passes_the_schema_and_the_notes(fake_client, settings):
    """
    Intent: Extraction must be constrained by the badge schema and grounded in the
        research notes, so it cannot invent badges from the model's own knowledge.
    Success: The request carries the DiscoveredBadges schema, the extraction system
        prompt, and the notes in the user message.
    Feature: Badge discovery — structured extraction pass.
    """
    client = fake_client(parsed=BADGES)
    badge_discovery.extract_badges("some notes", settings=settings)

    call = client.messages.parse_calls[0]
    assert call["output_format"] is DiscoveredBadges
    assert call["system"] == badge_discovery.EXTRACT_SYSTEM
    assert "some notes" in call["messages"][0]["content"]


def test_extract_does_not_search_the_web_again(fake_client, settings):
    """
    Intent: Extraction is a pure transcription step. Giving it search access would
        let new, unaudited claims enter after the research notes were captured.
    Success: No tools are passed and no streaming research call is made.
    Feature: Badge discovery — separation of research and extraction.
    """
    client = fake_client(parsed=BADGES)
    badge_discovery.extract_badges("notes", settings=settings)
    assert "tools" not in client.messages.parse_calls[0]
    assert client.messages.stream_calls == []


def test_extract_raises_when_no_structured_output_came_back(fake_client, settings):
    """
    Intent: A truncated or refused extraction yields no parsed output. Returning
        nothing would look like "no badges"; it must fail loudly with the stop
        reason so the cause is diagnosable.
    Success: RuntimeError mentioning the missing structured output is raised.
    Feature: Badge discovery — extraction failure handling.
    """
    fake_client(
        parsed=FakeParsedResponse(None, stop_reason="max_tokens", stop_details=None)
    )
    with pytest.raises(RuntimeError, match="no structured output"):
        badge_discovery.extract_badges("notes", settings=settings)


def test_extract_accepts_an_empty_result(fake_client, settings):
    """
    Intent: Notes that genuinely describe no badges must extract to an empty list —
        a real result, distinct from the failure case above.
    Success: An empty list is returned without error.
    Feature: Badge discovery — safe handling of an empty result.
    """
    fake_client(parsed=DiscoveredBadges(badges=[]))
    assert badge_discovery.extract_badges("notes", settings=settings) == []


# --- discover_badges ---


def test_discover_runs_research_then_extraction(fake_client, settings):
    """
    Intent: The end-to-end entry point must run both passes in order, feed the
        notes into extraction, and return the notes as well so the run is auditable.
    Success: Notes and badges are both returned, and the notes appear in the
        extraction request.
    Feature: Badge discovery — end-to-end run.
    """
    client = fake_client(stream_messages=[FakeMessage("raw notes")], parsed=BADGES)
    badges, notes = badge_discovery.discover_badges(settings=settings)

    assert notes == "raw notes"
    assert [b.slug for b in badges] == ["atlas-search"]
    assert "raw notes" in client.messages.parse_calls[0]["messages"][0]["content"]


def test_discover_propagates_extra_instructions(fake_client, settings):
    """
    Intent: Operator steering entered in the admin UI or CLI must survive the
        end-to-end call and reach the research prompt.
    Success: The instruction text appears in the research request.
    Feature: Badge discovery — operator-steered runs.
    """
    client = fake_client(stream_messages=[FakeMessage("notes")], parsed=BADGES)
    badge_discovery.discover_badges(extra_instructions="Atlas only.", settings=settings)
    assert "Atlas only." in client.messages.stream_calls[0]["messages"][0]["content"]


def test_client_is_constructed_without_an_explicit_key(monkeypatch, settings):
    """
    Intent: Credentials must resolve from the environment (env var or `ant auth`
        profile) so no API key is ever hardcoded or threaded through app code.
    Success: The Anthropic client is constructed with no arguments at all.
    Feature: Badge discovery — credential handling.
    """
    captured = {}

    class Recorder:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(badge_discovery.anthropic, "Anthropic", Recorder)
    badge_discovery._client(settings)
    assert captured == {"args": (), "kwargs": {}}


# --- missing credentials ---


SDK_AUTH_ERROR = TypeError(
    "Could not resolve authentication method. Expected one of api_key, auth_token, "
    "or credentials to be set."
)


def test_research_translates_missing_credentials_into_an_actionable_error(
    monkeypatch, settings
):
    """
    Intent: The SDK resolves credentials lazily and raises a bare TypeError naming
        its own constructor arguments, which tells an operator nothing. The run must
        instead say which variable to set, since an unset key is the most likely
        first-run failure.
    Success: RuntimeError is raised naming ANTHROPIC_API_KEY, with the SDK's
        TypeError kept as the cause.
    Feature: Badge discovery — missing credential diagnostics.
    """
    class Raising:
        def stream(self, **kwargs):
            raise SDK_AUTH_ERROR

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY") as caught:
        badge_discovery.research_badges(settings=settings)
    assert isinstance(caught.value.__cause__, TypeError)


def test_extract_translates_missing_credentials_into_an_actionable_error(
    monkeypatch, settings
):
    """
    Intent: Extraction is a second API call, so it can be the first place a
        credential problem surfaces (for example if research was replayed from
        stored notes). It must give the same actionable message.
    Success: RuntimeError is raised naming ANTHROPIC_API_KEY.
    Feature: Badge discovery — missing credential diagnostics.
    """
    class Raising:
        def parse(self, **kwargs):
            raise SDK_AUTH_ERROR

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        badge_discovery.extract_badges("notes", settings=settings)


def test_unrelated_type_errors_are_not_disguised_as_credential_problems(
    monkeypatch, settings
):
    """
    Intent: The translation matches on the SDK's message, so a genuine programming
        TypeError must still propagate unchanged rather than being mislabelled as a
        missing key and sending the operator down the wrong path.
    Success: The original TypeError propagates.
    Feature: Badge discovery — missing credential diagnostics.
    """
    class Raising:
        def stream(self, **kwargs):
            raise TypeError("stream() got an unexpected keyword argument 'modle'")

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(TypeError, match="unexpected keyword"):
        badge_discovery.research_badges(settings=settings)


def test_extraction_failures_other_than_credentials_still_propagate(
    monkeypatch, settings
):
    """
    Intent: The credential translator sits in front of every extraction failure, so a
        real API error (rate limit, overload, network) must pass through unchanged
        rather than being swallowed or relabelled.
    Success: The original exception propagates from extract_badges.
    Feature: Badge discovery — extraction failure handling.
    """
    class Raising:
        def parse(self, **kwargs):
            raise ConnectionError("connection reset by peer")

    client = FakeAnthropic()
    client.messages = Raising()
    monkeypatch.setattr(badge_discovery, "_client", lambda settings=None: client)

    with pytest.raises(ConnectionError, match="connection reset"):
        badge_discovery.extract_badges("notes", settings=settings)


# --- API gateway ---


def test_a_gateway_request_actually_reaches_the_gateway_with_its_key(stub_gateway):
    """
    Intent: Prove the gateway client can issue a real request. Asserting on
        constructor arguments is not enough: the SDK validates auth while building
        each request and only honours a per-request omission, so a client that looks
        correctly configured can still fail before anything is sent. This test runs
        that code path against a local stub.
    Success: The request completes, and the stub received the gateway's own
        api-key header carrying the configured key.
    Feature: Badge discovery — API gateway support.
    """
    settings = Settings(
        mongodb_uri="mongodb://test",
        gateway_base_url=stub_gateway.url,
        gateway_key="gw-secret",
    )
    client = badge_discovery._client(settings)

    message = client.messages.create(
        model="claude-opus-5",
        max_tokens=16,
        messages=[{"role": "user", "content": "ping"}],
    )

    assert message.content[0].text == "pong"
    assert stub_gateway.headers["api-key"] == "gw-secret"


def test_a_gateway_url_without_a_key_does_not_engage_the_gateway(monkeypatch):
    """
    Intent: A half-configured gateway must not silently produce unauthenticated
        requests to it — a base URL alone should leave normal credential resolution
        in place rather than sending a header with no key.
    Success: With only a base URL set, the client is constructed with no arguments.
    Feature: Badge discovery — API gateway support.
    """
    captured = {}

    class Recorder:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(badge_discovery.anthropic, "Anthropic", Recorder)
    badge_discovery._client(
        Settings(mongodb_uri="mongodb://test", gateway_base_url="https://gateway")
    )
    assert captured["kwargs"] == {}


# --- catalog as the authority ---


def test_research_points_the_model_at_the_authoritative_catalog(fake_client, settings):
    """
    Intent: Earlier runs sourced badges from Credly pages and blog posts, which listed
        retired badges and things that were never skill badges. The published catalog
        is the only authority, so its URL must reach the prompt and the model must be
        able to fetch it, not merely search for it.
    Success: The catalog URL appears in the prompt and the fetch tool is offered
        alongside search.
    Feature: Badge discovery — catalog-anchored research.
    """
    client = fake_client(stream_messages=[FakeMessage("notes")])
    badge_discovery.research_badges(settings=settings)

    call = client.messages.stream_calls[0]
    assert settings.catalog_url in call["messages"][0]["content"]
    assert settings.web_fetch_tool in [t["type"] for t in call["tools"]]


def test_the_research_brief_forbids_non_catalog_evidence(settings):
    """
    Intent: The instruction that a Credly page or blog post is not evidence of a badge
        is the fix for the suspect entries. If it is ever dropped from the brief, the
        deterministic filter still protects the database, but the run wastes effort
        researching badges that will be rejected.
    Success: The research brief names the catalog as the only authority and rules out
        Credly and blog posts as evidence.
    Feature: Badge discovery — catalog-anchored research.
    """
    brief = badge_discovery.RESEARCH_SYSTEM
    assert "ONLY authority" in brief
    assert "Credly" in brief
    assert "canonical title" in brief


def test_badges_without_catalog_evidence_are_rejected(settings):
    """
    Intent: Whatever the prompt says, a badge whose only evidence is off-catalog must
        not enter the collection — this is the deterministic backstop that keeps
        retired and non-badge entries out.
    Success: The badge citing only Credly is rejected; the one citing the catalog
        domain is kept.
    Feature: Badge discovery — catalog evidence required.
    """
    good = DiscoveredBadge(
        slug="atlas-search",
        name="Atlas Search",
        description="d",
        confidence="high",
        source_urls=["https://learn.mongodb.com/skills/atlas-search"],
    )
    bad = DiscoveredBadge(
        slug="retired-thing",
        name="Retired Thing",
        description="d",
        confidence="high",
        source_urls=["https://www.credly.com/org/mongodb/badge/retired-thing"],
    )

    kept, rejected = badge_discovery.split_by_catalog_evidence(
        [good, bad], settings.catalog_domain
    )
    assert [b.slug for b in kept] == ["atlas-search"]
    assert [b.slug for b in rejected] == ["retired-thing"]


def test_badges_citing_no_source_at_all_are_rejected(settings):
    """
    Intent: A badge with no cited source cannot be verified by a reviewer, so it is
        indistinguishable from a fabrication and must not be stored.
    Success: A badge with an empty source list is rejected.
    Feature: Badge discovery — catalog evidence required.
    """
    unsourced = DiscoveredBadge(
        slug="no-sources", name="No Sources", description="d", confidence="high"
    )
    kept, rejected = badge_discovery.split_by_catalog_evidence(
        [unsourced], settings.catalog_domain
    )
    assert kept == []
    assert [b.slug for b in rejected] == ["no-sources"]


# --- catalog-driven synchronisation ---


def test_catalog_sync_stores_the_published_badge_set(monkeypatch, fake_collection):
    """
    Intent: The published collection is the authority on which badges exist, so a sync
        must store exactly what it returns — no research, no judgement about the set.
    Success: Every badge from the collection is stored and reported, with the source
        recorded as the collection.
    Feature: Badge synchronisation — sync from the published collection.
    """
    from app.services import credly_catalog

    published = [
        DiscoveredBadge(
            slug="crud-operations-in-mongodb",
            name="CRUD Operations in MongoDB",
            description="d",
            confidence="high",
            source_urls=["https://www.credly.com/org/mongodb/badge/crud"],
        )
    ]
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: list(published))
    monkeypatch.setattr(
        badge_matching_module(), "match_discovered_to_existing", lambda *a, **k: {}
    )

    summary = badge_discovery.synchronize_from_catalog()

    assert summary["discovered"] == 1
    assert summary["source"] == "credly-collection"
    assert fake_collection.find_one({"slug": "crud-operations-in-mongodb"}) is not None


def test_catalog_sync_does_not_filter_on_learn_domain(monkeypatch, fake_collection):
    """
    Intent: The catalog-evidence filter exists to police web research. Applying it to
        the collection itself would discard badges that cite only their Credly page —
        the authoritative source — so a sync must not run that filter.
    Success: A badge whose only link is a credly.com URL is still stored.
    Feature: Badge synchronisation — sync from the published collection.
    """
    from app.services import credly_catalog

    credly_only = DiscoveredBadge(
        slug="observability-for-ai-agents",
        name="Observability for AI Agents",
        description="d",
        confidence="high",
        source_urls=["https://www.credly.com/org/mongodb/badge/observability"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [credly_only])
    monkeypatch.setattr(
        badge_matching_module(), "match_discovered_to_existing", lambda *a, **k: {}
    )

    badge_discovery.synchronize_from_catalog()
    assert fake_collection.find_one({"slug": "observability-for-ai-agents"}) is not None


def test_catalog_sync_reconciles_badges_stored_under_another_slug(
    monkeypatch, fake_collection
):
    """
    Intent: Records already exist from earlier research runs under different slugs and
        hand-corrected titles. A sync must merge onto those rather than duplicating
        every badge under its Credly slug.
    Success: The match is applied, leaving one record and reporting the merge.
    Feature: Badge synchronisation — merging a re-discovered badge.
    """
    from app.repositories import skill_badges
    from app.services import credly_catalog

    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                slug="crud-operations",
                name="MongoDB CRUD Operations",
                description="Stored earlier.",
                confidence="high",
                source_urls=["https://learn.mongodb.com/skills"],
            )
        ]
    )
    published = DiscoveredBadge(
        slug="crud-operations-in-mongodb",
        name="CRUD Operations in MongoDB",
        description="From the collection.",
        confidence="high",
        source_urls=["https://www.credly.com/org/mongodb/badge/crud"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [published])
    monkeypatch.setattr(
        badge_matching_module(),
        "match_discovered_to_existing",
        lambda *a, **k: {"crud-operations-in-mongodb": "crud-operations"},
    )

    summary = badge_discovery.synchronize_from_catalog()

    assert fake_collection.count_documents({}) == 1
    assert summary["merged"] == {"crud-operations-in-mongodb": "crud-operations"}
    assert fake_collection.find_one({"slug": "crud-operations"})["description"] == (
        "From the collection."
    )


def badge_matching_module():
    from app.services import badge_matching

    return badge_matching


# --- artwork titles and embeddings in the sync ---


def test_the_sync_replaces_catalog_titles_with_artwork_titles(monkeypatch, fake_collection):
    """
    Intent: The catalog's text title and the badge's own artwork disagree, and the artwork
        is authoritative — so a sync must store the artwork title as the badge name while
        keeping the catalog wording for reference.
    Success: The stored name is the artwork title, and the catalog title is retained as
        text_title.
    Feature: Badge titles — artwork title as the canonical name.
    """
    from app.services import badge_titles, credly_catalog

    catalog_badge = DiscoveredBadge(
        slug="mongodb-overview",
        name="MongoDB Overview: Core Concepts and Architecture",
        text_title="MongoDB Overview: Core Concepts and Architecture",
        description="d",
        confidence="high",
        image_url="https://images.credly.com/x/blob",
        source_urls=["https://www.credly.com/org/mongodb/badge/overview"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [catalog_badge])
    monkeypatch.setattr(
        badge_titles, "read_title_from_image", lambda url, settings=None: "MongoDB Overview"
    )
    monkeypatch.setattr(
        badge_matching_module(), "match_discovered_to_existing", lambda *a, **k: {}
    )

    summary = badge_discovery.synchronize_from_catalog()

    doc = fake_collection.find_one({"slug": "mongodb-overview"})
    assert doc["name"] == "MongoDB Overview"
    assert doc["image_title"] == "MongoDB Overview"
    assert doc["text_title"] == "MongoDB Overview: Core Concepts and Architecture"
    assert summary["artwork_titles_read"] == 1


def test_an_already_read_artwork_title_is_not_read_again(monkeypatch, fake_collection):
    """
    Intent: Reading artwork costs a vision call per badge. A re-sync of unchanged images
        must reuse what was already read rather than paying again for the same answer.
    Success: The second sync reports no artwork reads and keeps the stored title.
    Feature: Badge titles — reusing titles already read.
    """
    from app.services import badge_titles, credly_catalog

    catalog_badge = DiscoveredBadge(
        slug="mongodb-overview",
        name="MongoDB Overview: Core Concepts and Architecture",
        description="d",
        confidence="high",
        image_url="https://images.credly.com/x/blob",
        source_urls=["https://www.credly.com/org/mongodb/badge/overview"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [catalog_badge])
    monkeypatch.setattr(
        badge_matching_module(), "match_discovered_to_existing", lambda *a, **k: {}
    )

    reads = []

    def counting_read(url, settings=None):
        reads.append(url)
        return "MongoDB Overview"

    monkeypatch.setattr(badge_titles, "read_title_from_image", counting_read)
    badge_discovery.synchronize_from_catalog()
    second = badge_discovery.synchronize_from_catalog()

    assert len(reads) == 1
    assert second["artwork_titles_read"] == 0
    assert fake_collection.find_one({"slug": "mongodb-overview"})["name"] == (
        "MongoDB Overview"
    )


def test_a_badge_with_no_artwork_keeps_its_catalog_title(monkeypatch, fake_collection):
    """
    Intent: A badge without artwork must still be storable under a usable name rather than
        losing its title because there was nothing to read.
    Success: The catalog title is stored and no artwork read is attempted.
    Feature: Badge titles — fallback when there is no artwork.
    """
    from app.services import badge_titles, credly_catalog

    catalog_badge = DiscoveredBadge(
        slug="no-art",
        name="Catalog Title",
        description="d",
        confidence="high",
        source_urls=["https://www.credly.com/org/mongodb/badge/no-art"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [catalog_badge])
    monkeypatch.setattr(
        badge_matching_module(), "match_discovered_to_existing", lambda *a, **k: {}
    )

    def fail(url, settings=None):
        raise AssertionError("no artwork read should be attempted")

    monkeypatch.setattr(badge_titles, "read_title_from_image", fail)

    badge_discovery.synchronize_from_catalog()
    assert fake_collection.find_one({"slug": "no-art"})["name"] == "Catalog Title"


def test_a_synced_badge_takes_its_slug_from_the_artwork_title(monkeypatch, fake_collection):
    """
    Intent: A rebuilt collection must reproduce the slugs a reviewer sees today, so identity
        has to follow the artwork title during the sync itself — not the Credly vanity slug,
        which is derived from the catalog's much longer wording.
    Success: The badge is stored under the artwork-derived slug, with the catalog slug kept
        only as history.
    Feature: Badge identity — slug follows the artwork title.
    """
    from app.services import badge_art, badge_titles, credly_catalog

    catalog_badge = DiscoveredBadge(
        slug="building-ai-powered-search-with-mongodb-vector-sear",
        name="Building AI-Powered Search with MongoDB Vector Search",
        text_title="Building AI-Powered Search with MongoDB Vector Search",
        description="d",
        confidence="high",
        image_url="https://images.credly.com/x",
        source_urls=["https://www.credly.com/org/mongodb/badge/x"],
    )
    monkeypatch.setattr(credly_catalog, "fetch_catalog", lambda **k: [catalog_badge])
    monkeypatch.setattr(
        badge_titles, "read_title_from_image", lambda url, settings=None: "Vector Search Fundamentals"
    )
    monkeypatch.setattr(badge_art, "fetch_image", lambda url: (b"\x89PNG", "image/png"))
    monkeypatch.setattr(
        badge_matching_module(), "match_discovered_to_existing", lambda *a, **k: {}
    )

    badge_discovery.synchronize_from_catalog()

    assert fake_collection.find_one({"slug": "vector-search-fundamentals"}) is not None
    assert fake_collection.find_one(
        {"slug": "building-ai-powered-search-with-mongodb-vector-sear"}
    ) is None


def test_mongodb_title_verification_records_titles_and_reports_gaps(monkeypatch, fake_collection):
    """
    Intent: MongoDB publishes a title for most badges but not all, and some pages are not
        indexed at all. The step must record what it finds, report what it cannot, and not
        fail the sync over either.
    Success: A found title is stored; a badge with no title is listed as not found; a raising
        lookup is listed as a failure.
    Feature: Badge titles — learn.mongodb.com verification.
    """
    from app.services import mongodb_page

    for slug, url in (
        ("found-one", "https://learn.mongodb.com/courses/found-one"),
        ("unindexed", "https://learn.mongodb.com/courses/unindexed"),
        ("broken", "https://learn.mongodb.com/courses/broken"),
    ):
        skill_badges_upsert(slug, url)

    def lookup(mongodb_url, name=None, settings=None):
        if mongodb_url.endswith("found-one"):
            return "Found One Skill Badge", mongodb_url
        if mongodb_url.endswith("unindexed"):
            return None
        raise RuntimeError("search unavailable")

    monkeypatch.setattr(mongodb_page, "fetch_indexed_title", lookup)

    result = badge_discovery.verify_mongodb_titles()

    assert result["mongodb_titles_verified"] == 1
    assert result["mongodb_titles_not_found"] == ["unindexed"]
    assert [f["slug"] for f in result["mongodb_title_failures"]] == ["broken"]
    assert fake_collection.find_one({"slug": "found-one"})["mongodb_title"] == (
        "Found One Skill Badge"
    )


def skill_badges_upsert(slug: str, mongodb_url: str) -> None:
    from app.repositories import skill_badges

    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                slug=slug,
                name=slug.replace("-", " ").title(),
                description="d",
                confidence="high",
                mongodb_url=mongodb_url,
                source_urls=["https://learn.mongodb.com/skills"],
            )
        ]
    )
