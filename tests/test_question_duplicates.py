"""Tests for app/services/question_duplicates.py — the ad-hoc duplicate sweep.

The sweep replaced an earlier design that screened every generation run with a
Claude call per candidate pair. That was accurate but slow and expensive, and the
cost fell on authoring — the one part of the workflow a person waits for. Duplicates
are now found on request, over what is stored, by vector search shortlisting and a
reranker deciding. No language model is involved.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.services import question_duplicates


def stored(question_id: str, stem: str, **overrides) -> dict:
    return {
        "question_id": question_id,
        "stem": stem,
        "explanation": "Because.",
        "embedding_text": f"Question: {stem}\nExplanation: Because.",
        "status": "draft",
        "skill_badges": ["atlas-search"],
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        **overrides,
    }


@pytest.fixture
def collection(monkeypatch):
    """Script the stored questions and what the vector index proposes for each."""
    def install(docs: list[dict], neighbours: dict[str, list[dict]]):
        deleted: list[str] = []

        def list_questions(*args, **kwargs):
            return [d for d in docs if d["question_id"] not in deleted]

        def similar(text, index_name, *, limit=5, exclude_question_id=None, **kwargs):
            return neighbours.get(exclude_question_id, [])

        def delete(question_id):
            deleted.append(question_id)
            return True

        monkeypatch.setattr(question_duplicates.questions_repo, "list_questions", list_questions)
        monkeypatch.setattr(
            question_duplicates.questions_repo, "similar_by_embedding_text", similar
        )
        monkeypatch.setattr(question_duplicates.questions_repo, "delete_question", delete)
        return deleted

    return install


@pytest.fixture
def reranks(monkeypatch):
    """Script the reranker's scores, and record what it was asked to score."""
    def install(scores):
        calls: list[tuple[str, list[str]]] = []

        def rerank(query, documents):
            calls.append((query, list(documents)))
            if callable(scores):
                return scores(query, documents)
            return [scores] * len(documents)

        monkeypatch.setattr(question_duplicates, "rerank_pairs", rerank)
        return calls

    return install


def test_a_pair_the_reranker_is_sure_about_loses_one_question(collection, reranks, settings):
    """
    Intent: The sweep exists to remove repetition from the collection. If a confident pair
        were only reported, the collection would stay duplicated and the sweep would be a
        report generator.
    Success: A pair scoring above the delete threshold has one question deleted, and the
        deletion is reported.
    Feature: Question duplicate sweep — clear duplicates are deleted.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters documents?")]
    deleted = collection(docs, {"a": [{"question_id": "b", "score": 0.9}]})
    reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert deleted == [result["deleted"][0]["drop"]]
    assert result["deleted"][0]["rerank_score"] == 0.99


def test_a_pair_below_the_threshold_is_reported_and_kept(collection, reranks, settings):
    """
    Intent: A deletion here has no judge behind it and cannot be undone, so anything short
        of certain must survive for a person to look at.
    Success: A pair scoring below the threshold deletes nothing and is reported as a
        possible duplicate.
    Feature: Question duplicate sweep — only certain pairs are deleted.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "How do indexes work?")]
    deleted = collection(docs, {"a": [{"question_id": "b", "score": 0.8}]})
    reranks(0.42)
    result = question_duplicates.sweep(settings=settings)
    assert deleted == []
    assert result["possible_duplicates"][0]["rerank_score"] == 0.42


def test_a_dry_run_deletes_nothing_but_says_what_it_would(collection, reranks, settings):
    """
    Intent: The delete threshold was set without real duplicates to calibrate against, so
        it has to be checkable against live data before it is trusted to remove anything.
        A dry run is how that check is made safely.
    Success: With delete=False nothing is deleted, and the pair is marked as one that would
        have been.
    Feature: Question duplicate sweep — dry run for calibrating the threshold.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters documents?")]
    deleted = collection(docs, {"a": [{"question_id": "b", "score": 0.9}]})
    reranks(0.99)
    result = question_duplicates.sweep(delete=False, settings=settings)
    assert deleted == []
    assert result["dry_run"] is True
    assert result["possible_duplicates"][0]["would_delete"] is True


def test_the_vector_score_only_shortlists(collection, reranks, settings):
    """
    Intent: Two independently embedded texts cannot distinguish "same question reworded"
        from "same topic". The reranker reads both together and is the thing that decides;
        a high vector score alone must never delete anything.
    Success: A pair with a very high vector score but a low rerank score survives.
    Feature: Question duplicate sweep — the reranker decides, not the vector score.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage sorts?")]
    deleted = collection(docs, {"a": [{"question_id": "b", "score": 0.99}]})
    reranks(0.10)
    result = question_duplicates.sweep(settings=settings)
    assert deleted == []
    assert result["possible_duplicates"][0]["vector_score"] == 0.99


