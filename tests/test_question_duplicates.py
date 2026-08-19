"""Tests for app/services/question_duplicates.py — the ad-hoc duplicate sweep.

The sweep replaced an earlier design that screened every generation run with a
Claude call per candidate pair. That was accurate but slow and expensive, and the
cost fell on authoring — the one part of the workflow a person waits for. Duplicates
are now found on request, over what is stored, by one aggregation per question:
$vectorSearch shortlists and $rerank decides, both on the cluster. No language model
and no API key are involved.

The scores scripted here are rerank scores. Measured against rerank-2.5 on the live
collection, genuinely distinct questions score 0.379-0.512 and duplicates score
~0.94, so the fixtures use values from those bands.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.services import question_duplicates

DUPLICATE_SCORE = 0.94
DISTINCT_SCORE = 0.45


def stored(question_id: str, stem: str, **overrides) -> dict:
    return {
        "question_id": question_id,
        "stem": stem,
        "explanation": "Because.",
        "embedding_text": f"Question: {stem}\nExplanation: Because.",
        "skill_badges": ["atlas-search"],
        "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
        **overrides,
    }


def neighbour(question_id: str, score: float) -> dict:
    return {"question_id": question_id, "score": score}


@pytest.fixture
def collection(monkeypatch):
    """Script the stored questions and what the reranked search returns for each."""
    def install(docs: list[dict], neighbours: dict[str, list[dict]]):
        deleted: list[str] = []
        calls: list[dict] = []

        def list_questions(*args, **kwargs):
            return [d for d in docs if d["question_id"] not in deleted]

        def reranked(text, index_name, *, model, limit=5, exclude_question_id=None, **kw):
            calls.append(
                {
                    "text": text,
                    "index": index_name,
                    "model": model,
                    "limit": limit,
                    "exclude": exclude_question_id,
                }
            )
            return neighbours.get(exclude_question_id, [])

        def delete(question_id):
            deleted.append(question_id)
            return True

        monkeypatch.setattr(
            question_duplicates.questions_repo, "list_questions", list_questions
        )
        monkeypatch.setattr(
            question_duplicates.questions_repo, "reranked_by_embedding_text", reranked
        )
        monkeypatch.setattr(
            question_duplicates.questions_repo, "delete_question", delete
        )
        return deleted, calls

    return install


def test_a_pair_below_the_threshold_is_reported_and_kept(collection, settings):
    """
    Intent: A deletion here has no judge behind it and cannot be undone, so anything short
        of certain must survive for a person to look at.
    Success: A pair scoring below the threshold deletes nothing and is reported as a
        possible duplicate.
    Feature: Question duplicate sweep — only certain pairs are deleted.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "How do indexes work?")]
    deleted, _ = collection(docs, {"a": [neighbour("b", DISTINCT_SCORE)]})
    result = question_duplicates.report(settings=settings)
    assert deleted == []
    assert result["below_threshold"][0]["rerank_score"] == DISTINCT_SCORE


def test_each_pair_is_scored_once(collection, settings):
    """
    Intent: A and B are the same pair as B and A. Reporting both would list every duplicate
        twice, and acting on both could delete each half of one pair.
    Success: A mutual pair produces one comparison.
    Feature: Question duplicate sweep — each pair is compared once.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters docs?")]
    collection(
        docs,
        {
            "a": [neighbour("b", DUPLICATE_SCORE)],
            "b": [neighbour("a", DUPLICATE_SCORE)],
        },
    )
    result = question_duplicates.report(settings=settings)
    assert result["compared"] == 1


def test_the_pair_is_compared_on_the_text_that_was_embedded(collection, settings):
    """
    Intent: The shortlist comes from the embedded stem-and-explanation block. Comparing
        different text than was indexed would score a pair on something other than what
        made it a candidate.
    Success: The query text sent to the search is the stored embedding_text.
    Feature: Question duplicate sweep — shortlist and decision compare the same text.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters docs?")]
    _, calls = collection(docs, {"a": [neighbour("b", DUPLICATE_SCORE)]})
    question_duplicates.report(settings=settings)
    assert calls[0]["text"] == docs[0]["embedding_text"]


