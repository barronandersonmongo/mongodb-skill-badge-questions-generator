"""Tests for app/services/reranker.py — scoring pairs with Voyage's reranker.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from app.services import reranker


@pytest.fixture
def stub_voyage(monkeypatch):
    """Serve a canned rerank response and record the request that produced it."""
    state: dict = {"request": None, "payload": None, "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            state["request"] = {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": json.loads(self.rfile.read(length) or b"{}"),
            }
            body = json.dumps(state["payload"] or {"data": []}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("VOYAGE_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def test_scores_are_returned_in_the_order_the_documents_were_given(stub_voyage):
    """
    Intent: The API answers sorted by score and identifies each document by index. A
        caller pairs each score with the question it belongs to by position, so leaving
        them in the API's order would attribute one question's score to another and
        delete the wrong question.
    Success: Scores come back in input order, not the API's sorted order.
    Feature: Reranking — scores map to the documents they were computed for.
    """
    stub_voyage["payload"] = {
        "data": [
            {"index": 2, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.5},
            {"index": 1, "relevance_score": 0.1},
        ]
    }
    assert reranker.rerank_pairs("q", ["a", "b", "c"]) == [0.5, 0.1, 0.9]


def test_the_query_and_documents_are_sent_to_the_rerank_endpoint(stub_voyage):
    """
    Intent: A reranker's whole value is reading both texts together, so both sides must
        reach the API. Sending only one would return meaningless scores that still look
        like scores.
    Success: The request carries the query, the documents and the model, to /rerank.
    Feature: Reranking — the pair is sent to the reranker.
    """
    stub_voyage["payload"] = {"data": [{"index": 0, "relevance_score": 0.4}]}
    reranker.rerank_pairs("the query", ["the document"])
    request = stub_voyage["request"]
    assert request["path"].endswith("/rerank")
    assert request["body"]["query"] == "the query"
    assert request["body"]["documents"] == ["the document"]
    assert request["body"]["model"] == "rerank-2.5"


def test_the_key_is_sent_as_a_bearer_token(stub_voyage):
    """
    Intent: The API rejects an unauthenticated request, and a 401 during a sweep would be
        reported as a rerank failure rather than a missing credential.
    Success: The Authorization header carries the configured key as a bearer token.
    Feature: Reranking — authenticated requests.
    """
    stub_voyage["payload"] = {"data": [{"index": 0, "relevance_score": 0.4}]}
    reranker.rerank_pairs("q", ["d"])
    assert stub_voyage["request"]["headers"]["authorization"] == "Bearer test-key"


def test_the_model_can_be_changed_without_a_code_change(stub_voyage, monkeypatch):
    """
    Intent: rerank-2.5-lite trades accuracy for cost, and newer models will appear. The
        choice belongs in the environment, like every other setting here.
    Success: VOYAGE_RERANK_MODEL selects the model sent to the API.
    Feature: Reranking — configurable model.
    """
    monkeypatch.setenv("VOYAGE_RERANK_MODEL", "rerank-2.5-lite")
    stub_voyage["payload"] = {"data": [{"index": 0, "relevance_score": 0.4}]}
    reranker.rerank_pairs("q", ["d"])
    assert stub_voyage["request"]["body"]["model"] == "rerank-2.5-lite"


def test_a_missing_key_names_the_variable_to_set(monkeypatch):
    """
    Intent: No key is the expected first-run state — none existed when this was written.
        The failure has to say which variable to set and that a login shell's profile is
        not visible to a service, or an operator is left guessing.
    Success: RuntimeError names VOYAGE_API_KEY, and no request is attempted.
    Feature: Reranking — actionable missing-credential message.
    """
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY"):
        reranker.rerank_pairs("q", ["d"])


def test_scoring_nothing_costs_no_request(stub_voyage):
    """
    Intent: A question with no shortlisted neighbours has nothing to compare against.
        Calling the API with an empty document list spends a request to be told so.
    Success: An empty document list returns no scores and sends no request.
    Feature: Reranking — no needless API calls.
    """
    assert reranker.rerank_pairs("q", []) == []
    assert stub_voyage["request"] is None


def test_more_documents_than_the_api_accepts_is_refused_locally(stub_voyage):
    """
    Intent: The API caps a request at 1000 documents. Sending more returns an error that
        reads as a rerank failure; catching it here names the real problem and costs
        nothing.
    Success: Exceeding the limit raises before any request is made.
    Feature: Reranking — respects the API's document limit.
    """
    with pytest.raises(ValueError, match="1000"):
        reranker.rerank_pairs("q", ["d"] * 1001)
    assert stub_voyage["request"] is None


def test_a_score_for_a_document_we_did_not_send_is_ignored(stub_voyage):
    """
    Intent: An out-of-range index cannot be attributed to any document. Letting it through
        would either crash the sweep or shift every score onto the wrong question.
    Success: The bogus entry is dropped and the valid score is kept in place.
    Feature: Reranking — malformed responses cannot mis-score a pair.
    """
    stub_voyage["payload"] = {
        "data": [
            {"index": 99, "relevance_score": 1.0},
            {"index": 0, "relevance_score": 0.3},
        ]
    }
    assert reranker.rerank_pairs("q", ["a"]) == [0.3]


def test_an_api_failure_is_raised_rather_than_scored_as_zero(stub_voyage):
    """
    Intent: A failed request scored as zero would read as "definitely not duplicates",
        so a broken sweep would look like a clean collection.
    Success: A non-success status raises.
    Feature: Reranking — failures are not silently scored.
    """
    stub_voyage["status"] = 500
    with pytest.raises(httpx.HTTPStatusError):
        reranker.rerank_pairs("q", ["d"])


def test_a_document_the_api_omitted_scores_zero_rather_than_inheriting_a_score(stub_voyage):
    """
    Intent: If the response omits a document, its slot must not keep another document's
        score — that would be an unrelated pair carrying a duplicate's score, and could
        delete a question that resembles nothing.
    Success: An omitted document scores 0.0.
    Feature: Reranking — every document gets its own score or none.
    """
    stub_voyage["payload"] = {"data": [{"index": 1, "relevance_score": 0.8}]}
    assert reranker.rerank_pairs("q", ["a", "b"]) == [0.0, 0.8]
