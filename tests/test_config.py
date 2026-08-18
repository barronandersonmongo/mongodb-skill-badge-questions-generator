"""Tests for app/config.py — runtime configuration.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import pytest

from app.config import Settings, get_settings


def test_reads_ptm_hackathon_connection_string(monkeypatch):
    """
    Intent: Verify the app picks up the Atlas connection string from the
        PTM_HACKATHON_CONNECTION_STRING variable the .mcp.json config already uses.
    Success: get_settings().mongodb_uri equals the value of that variable.
    Feature: Configuration — Atlas credential resolution.
    """
    monkeypatch.setenv("PTM_HACKATHON_CONNECTION_STRING", "mongodb://from-ptm")
    assert get_settings().mongodb_uri == "mongodb://from-ptm"


def test_falls_back_to_mongodb_uri(monkeypatch):
    """
    Intent: Verify a developer can use the conventional MONGODB_URI variable when
        the project-specific one is not set.
    Success: get_settings().mongodb_uri equals the MONGODB_URI value.
    Feature: Configuration — Atlas credential resolution.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://from-fallback")
    assert get_settings().mongodb_uri == "mongodb://from-fallback"


def test_ptm_variable_wins_over_fallback(monkeypatch):
    """
    Intent: Pin the precedence order so a stale MONGODB_URI in a shell cannot
        silently redirect writes away from the PTM-Hackathon cluster.
    Success: With both variables set, the PTM_HACKATHON_CONNECTION_STRING value wins.
    Feature: Configuration — Atlas credential resolution.
    """
    monkeypatch.setenv("PTM_HACKATHON_CONNECTION_STRING", "mongodb://from-ptm")
    monkeypatch.setenv("MONGODB_URI", "mongodb://from-fallback")
    assert get_settings().mongodb_uri == "mongodb://from-ptm"


def test_missing_connection_string_raises_with_actionable_message():
    """
    Intent: An unconfigured environment must fail loudly at startup, naming the
        variable to set, rather than failing later with an opaque driver error.
    Success: RuntimeError is raised and its message names
        PTM_HACKATHON_CONNECTION_STRING.
    Feature: Configuration — fail-fast on missing credentials.
    """
    with pytest.raises(RuntimeError, match="PTM_HACKATHON_CONNECTION_STRING"):
        get_settings()


def test_empty_connection_string_is_treated_as_missing(monkeypatch):
    """
    Intent: An exported-but-empty variable is a common shell mistake; it must be
        rejected rather than passed to the driver as an empty URI.
    Success: RuntimeError is raised when the variable is set to an empty string.
    Feature: Configuration — fail-fast on missing credentials.
    """
    monkeypatch.setenv("PTM_HACKATHON_CONNECTION_STRING", "")
    with pytest.raises(RuntimeError):
        get_settings()


def test_defaults_target_the_agreed_database_and_model(monkeypatch):
    """
    Intent: Lock in the agreed storage target and Claude model so they cannot
        drift unnoticed — the database name and collection are what the question
        tooling will query, and the model determines generation behavior.
    Success: database == "skill-badge-questions", collection == "skill_badges",
        model == "claude-opus-5", effort == "high".
    Feature: Configuration — storage target and model defaults.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    settings = get_settings()
    assert settings.database == "skill-badge-questions"
    assert settings.skill_badges_collection == "skill_badges"
    assert settings.model == "claude-opus-5"
    assert settings.effort == "high"




def test_gateway_settings_are_read_from_the_environment(monkeypatch):
    """
    Intent: Where Claude is only reachable through an internal gateway, both the
        gateway URL and its key must come from the environment — the URL is internal
        infrastructure and this repository is public, so neither may be defaulted in
        code.
    Success: Both values are read from the environment and uses_gateway is True.
    Feature: Configuration — API gateway credentials.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    monkeypatch.setenv("GROVE_ANTHROPIC_BASE_URL", "https://gateway.example/anthropic")
    monkeypatch.setenv("GROVE_PRIMARY_KEY", "gw-secret")

    settings = get_settings()
    assert settings.gateway_base_url == "https://gateway.example/anthropic"
    assert settings.gateway_key == "gw-secret"
    assert settings.uses_gateway is True


def test_a_gateway_key_without_a_url_fails_loudly(monkeypatch):
    """
    Intent: A key with no URL is a half-finished setup that would otherwise send the
        gateway's key to the public API, where it means nothing — the operator must be
        told which variable is missing instead of getting a confusing auth error.
    Success: RuntimeError naming GROVE_ANTHROPIC_BASE_URL is raised.
    Feature: Configuration — API gateway credentials.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    monkeypatch.setenv("GROVE_PRIMARY_KEY", "gw-secret")

    with pytest.raises(RuntimeError, match="GROVE_ANTHROPIC_BASE_URL"):
        get_settings()


def test_no_gateway_configured_means_direct_api_access(monkeypatch):
    """
    Intent: The gateway is optional. With neither variable set, the app must use the
        public API and normal ANTHROPIC_API_KEY resolution.
    Success: Both gateway fields are None and uses_gateway is False.
    Feature: Configuration — API gateway credentials.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    settings = get_settings()
    assert settings.gateway_base_url is None
    assert settings.gateway_key is None
    assert settings.uses_gateway is False