def test_the_configured_index_and_model_are_used(collection, settings):
    """
    Intent: Both are external state — the index is created by hand in Atlas, and the model
        name is what the $rerank stage dispatches on. A wrong value fails the sweep, or
        worse returns nothing and reports a clean collection.
    Success: The search is given the configured index name and rerank model.
    Feature: Question duplicate sweep — uses the configured index and model.
    """
    docs = [stored("a", "Which stage filters?")]
    _, calls = collection(docs, {})
    question_duplicates.report(settings=settings)
    assert calls[0]["index"] == settings.questions_vector_index_name
    assert calls[0]["model"] == settings.rerank_model


def test_a_question_is_never_compared_against_itself(collection, settings):
    """
    Intent: A question is a perfect match for itself, so without exclusion every question
        would be reported as its own duplicate and the sweep would delete the collection.
    Success: Each search excludes the question it was run for.
    Feature: Question duplicate sweep — a question is not its own duplicate.
    """
    docs = [stored("a", "Which stage filters?")]
    _, calls = collection(docs, {})
    question_duplicates.report(settings=settings)
    assert calls[0]["exclude"] == "a"


def test_the_more_widely_filed_question_outlives_the_narrower_one(fake_questions):
    """
    Intent: Replaces a test requiring an approved question to outlive a draft. With no
        review state there is no decision to preserve, so the tie turns on findability:
        the question filed under more badges is reachable from more places, and dropping
        it loses the most.
    Success: Of a duplicate pair, the one attributed to more badges survives.
    Feature: Duplicate detection — the most findable question survives.
    """
    wide = stored("wide", "Which stage filters?", skill_badges=["a", "b"])
    narrow = stored("narrow", "Which stage filters?", skill_badges=["a"])
    keep, drop = question_duplicates.choose_survivor(narrow, wide)
    assert keep["question_id"] == "wide"
    assert drop["question_id"] == "narrow"


def test_the_question_serving_more_badges_is_preferred(collection, settings):
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
    collection(docs, {"a": [neighbour("b", DUPLICATE_SCORE)]})
    result = question_duplicates.report(settings=settings)
    assert result["flagged"][0]["keep"] == "b"


def test_pairs_are_reported_most_similar_first(collection, settings):
    """
    Intent: A reviewer works down the list, so the likeliest duplicates must be at the top;
        otherwise the useful findings sit below the noise.
    Success: Reported pairs are ordered by descending rerank score.
    Feature: Question duplicate sweep — most likely duplicates first.
    """
    docs = [stored(i, f"Question {i}?") for i in ("a", "b", "c")]
    collection(docs, {"a": [neighbour("b", 0.30), neighbour("c", 0.80)]})
    result = question_duplicates.report(settings=settings)
    scores = [p["rerank_score"] for p in result["flagged"] + result["below_threshold"]]
    assert scores == sorted(scores, reverse=True)


def test_a_failed_comparison_does_not_abandon_the_sweep(collection, settings, monkeypatch):
    """
    Intent: The shortlist and the rerank happen in one aggregation, so any failure — a
        transient index error, a rate limit on the reranker — arrives the same way. Losing
        the findings for every other question would silently narrow the sweep, and must
        never be read as "no duplicates here". Replaces two earlier tests that separated
        search failures from rerank failures, which is no longer a distinction.
    Success: The failure is reported in errors and the sweep completes.
    Feature: Question duplicate sweep — partial failures are reported, not fatal.
    """
    docs = [stored("a", "Which stage filters?")]
    collection(docs, {})

    def explode(*args, **kwargs):
        raise RuntimeError("index not found")

    monkeypatch.setattr(
        question_duplicates.questions_repo, "reranked_by_embedding_text", explode
    )
    result = question_duplicates.report(settings=settings)
    assert result["flagged"] == []
    assert "index not found" in result["errors"][0]


def test_an_empty_collection_sweeps_without_comparing_anything(collection, settings):
    """
    Intent: A sweep of nothing must be free and quiet — it will be run out of habit on a
        collection that is new or has just been emptied.
    Success: Nothing is compared and no search is made.
    Feature: Question duplicate sweep — no needless work.
    """
    _, calls = collection([], {})
    result = question_duplicates.report(settings=settings)
    assert result["compared"] == 0 and calls == []