def test_a_distant_neighbour_is_never_reranked(collection, reranks, settings):
    """
    Intent: The shortlist floor is the only thing keeping the sweep from reranking every
        pair in the collection, which grows as the square of its size.
    Success: A neighbour below the shortlist floor produces no rerank call.
    Feature: Question duplicate sweep — the shortlist bounds the cost.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Unrelated question?")]
    collection(docs, {"a": [{"question_id": "b", "score": 0.20}]})
    calls = reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert calls == []
    assert result["compared"] == 0


def test_each_pair_is_reranked_once(collection, reranks, settings):
    """
    Intent: A and B are the same pair as B and A. Reranking both doubles the cost to reach
        the same answer, and would report the duplicate twice.
    Success: A mutual pair produces one comparison.
    Feature: Question duplicate sweep — each pair costs one comparison.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters documents?")]
    collection(
        docs,
        {
            "a": [{"question_id": "b", "score": 0.9}],
            "b": [{"question_id": "a", "score": 0.9}],
        },
    )
    calls = reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert len(calls) == 1
    assert result["compared"] == 1


def test_the_pair_is_compared_on_the_text_that_was_embedded(collection, reranks, settings):
    """
    Intent: The shortlist comes from the embedded stem-and-explanation block. Reranking
        different text than was shortlisted would score a pair on something other than
        what made it a candidate.
    Success: Both sides sent to the reranker are the stored embedding_text values.
    Feature: Question duplicate sweep — shortlist and decision compare the same text.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters documents?")]
    collection(docs, {"a": [{"question_id": "b", "score": 0.9}]})
    calls = reranks(0.99)
    question_duplicates.sweep(settings=settings)
    assert calls[0][0] == docs[0]["embedding_text"]
    assert calls[0][1] == [docs[1]["embedding_text"]]


def test_an_approved_question_outlives_a_draft(collection, reranks, settings):
    """
    Intent: Approval is a human decision the tool exists to capture. Deleting the approved
        question of a pair and keeping the unreviewed one would throw away exactly the work
        that matters.
    Success: The approved question is kept and the draft dropped, whichever way round the
        pair is found.
    Feature: Question duplicate sweep — review work survives.
    """
    docs = [stored("a", "Draft one?"), stored("b", "Approved one?", status="approved")]
    collection(docs, {"a": [{"question_id": "b", "score": 0.9}]})
    reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert result["deleted"][0]["keep"] == "b"
    assert result["deleted"][0]["drop"] == "a"


def test_the_question_serving_more_badges_is_preferred(collection, reranks, settings):
    """
    Intent: Between two equal drafts, the one attributed to more badges is reachable from
        more places, so keeping it loses the least findability.
    Success: The question with more skill badges is kept.
    Feature: Question duplicate sweep — keeps the more widely useful question.
    """
    docs = [
        stored("a", "One badge?", skill_badges=["atlas-search"]),
        stored("b", "Two badges?", skill_badges=["atlas-search", "aggregation"]),
    ]
    collection(docs, {"a": [{"question_id": "b", "score": 0.9}]})
    reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert result["deleted"][0]["keep"] == "b"


def test_a_question_is_not_deleted_twice_over(collection, reranks, settings):
    """
    Intent: Three near-identical questions produce overlapping pairs. Acting on each pair
        independently could delete both of a pair whose survivor was already removed,
        leaving no copy of the question at all.
    Success: With three mutually similar questions, at most two are deleted and the
        skipped pair is reported.
    Feature: Question duplicate sweep — never deletes every copy.
    """
    docs = [stored(i, f"Near duplicate {i}?") for i in ("a", "b", "c")]
    deleted = collection(
        docs,
        {
            "a": [{"question_id": "b", "score": 0.9}, {"question_id": "c", "score": 0.9}],
            "b": [{"question_id": "c", "score": 0.9}],
        },
    )
    reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert len(deleted) == 2
    assert any(p.get("skipped") for p in result["possible_duplicates"])
    assert len(docs) - len(deleted) == 1


def test_pairs_are_reported_most_similar_first(collection, reranks, settings):
    """
    Intent: A reviewer works down the list, so the likeliest duplicates must be at the top;
        otherwise the useful findings sit below the noise.
    Success: Reported pairs are ordered by descending rerank score.
    Feature: Question duplicate sweep — most likely duplicates first.
    """
    docs = [stored(i, f"Question {i}?") for i in ("a", "b", "c")]
    collection(
        docs,
        {"a": [{"question_id": "b", "score": 0.9}, {"question_id": "c", "score": 0.9}]},
    )
    reranks(lambda query, documents: [0.30, 0.80])
    result = question_duplicates.sweep(settings=settings)
    scores = [p["rerank_score"] for p in result["possible_duplicates"]]
    assert scores == sorted(scores, reverse=True)


def test_a_failed_rerank_does_not_abandon_the_sweep(collection, reranks, settings, monkeypatch):
    """
    Intent: A rate limit or network blip on one question must not discard the findings for
        every other question, and must never be read as "no duplicates here".
    Success: The failure is reported in errors and the sweep still completes.
    Feature: Question duplicate sweep — partial failures are reported, not fatal.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters documents?")]
    collection(docs, {"a": [{"question_id": "b", "score": 0.9}]})

    def explode(query, documents):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(question_duplicates, "rerank_pairs", explode)
    result = question_duplicates.sweep(settings=settings)
    assert result["deleted"] == []
    assert "rate limited" in result["errors"][0]