def test_model_can_be_overridden_from_the_environment(monkeypatch):
    """
    Intent: A gateway may not serve every model, so the model must be overridable
        without a code change — otherwise a deployment that lacks the default model
        cannot run at all.
    Success: ANTHROPIC_MODEL overrides the default; unset leaves the default.
    Feature: Configuration — model override.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-fable-5")
    assert get_settings().model == "claude-fable-5"

    monkeypatch.delenv("ANTHROPIC_MODEL")
    assert get_settings().model == "claude-opus-5"


def test_the_secondary_gateway_key_is_used_when_the_primary_is_absent(monkeypatch):
    """
    Intent: The gateway issues two interchangeable keys so one can be rotated while
        the other serves traffic. If the primary is being rotated out, the app must
        keep working on the secondary rather than losing API access.
    Success: With only GROVE_SECONDARY_KEY set, that key is used and the gateway is
        engaged.
    Feature: Configuration — API gateway key rotation.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    monkeypatch.setenv("GROVE_ANTHROPIC_BASE_URL", "https://gateway.example/anthropic")
    monkeypatch.setenv("GROVE_SECONDARY_KEY", "secondary-secret")

    settings = get_settings()
    assert settings.gateway_key == "secondary-secret"
    assert settings.uses_gateway is True


def test_the_primary_gateway_key_wins_when_both_are_present(monkeypatch):
    """
    Intent: Both keys are normally exported together, so the precedence must be
        deterministic — the primary is the one in active use, the secondary is the
        standby held for rotation.
    Success: With both set, the primary is used.
    Feature: Configuration — API gateway key rotation.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    monkeypatch.setenv("GROVE_ANTHROPIC_BASE_URL", "https://gateway.example/anthropic")
    monkeypatch.setenv("GROVE_PRIMARY_KEY", "primary-secret")
    monkeypatch.setenv("GROVE_SECONDARY_KEY", "secondary-secret")

    assert get_settings().gateway_key == "primary-secret"


def test_gateway_environments_use_the_basic_web_search_tool(monkeypatch):
    """
    Intent: The Grove workspace rejects the dynamic-filtering search variant
        ("web_search_20260209 not supported in your workspace"), which would fail
        every discovery run. Gateway environments must therefore request the basic
        variant, which that workspace does serve.
    Success: With a gateway configured, web_search_tool is web_search_20250305.
    Feature: Configuration — web search availability per environment.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    monkeypatch.setenv("GROVE_ANTHROPIC_BASE_URL", "https://gateway.example/anthropic")
    monkeypatch.setenv("GROVE_PRIMARY_KEY", "primary-secret")

    assert get_settings().web_search_tool == "web_search_20250305"


def test_direct_api_access_uses_the_dynamic_filtering_web_search_tool(monkeypatch):
    """
    Intent: The first-party API supports the newer search variant, whose dynamic
        filtering keeps irrelevant results out of the context window. Direct access
        must not be downgraded just because a gateway needs the older one.
    Success: With no gateway configured, web_search_tool is web_search_20260209.
    Feature: Configuration — web search availability per environment.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    assert get_settings().web_search_tool == "web_search_20260209"


def test_the_web_search_tool_version_can_be_overridden(monkeypatch):
    """
    Intent: Workspace tool availability changes without notice, so an operator must
        be able to switch search variants without a code change or redeploy.
    Success: WEB_SEARCH_TOOL_TYPE overrides the per-environment default.
    Feature: Configuration — web search availability per environment.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    monkeypatch.setenv("WEB_SEARCH_TOOL_TYPE", "web_search_20250305")
    assert get_settings().web_search_tool == "web_search_20250305"


def test_the_catalog_url_is_configurable(monkeypatch):
    """
    Intent: The catalog is the authority for which badges exist, and its URL (or the
        team filter on it) can change without this program changing — so it must be
        overridable from the environment.
    Success: The default points at the published skills catalog, and
        SKILL_BADGE_CATALOG_URL overrides it.
    Feature: Configuration — authoritative catalog location.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    assert get_settings().catalog_url == "https://learn.mongodb.com/skills?team=devrel"

    monkeypatch.setenv("SKILL_BADGE_CATALOG_URL", "https://learn.mongodb.com/skills")
    assert get_settings().catalog_url == "https://learn.mongodb.com/skills"


def test_gateway_environments_use_the_basic_web_fetch_tool(monkeypatch):
    """
    Intent: The Grove workspace rejects web_fetch_20260209 the same way it rejects the
        newer search variant. Without the basic fetch tool the model cannot read the
        catalog at all, which is now the backbone of discovery.
    Success: With a gateway configured, web_fetch_tool is web_fetch_20250910; without
        one it is the dynamic-filtering variant.
    Feature: Configuration — web fetch availability per environment.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    assert get_settings().web_fetch_tool == "web_fetch_20260209"

    monkeypatch.setenv("GROVE_ANTHROPIC_BASE_URL", "https://gateway.example/anthropic")
    monkeypatch.setenv("GROVE_PRIMARY_KEY", "primary-secret")
    assert get_settings().web_fetch_tool == "web_fetch_20250910"


