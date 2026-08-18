"""Rerank text pairs with Voyage's reranker.

MongoDB 8.3 exposes this as a `$rerank` aggregation stage, which needs no API key
and no second round trip. This cluster runs 8.0, so the same model (rerank-2.5) is
called over HTTP instead. The seam is deliberate: `rerank_pairs` is the only place
that knows how the score is obtained, so moving to the native stage later is a
change here and nowhere else.

A reranker is used rather than an LLM because scoring a pair is the whole task — a
cross-encoder does it in one cheap call, where an LLM costs a generation round
trip per pair and answers in prose that then has to be parsed.
"""

import os

import httpx

DEFAULT_MODEL = "rerank-2.5"
DEFAULT_BASE_URL = "https://api.voyageai.com/v1"
TIMEOUT_SECONDS = 60.0
# The API's own ceiling per request.
MAX_DOCUMENTS = 1000

MISSING_KEY_MESSAGE = (
    "No Voyage API key found. Set VOYAGE_API_KEY in the environment the server was "
    "started from, then restart it. Note that ~/.profile is only read by login "
    "shells, so a variable exported there is not visible to a service started "
    "another way. (On MongoDB 8.3+ the $rerank aggregation stage removes the need "
    "for a key entirely.)"
)


def api_key() -> str | None:
    return os.environ.get("VOYAGE_API_KEY") or None


def model() -> str:
    return os.environ.get("VOYAGE_RERANK_MODEL") or DEFAULT_MODEL


def base_url() -> str:
    return (os.environ.get("VOYAGE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def rerank_pairs(query: str, documents: list[str]) -> list[float]:
    """Score each document's relevance to the query, in the order given.

    The API returns results sorted by score and identified by index; they are put
    back into input order here so a caller can pair each score with the candidate
    it belongs to. Getting that mapping wrong would attribute one question's score
    to another, so it is done once, here.
    """
    if not documents:
        return []
    if len(documents) > MAX_DOCUMENTS:
        raise ValueError(
            f"{len(documents)} documents exceeds the API limit of {MAX_DOCUMENTS}."
        )
    key = api_key()
    if not key:
        raise RuntimeError(MISSING_KEY_MESSAGE)

    response = httpx.post(
        f"{base_url()}/rerank",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"query": query, "documents": documents, "model": model()},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()

    scores = [0.0] * len(documents)
    for item in payload.get("data") or []:
        index = item.get("index")
        if index is None or not 0 <= index < len(documents):
            # A score that names a document we did not send cannot be attributed
            # to anything; dropping it is safer than shifting the rest.
            continue
        scores[index] = float(item.get("relevance_score") or 0.0)
    return scores
