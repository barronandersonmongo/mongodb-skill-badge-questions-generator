"""Runtime configuration, read from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    database: str = "skill-badge-questions"
    skill_badges_collection: str = "skill_badges"
    questions_collection: str = "questions"
    # Claude Opus 5: adaptive thinking is on by default; effort tunes depth.
    model: str = "claude-opus-5"
    effort: str = "high"
    # Optional API gateway (e.g. an internal Azure APIM route fronting the
    # Anthropic Messages API). When both are set, requests go through the gateway
    # authenticated with its own header instead of ANTHROPIC_API_KEY.
    gateway_base_url: str | None = None
    gateway_key: str | None = None
    gateway_key_header: str = "api-key"
    # Server-side web search and fetch. The dynamic-filtering variants are not
    # available in every workspace, so tool versions are resolved per environment.
    web_search_tool: str = "web_search_20260209"
    web_fetch_tool: str = "web_fetch_20260209"
    # The published catalog is the only authority on which badges exist. Anything
    # not evidenced on this domain is not treated as a skill badge.
    catalog_url: str = "https://learn.mongodb.com/skills?team=devrel"
    catalog_domain: str = "learn.mongodb.com"
    # The Credly collection returns the full badge set as JSON, so the badge list
    # is a deterministic fetch rather than a research result.
    credly_collection_url: str = (
        "https://www.credly.com/organizations/mongodb/collections/"
        "mongodb-skill-badges/badge_templates"
    )
    # Duplicate detection searches this Atlas Vector Search index. The index is
    # configured with autoEmbed on `description`, so Atlas embeds both the stored
    # descriptions and the query text — this program stores no vectors.
    vector_index_name: str = "skill-badge-description-vector"
    # Neighbours below this score are not worth a model call. Measured against the
    # Atlas index (voyage-4-large): real duplicates score around 0.80 and rank
    # below some non-duplicates, so this floor only trims cost — the model decides.
    duplicate_score_threshold: float = 0.75

    @property
    def uses_gateway(self) -> bool:
        return bool(self.gateway_base_url and self.gateway_key)


def get_settings() -> Settings:
    uri = os.environ.get("PTM_HACKATHON_CONNECTION_STRING") or os.environ.get(
        "MONGODB_URI"
    )
    if not uri:
        raise RuntimeError(
            "Set PTM_HACKATHON_CONNECTION_STRING (or MONGODB_URI) to the Atlas "
            "connection string for the PTM-Hackathon cluster."
        )

    # The gateway URL is deliberately not defaulted in code: it is internal
    # infrastructure and this repository is public.
    gateway_base_url = os.environ.get("GROVE_ANTHROPIC_BASE_URL") or None
    # The gateway issues two interchangeable keys so they can be rotated without
    # downtime; prefer the primary and fall back to the secondary.
    gateway_key = (
        os.environ.get("GROVE_PRIMARY_KEY")
        or os.environ.get("GROVE_SECONDARY_KEY")
        or None
    )
    if gateway_key and not gateway_base_url:
        raise RuntimeError(
            "GROVE_PRIMARY_KEY is set but GROVE_ANTHROPIC_BASE_URL is not. Set the "
            "gateway's Anthropic Messages base URL (the part before /v1/messages), "
            "or unset the Grove keys to use ANTHROPIC_API_KEY directly."
        )

    # Verified against the Grove gateway on 2026-08-17: the workspace rejects
    # web_search_20260209 ("not supported in your workspace"), while the basic
    # variant works. Direct API access keeps the newer dynamic-filtering variant.
    default_search_tool = (
        "web_search_20250305" if gateway_base_url else Settings.web_search_tool
    )
    default_fetch_tool = (
        "web_fetch_20250910" if gateway_base_url else Settings.web_fetch_tool
    )

    return Settings(
        mongodb_uri=uri,
        model=os.environ.get("ANTHROPIC_MODEL") or Settings.model,
        gateway_base_url=gateway_base_url,
        gateway_key=gateway_key,
        web_search_tool=os.environ.get("WEB_SEARCH_TOOL_TYPE") or default_search_tool,
        web_fetch_tool=os.environ.get("WEB_FETCH_TOOL_TYPE") or default_fetch_tool,
        catalog_url=os.environ.get("SKILL_BADGE_CATALOG_URL") or Settings.catalog_url,
        credly_collection_url=os.environ.get("CREDLY_COLLECTION_URL")
        or Settings.credly_collection_url,
        vector_index_name=os.environ.get("VECTOR_INDEX_NAME") or Settings.vector_index_name,
    )