def test_the_credly_collection_url_is_configurable(monkeypatch):
    """
    Intent: The Credly collection is the authoritative badge set, and its URL can
        change without this program changing, so it must be overridable from the
        environment.
    Success: The default points at the MongoDB skill badges collection and
        CREDLY_COLLECTION_URL overrides it.
    Feature: Configuration — authoritative badge set location.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    assert "mongodb-skill-badges" in get_settings().credly_collection_url

    monkeypatch.setenv("CREDLY_COLLECTION_URL", "https://example.test/badges")
    assert get_settings().credly_collection_url == "https://example.test/badges"


def test_the_duplicate_similarity_floor_is_configurable(monkeypatch):
    """
    Intent: The floor decides which neighbours are worth a model call, trading scan cost
        against missed duplicates, so it must be tunable without a code change.
    Success: A default floor is set and can be overridden per Settings instance.
    Feature: Configuration — duplicate scan tuning.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    assert 0 < get_settings().duplicate_score_threshold < 1
    assert Settings(mongodb_uri="x", duplicate_score_threshold=0.5).duplicate_score_threshold == 0.5


def test_the_vector_index_name_is_configurable(monkeypatch):
    """
    Intent: The Atlas Vector Search index is created and named outside this program, so
        its name must be configurable — a mismatch means duplicate detection silently
        finds nothing.
    Success: The default is the Atlas auto-embedding index and VECTOR_INDEX_NAME
        overrides it.
    Feature: Configuration — vector index location.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    assert get_settings().vector_index_name == "skill-badge-description-vector"

    monkeypatch.setenv("VECTOR_INDEX_NAME", "another-index")
    assert get_settings().vector_index_name == "another-index"


def test_the_shortlist_floor_is_loose_enough_to_catch_reworded_duplicates():
    """
    Intent: This value used to be the decision — the score above which a pair was put to an
        LLM — and was set between the measured bands. It is now only a shortlist for the
        reranker, which is the thing that decides, so it must sit BELOW the band that
        distinct questions occupy (0.765-0.783 measured on the live index). Otherwise a
        duplicate phrased very differently is never shortlisted and the reranker never sees
        it. This replaces a test that required the opposite, because the value's job
        changed.
    Success: The shortlist floor is below the observed distinct-question band, and not so
        low that every pair is shortlisted.
    Feature: Question duplicate sweep — the shortlist favours recall, the reranker decides.
    """
    floor = Settings(mongodb_uri="mongodb://test").question_duplicate_score_threshold
    assert 0.5 < floor < 0.765


def test_the_delete_threshold_is_stricter_than_the_shortlist_floor():
    """
    Intent: Deleting is irreversible and has no judge behind it, so the bar for removing a
        question must be far higher than the bar for merely comparing one. A delete
        threshold at or below the shortlist floor would delete everything shortlisted.
    Success: The delete threshold is well above the shortlist floor.
    Feature: Question duplicate sweep — deletion requires more certainty than comparison.
    """
    settings = Settings(mongodb_uri="mongodb://test")
    assert settings.question_rerank_delete_threshold > settings.question_duplicate_score_threshold
    assert settings.question_rerank_delete_threshold >= 0.9


def test_the_questions_vector_index_can_be_renamed_without_a_code_change():
    """
    Intent: The index is created by hand in Atlas and may be rebuilt under a different
        name. If the name were only a literal in code, a rebuild would mean editing and
        redeploying the application.
    Success: QUESTIONS_VECTOR_INDEX_NAME overrides the configured index name.
    Feature: Question search — configurable index name.
    """
    import os

    os.environ["MONGODB_URI"] = "mongodb://test"
    os.environ["QUESTIONS_VECTOR_INDEX_NAME"] = "some_other_index"
    try:
        assert get_settings().questions_vector_index_name == "some_other_index"
    finally:
        del os.environ["QUESTIONS_VECTOR_INDEX_NAME"]
        del os.environ["MONGODB_URI"]


def test_the_questions_index_defaults_to_the_one_that_exists():
    """
    Intent: The default must match the index actually built in Atlas, or a fresh checkout
        searches a name that does not exist and reports every question as unique — a
        silent failure, since an empty result looks like a clean collection.
    Success: The default index name is the one created for this collection.
    Feature: Question search — default matches the deployed index.
    """
    settings = Settings(mongodb_uri="mongodb://test")
    assert settings.questions_vector_index_name == "questions_embedding_text_vector"