def test_an_unsearchable_question_does_not_abandon_the_sweep(collection, reranks, settings, monkeypatch):
    """
    Intent: One question failing to search — a transient index error, say — must not lose
        the results for the rest, which would silently narrow the sweep.
    Success: The search failure is reported and the sweep completes.
    Feature: Question duplicate sweep — partial failures are reported, not fatal.
    """
    docs = [stored("a", "Which stage filters?")]
    collection(docs, {})

    def explode(*args, **kwargs):
        raise RuntimeError("index not found")

    monkeypatch.setattr(
        question_duplicates.questions_repo, "similar_by_embedding_text", explode
    )
    result = question_duplicates.sweep(settings=settings)
    assert "index not found" in result["errors"][0]


def test_an_empty_collection_sweeps_without_calling_anything(collection, reranks, settings):
    """
    Intent: A sweep of nothing must be free — no rerank request, no error — because it will
        be run out of habit on a collection that has just been emptied or is new.
    Success: Nothing is compared and no rerank call is made.
    Feature: Question duplicate sweep — no needless API calls.
    """
    collection([], {})
    calls = reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert result["compared"] == 0 and calls == []


def test_the_delete_threshold_is_configurable(collection, reranks):
    """
    Intent: The threshold governs irreversible deletion and was set without real duplicates
        to calibrate against. It must be tunable from configuration once a dry run shows
        what real scores look like.
    Success: Lowering the threshold turns a reported pair into a deleted one.
    Feature: Question duplicate sweep — tunable delete threshold.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters documents?")]
    deleted = collection(docs, {"a": [{"question_id": "b", "score": 0.9}]})
    reranks(0.60)
    settings = Settings(mongodb_uri="mongodb://test", question_rerank_delete_threshold=0.5)
    question_duplicates.sweep(settings=settings)
    assert len(deleted) == 1


def test_a_neighbour_that_is_no_longer_stored_is_ignored(collection, reranks, settings):
    """
    Intent: The vector index lags deletions, so a search can return a question that has just
        been removed — including one the same sweep deleted moments earlier. Comparing
        against it would report a pair whose second half no longer exists, and could delete
        the survivor of an already-resolved duplicate.
    Success: A neighbour absent from the collection is skipped, and nothing is compared.
    Feature: Question duplicate sweep — tolerates an index lagging behind deletions.
    """
    docs = [stored("a", "Which stage filters?")]
    collection(docs, {"a": [{"question_id": "ghost", "score": 0.99}]})
    calls = reranks(0.99)
    result = question_duplicates.sweep(settings=settings)
    assert result["compared"] == 0 and calls == []