def test_a_neighbour_that_is_no_longer_stored_is_ignored(collection, settings):
    """
    Intent: The vector index lags deletions, so a search can return a question that has just
        been removed — including one the same sweep deleted moments earlier. Comparing
        against it would report a pair whose second half no longer exists, and could delete
        the survivor of an already-resolved duplicate.
    Success: A neighbour absent from the collection is skipped, and nothing is compared.
    Feature: Question duplicate sweep — tolerates an index lagging behind deletions.
    """
    docs = [stored("a", "Which stage filters?")]
    collection(docs, {"a": [neighbour("ghost", DUPLICATE_SCORE)]})
    result = question_duplicates.report(settings=settings)
    assert result["compared"] == 0


def test_the_threshold_is_configurable_and_only_flags(collection):
    """
    Intent: Replaces a test where lowering the threshold turned a reported pair into a
        deleted one. The sweep no longer deletes, so the threshold decides what is flagged
        for a person to look at — which makes it a shortlist filter rather than the thing
        that decides which questions die. It still has to be tunable from configuration,
        because the collection will change character as it spans more badges.
    Success: Lowering the threshold moves a pair from below-threshold to flagged, and still
        deletes nothing.
    Feature: Question duplicate sweep — a tunable flagging threshold.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters docs?")]
    deleted, _ = collection(docs, {"a": [neighbour("b", DISTINCT_SCORE)]})
    settings = Settings(
        mongodb_uri="mongodb://test", question_rerank_delete_threshold=0.4
    )
    result = question_duplicates.report(settings=settings)
    assert len(result["flagged"]) == 1
    assert result["below_threshold"] == []
    assert deleted == []

def test_the_number_of_neighbours_compared_is_bounded_by_configuration(collection, settings):
    """
    Intent: Comparing every pair grows as the square of the collection, so the neighbour
        count is the only thing bounding the sweep's cost. If it were ignored the sweep would
        get slower and more expensive without limit as questions accumulate.
    Success: The configured neighbour count is passed as the search limit.
    Feature: Question duplicate sweep — bounded cost.
    """
    docs = [stored("a", "Which stage filters?")]
    _, calls = collection(docs, {})
    question_duplicates.report(settings=settings)
    assert calls[0]["limit"] == settings.question_duplicate_neighbours


# --- finding reports, deleting is separate ---


def test_a_confident_pair_is_flagged_rather_than_deleted(collection, settings):
    """
    Intent: Replaces a test requiring a confident pair to lose a question. Deleting during
        the sweep made the operator choose between a safe mode and a destructive one before
        seeing the collection — and since the safe mode is strictly more informative, nobody
        should have run the other first. The sweep now reports and deletion is a separate
        act on a list somebody has read.
    Success: A pair above the threshold is flagged, with the question to drop and the one to
        keep both named, and nothing is deleted.
    Feature: Question duplicate sweep — finding never deletes.
    """
    docs = [stored("a", "Which stage filters?"), stored("b", "Which stage filters docs?")]
    deleted, _ = collection(docs, {"a": [neighbour("b", DUPLICATE_SCORE)]})
    result = question_duplicates.report(settings=settings)
    assert deleted == []
    assert len(result["flagged"]) == 1
    assert result["flagged"][0]["drop"] and result["flagged"][0]["keep"]
    assert result["flagged"][0]["rerank_score"] == DUPLICATE_SCORE


def test_the_report_says_which_threshold_it_used(collection, settings):
    """
    Intent: The threshold is a measured judgement, not a fact, and the operator is being
        asked to act on a list it produced. Reporting the number is what lets them see that
        a pair at 0.86 was flagged by a hair rather than by certainty.
    Success: The report carries the threshold it flagged against.
    Feature: Question duplicate sweep — the threshold is visible in the report.
    """
    collection([], {})
    result = question_duplicates.report(settings=settings)
    assert result["threshold"] == settings.question_rerank_delete_threshold
