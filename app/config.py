"""Runtime configuration, read from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mongodb_uri: str
    database: str = "skill-badge-questions"
    skill_badges_collection: str = "skill_badges"
    questions_collection: str = "questions"
    doc_pages_collection: str = "doc_pages"
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
    # The questions index: autoEmbed on `embedding_text` (voyage-4-large), so Atlas
    # embeds both the stored questions and the query text.
    questions_vector_index_name: str = "questions_embedding_text_vector"
    # Neighbours below this score are not worth a model call. Measured against the
    # Atlas index (voyage-4-large): real duplicates score around 0.80 and rank
    # below some non-duplicates, so this floor only trims cost — the model decides.
    duplicate_score_threshold: float = 0.75
    # The duplicate sweep shortlists with vector search and decides with the native
    # $rerank stage, in one aggregation. Nothing here needs an API key: the cluster
    # runs the reranker, as it does the embedding.
    rerank_model: str = "rerank-2.5"
    # What $rerank must score for two questions to be treated as the same one.
    # Measured on 2026-08-18 against rerank-2.5 on the live collection: five
    # genuinely distinct questions scored 0.379-0.512 against each other, a
    # deliberately reworded copy scored 0.945, and identical text scored 0.941 (the
    # reranker does not return 1.0 for identity). This sits in the wide gap between
    # those bands.
    question_rerank_delete_threshold: float = 0.85
    # How many neighbours each question is shortlisted against.
    question_duplicate_neighbours: int = 5
    # Documentation search. The index is configured with autoEmbed on `text`, so Atlas
    # embeds both the stored pages and the query — this program stores no vectors and
    # needs no embedding API key. Searching by meaning rather than by keyword is what
    # lets an author ask for a topic ("how do I model a one-to-many relationship")
    # without knowing the words the documentation happens to use.
    doc_pages_vector_index_name: str = "doc_pages_text_vector"
    doc_pages_vector_path: str = "text"
    # A page is not compared against the whole corpus: Atlas narrows to this many
    # approximate neighbours before scoring them exactly. Roughly 10x the result cap,
    # which is the usual recommendation for recall at this corpus size.
    doc_search_num_candidates: int = 500
    # Question authoring reads its source material out of this corpus rather than
    # searching the web: a run that fetches the web spends most of its wall clock
    # waiting, and two runs on the same badge see different source text. One search
    # per topic area rather than one per badge, because the top pages for a single
    # badge-wide query cluster on one topic — and five questions off one page is the
    # failure mode this tool exists to avoid.
    doc_context_pages_per_topic: int = 3
    # Pages are long and a badge has several topic areas, so both the per-page share
    # and the whole context are bounded. A page cut short is still usable material;
    # an authoring turn that will not fit is not.
    doc_context_page_chars: int = 24_000
    doc_context_char_budget: int = 360_000
    # --- badge-scoped page walk ---
    # Questions are written one documentation page at a time, over the set of pages
    # that belong to a badge. A badge is resolved to that set first, then walked: each
    # page is read once, yields several questions, and coverage is a counter against an
    # enumerable list rather than a guess. Cost then arrives per badge, when someone
    # asks for that badge, instead of as one sweep of the whole corpus.
    #
    # How wide the set is drawn. Deliberately much wider than the old single-prompt
    # retrieval: the point is to enumerate a badge's material, not to pick the best few
    # pages that fit in one request.
    doc_page_set_per_topic: int = 60
    doc_page_set_size: int = 400
    # Pages further away than this are not this badge's material. Measured against the
    # Atlas index (voyage-4-large) on the Cluster Reliability badge: pages plainly about
    # the badge scored 0.70-0.86, while the noise its Credly tags dragged in — IP access
    # lists, VPC peering — sat at 0.64-0.69.
    doc_page_set_score_floor: float = 0.70
    # Reference material is excluded from question material. Measured 2026-08-19:
    # 3,318 of 7,162 stored pages are parameter lists, CLI synopses and command
    # references, and a question written from a parameter list tests lookup, not skill.
    doc_reference_url_pattern: str = r"/reference/|/cli/|/api/|/command/"
    # How many questions one page is asked for, and how many pages a single run walks.
    # The page cap bounds a run's wall clock and cost; the walk resumes where it left
    # off, because pages already written from are skipped.
    questions_per_page: int = 3
    # Reading one page and writing three questions from it is a bounded task, not the
    # open-ended research the badge-wide path does. Output tokens — thinking most of
    # all — dominate the cost of a walk, so the effort is tuned separately here rather
    # than inheriting the `high` used for research.
    page_author_effort: str = "medium"
    # --- what a run costs ---
    # Claude Opus 5 list prices, dollars per million tokens, as published on
    # 2026-08-19. Cached input reads at a tenth and writes at 1.25x. These are only
    # used to report what a run has spent: the figure on the screen is computed from
    # the token counts Claude actually returns, not from an estimate of them, so the
    # only thing that can be wrong here is the price itself. Update alongside `model`.
    cost_input_per_mtok: float = 5.00
    cost_output_per_mtok: float = 25.00
    cost_cache_read_per_mtok: float = 0.50
    cost_cache_write_per_mtok: float = 6.25
    max_pages_per_run: int = 25
    # MongoDB publishes an agent-oriented index of its documentation, and serves
    # every page as Markdown. That is the only enumerable route to the whole corpus:
    # search-knowledge returns the best chunks for a query and cannot be asked "give
    # me everything".
    docs_index_url: str = "https://www.mongodb.com/docs/llms.txt"
    # ~10,000 pages, so the crawl is concurrent — but politely so, against a site
    # this program does not own.
    docs_fetch_concurrency: int = 8
    docs_request_timeout: float = 30.0
    # Below this, a documentation page is a navigation stub — a title and a list of
    # links to the real pages. Measured on the first full crawl: every page under 500
    # bytes was one of those (drivers/csharp-drivers.md is 108 bytes and only points at
    # the C# driver docs), while the corpus averages 10 KB. They are excluded because a
    # question cannot be written from a link list, and they crowd the source listings.
    # Deliberately conservative: nav stubs continue up to roughly 900 bytes, but so do
    # a few genuinely short pages, and dropping real content is the worse mistake.
    docs_min_page_bytes: int = 500
    # The docs are served through CloudFront, which returns 403 when it decides a client
    # is asking for too much. Retrying with a growing pause clears a transient block;
    # hammering through it does not, and risks a longer one.
    docs_retry_attempts: int = 3
    docs_retry_backoff_seconds: float = 2.0
    # If this many pages fail in a row, the crawl is being refused rather than hitting
    # bad links. Stopping then keeps what was fetched, leaves the corpus intact, and
    # produces one clear message instead of seven thousand identical failures.
    docs_block_threshold: int = 25

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
        questions_vector_index_name=os.environ.get("QUESTIONS_VECTOR_INDEX_NAME")
        or Settings.questions_vector_index_name,
        doc_pages_vector_index_name=os.environ.get("DOC_PAGES_VECTOR_INDEX_NAME")
        or Settings.doc_pages_vector_index_name,
        docs_index_url=os.environ.get("DOCS_INDEX_URL") or Settings.docs_index_url,
    )
