"""Tests for app/repositories/questions.py — storing and filtering questions.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from app.models.question import GeneratedQuestion, QuestionOption
from app.repositories import questions


def make(stem: str = "Which stage filters documents?", **overrides) -> GeneratedQuestion:
    return GeneratedQuestion(
        **{
            "stem": stem,
            "options": [
                QuestionOption(text="$match", is_correct=True, rationale="filters"),
                QuestionOption(text="$project", is_correct=False, rationale="reshapes"),
                QuestionOption(text="$sort", is_correct=False, rationale="orders"),
                QuestionOption(text="$limit", is_correct=False, rationale="truncates"),
            ],
            "explanation": "$match filters.",
            "difficulty": "foundational",
            "categories": ["aggregation"],
            "skill_badges": ["atlas-search"],
            **overrides,
        }
    )


def test_every_question_gets_its_own_identity(fake_questions):
    """
    Intent: Two questions on the same topic are legitimately different questions, so
        identity cannot be derived from content. Without a per-question id the review
        actions could not address one question.
    Success: Two questions stored in one run get different question_ids.
    Feature: Question identity — a generated id, not a content slug.
    """
    result = questions.insert_questions([make("First?"), make("Second?")])
    ids = [d["question_id"] for d in fake_questions.docs]
    assert len(set(ids)) == 2
    assert result["question_ids"] == ids


def test_questions_from_one_run_share_a_run_id(fake_questions):
    """
    Intent: A run's output must be traceable back to the run that produced it, so a
        bad batch can be found and removed after the fact.
    Success: Both questions carry the same generation_run_id, and it is reported.
    Feature: Question provenance — generation runs are identifiable.
    """
    result = questions.insert_questions([make("First?"), make("Second?")])
    run_ids = {d["generation_run_id"] for d in fake_questions.docs}
    assert run_ids == {result["run_id"]}


def test_storing_nothing_costs_no_write(fake_questions):
    """
    Intent: A run can legitimately produce no usable question. That must be reported
        as an empty result, not raise, and must not create an empty run record.
    Success: insert_questions([]) reports zero inserted and writes nothing.
    Feature: Question generation — an empty run is a result, not an error.
    """
    result = questions.insert_questions([])
    assert result == {"run_id": None, "inserted": 0, "question_ids": []}
    assert fake_questions.docs == []


def test_listings_do_not_leak_mongos_own_key(fake_questions):
    """
    Intent: The listing is also the export — it is returned as JSON to be pasted
        elsewhere. Mongo's _id is not part of this program's model and is not JSON
        serialisable, so it must never reach the response.
    Success: No listed question carries an _id.
    Feature: Question export — clean JSON output.
    """
    questions.insert_questions([make()])
    assert all("_id" not in q for q in questions.list_questions())


def test_questions_are_listed_newest_first(fake_questions):
    """
    Intent: The author's usual question is "what did that run just produce?", so the
        newest work must be at the top rather than buried under earlier batches.
    Success: The most recently created question is listed first.
    Feature: Question review screen — newest work first.
    """
    questions.insert_questions([make("Older?")])
    fake_questions.docs[0]["created_at"] = fake_questions.docs[0]["created_at"].replace(year=2020)
    questions.insert_questions([make("Newer?")])
    assert questions.list_questions()[0]["stem"] == "Newer?"


def test_a_question_is_found_by_any_of_its_badges(fake_questions):
    """
    Intent: A question serving several badges must be findable under each of them —
        that is the whole reason skill_badges is an array. Matching only the first
        element would hide shared questions.
    Success: A two-badge question is returned when filtering by its second badge.
    Feature: Question filtering — by skill badge.
    """
    questions.insert_questions([make(skill_badges=["atlas-search", "aggregation"])])
    assert len(questions.list_questions(skill_badge="aggregation")) == 1
    assert questions.list_questions(skill_badge="indexing") == []


def test_a_question_is_found_by_any_of_its_categories(fake_questions):
    """
    Intent: Categories are the second axis authors browse by, and are equally an
        array; the same containment behaviour must hold.
    Success: A two-category question is returned when filtering by its second
        category.
    Feature: Question filtering — by category.
    """
    questions.insert_questions([make(categories=["aggregation", "indexing"])])
    assert len(questions.list_questions(category="indexing")) == 1
    assert questions.list_questions(category="search") == []


def test_no_filter_returns_everything(fake_questions):
    """
    Intent: The unfiltered view must not accidentally apply a filter built from empty
        strings, which would return nothing and read as an empty collection.
    Success: Calling list_questions with no arguments returns both stored questions.
    Feature: Question filtering — unfiltered listing.
    """
    questions.insert_questions([make("First?"), make("Second?")])
    assert len(questions.list_questions()) == 2


def test_a_question_can_be_deleted(fake_questions):
    """
    Intent: An author must be able to remove a question outright, not only reject it,
        so an off-topic or embarrassing draft need not be kept.
    Success: delete_question removes the document and reports success.
    Feature: Question lifecycle — delete.
    """
    question_id = questions.insert_questions([make()])["question_ids"][0]
    assert questions.delete_question(question_id) is True
    assert fake_questions.docs == []


def test_deleting_an_unknown_question_reports_failure(fake_questions):
    """
    Intent: A double-click or a stale page must not report a successful delete of
        something that was already gone.
    Success: delete_question on an unknown id returns False.
    Feature: Question lifecycle — unknown question is reported, not ignored.
    """
    assert questions.delete_question("nope") is False


def test_the_category_menu_lists_each_category_once(fake_questions):
    """
    Intent: The filter menu is built from the categories actually in use. Repeating a
        category once per question that uses it would make the menu unusable as the
        collection grows.
    Success: A category shared by two questions appears once, and the list is sorted.
    Feature: Question filtering — category menu.
    """
    questions.insert_questions(
        [make("First?", categories=["search", "aggregation"]), make("Second?", categories=["search"])]
    )
    assert questions.categories_in_use() == ["aggregation", "search"]


def test_the_repository_targets_the_configured_questions_collection(monkeypatch):
    """
    Intent: Questions must land in their own collection in the configured database.
        Writing them into the badge collection would corrupt the badge catalog, and
        nothing else in the program would notice.
    Success: collection() resolves to the configured database and questions collection.
    Feature: Question storage — the configured storage target.
    """
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")
    resolved = questions.collection()
    assert resolved.name == "questions"
    assert resolved.database.name == "skill-badge-questions"


# --- the combined field a vector index points at ---


def test_a_stored_question_carries_the_combined_embedding_text(fake_questions):
    """
    Intent: The field exists to be the path an Atlas auto-embedding index points at. If
        it were only composed by some later maintenance step, every freshly generated
        question would be missing from the index until that step ran.
    Success: A question stored by a generation run already carries the combined text.
    Feature: Question embedding text — composed on write.
    """
    questions.insert_questions([make()])
    stored = fake_questions.docs[0]
    assert stored["embedding_text"] == (
        "Question: Which stage filters documents?\nExplanation: $match filters."
    )


def test_the_embedding_field_has_a_stable_name(fake_questions):
    """
    Intent: An Atlas index definition names this path, and that definition lives outside
        this repository. Renaming the field would silently stop the index matching
        anything, with no error anywhere in this program.
    Success: The constant and the stored key are both "embedding_text".
    Feature: Question embedding text — stable external contract.
    """
    assert questions.EMBEDDING_FIELD == "embedding_text"
    questions.insert_questions([make()])
    assert questions.EMBEDDING_FIELD in fake_questions.docs[0]


def test_the_combined_text_is_returned_by_listings(fake_questions):
    """
    Intent: The listing is the export. Omitting the embedded text would make the exported
        JSON an incomplete copy of the document, so a re-import could not reproduce what
        the index was built from.
    Success: The field appears on a listed question.
    Feature: Question export — includes the embedding text.
    """
    questions.insert_questions([make()])
    assert "embedding_text" in questions.list_questions()[0]


def test_a_question_written_before_the_field_existed_is_backfilled(fake_questions):
    """
    Intent: A vector index skips a document whose indexed path is absent, and an empty
        search result is indistinguishable from a question that was never written. Older
        questions must therefore be able to gain the field without being regenerated.
    Success: The backfill composes the field for a document that has none.
    Feature: Question embedding text — backfill for existing questions.
    """
    fake_questions.docs.append(
        {"question_id": "old1", "stem": "An older question?", "explanation": "Because."}
    )
    result = questions.backfill_embedding_text()
    assert result["written"] == 1
    assert fake_questions.docs[0]["embedding_text"] == (
        "Question: An older question?\nExplanation: Because."
    )


def test_the_backfill_leaves_correct_documents_alone(fake_questions):
    """
    Intent: The backfill is safe to re-run, so it must not rewrite every document each
        time — that would be a full collection write on every invocation and, with
        autoEmbed, could trigger needless re-embedding of unchanged text.
    Success: A question whose field already matches is reported as correct, not written.
    Feature: Question embedding text — backfill is idempotent.
    """
    questions.insert_questions([make()])
    result = questions.backfill_embedding_text()
    assert result == {"written": 0, "already_correct": 1}


def test_the_backfill_recomposes_text_that_no_longer_matches(fake_questions):
    """
    Intent: A stem corrected by hand in Atlas would leave the embedded text describing the
        old wording, so vector search would keep matching on text no longer shown to
        anyone. Drift has to be repairable.
    Success: A stale combined value is rewritten from the current stem and explanation.
    Feature: Question embedding text — drift is repaired.
    """
    questions.insert_questions([make()])
    fake_questions.docs[0]["stem"] = "A corrected stem?"
    questions.backfill_embedding_text()
    assert fake_questions.docs[0]["embedding_text"].startswith("Question: A corrected stem?")


# --- vector search over the embedding text ---


def test_similar_questions_are_searched_on_the_embedded_field(fake_questions):
    """
    Intent: The Atlas index embeds `embedding_text`, so the search must query that path
        with the query text itself — autoEmbed means Atlas embeds both sides and this
        program stores no vectors. Querying another path would match nothing.
    Success: The pipeline is a $vectorSearch on embedding_text carrying the query text and
        the given index name.
    Feature: Question search — queries the auto-embedding index.
    """
    captured = {}

    def aggregate(pipeline):
        captured["stage"] = pipeline[0]["$vectorSearch"]
        return []

    fake_questions.aggregate = aggregate
    questions.similar_by_embedding_text("joining collections", "questions_embedding_text_vector")
    assert captured["stage"]["path"] == "embedding_text"
    assert captured["stage"]["query"] == "joining collections"
    assert captured["stage"]["index"] == "questions_embedding_text_vector"


def test_search_results_carry_the_similarity_score(fake_questions):
    """
    Intent: A ranked list without scores hides how weak the tail is, so an author cannot
        tell a real match from the closest of several poor ones.
    Success: Each result carries a score, and Mongo's own key is not returned.
    Feature: Question search — scores are visible.
    """
    questions.insert_questions([make("Which stage filters documents?")])
    results = questions.similar_by_embedding_text("stage filters documents", "ix")
    assert results and results[0]["score"] > 0
    assert "_id" not in results[0]


def test_a_question_is_not_returned_as_its_own_nearest_neighbour(fake_questions):
    """
    Intent: Rescreening a stored question would otherwise always find itself as a perfect
        match and report every question as a duplicate of itself.
    Success: Excluding a question by id removes it from its own results.
    Feature: Question search — a question is not its own duplicate.
    """
    question_id = questions.insert_questions([make()])["question_ids"][0]
    results = questions.similar_by_embedding_text(
        "Which stage filters documents?", "ix", exclude_question_id=question_id
    )
    assert all(r["question_id"] != question_id for r in results)


def test_the_search_returns_no_more_than_the_requested_limit(fake_questions):
    """
    Intent: The limit bounds both the response size and, downstream, how many model calls
        a duplicate screen can make. An ignored limit would make cost unpredictable.
    Success: Asking for one result returns one.
    Feature: Question search — bounded result set.
    """
    questions.insert_questions([make("First stage question?"), make("Second stage question?")])
    assert len(questions.similar_by_embedding_text("stage question", "ix", limit=1)) == 1


def test_the_reranked_search_shortlists_then_reranks_in_one_aggregation(fake_questions):
    """
    Intent: The whole point of the native stage is that both steps happen on the cluster in
        one query — no API key, no second round trip. A pipeline missing $rerank would
        silently return vector scores, which are on a different scale, so the sweep would
        compare them against a rerank threshold and delete the wrong things.
    Success: The pipeline is $vectorSearch then $rerank on the embedded field, with the same
        query text, and projects the rerank score from $meta.
    Feature: Question duplicate sweep — native shortlist-then-rerank pipeline.
    """
    captured = {}

    def aggregate(pipeline):
        captured["pipeline"] = pipeline
        return []

    fake_questions.aggregate = aggregate
    questions.reranked_by_embedding_text("joining collections", "ix", model="rerank-2.5")

    stages = [next(iter(stage)) for stage in captured["pipeline"]]
    assert stages == ["$vectorSearch", "$rerank", "$project"]
    rerank = captured["pipeline"][1]["$rerank"]
    assert rerank["path"] == "embedding_text"
    assert rerank["query"] == {"text": "joining collections"}
    assert rerank["model"] == "rerank-2.5"
    assert captured["pipeline"][2]["$project"]["score"] == {"$meta": "score"}


def test_the_reranked_search_asks_the_reranker_for_every_shortlisted_document(fake_questions):
    """
    Intent: numDocsToRerank bounds how many candidates the reranker scores. If it were
        smaller than the shortlist, the tail would keep its vector score — mixing two
        incompatible scales in one result set, where a high vector score could pass a rerank
        threshold and delete a question that is merely on-topic.
    Success: numDocsToRerank matches the shortlist size.
    Feature: Question duplicate sweep — every shortlisted candidate is reranked.
    """
    captured = {}

    def aggregate(pipeline):
        captured["pipeline"] = pipeline
        return []

    fake_questions.aggregate = aggregate
    questions.reranked_by_embedding_text(
        "text", "ix", model="rerank-2.5", limit=3, exclude_question_id="q1"
    )
    assert (
        captured["pipeline"][1]["$rerank"]["numDocsToRerank"]
        == captured["pipeline"][0]["$vectorSearch"]["limit"]
    )


# --- what a badge has already been written from ---


def test_the_pages_a_badge_has_been_written_from_are_queryable(fake_questions):
    """
    Intent: A walk resumes by skipping the pages a badge already has questions from, so
        that set has to be derivable from the questions themselves. Kept anywhere else it
        could disagree with what was actually stored.
    Success: The source URLs of a badge's questions come back as a set.
    Feature: Question coverage — the pages a badge has been written from.
    """
    fake_questions.docs.extend([
        {"skill_badges": ["atlas-search"], "source_urls": ["https://x/a.md"]},
        {"skill_badges": ["atlas-search"], "source_urls": ["https://x/b.md", "https://x/a.md"]},
        {"skill_badges": ["indexing"], "source_urls": ["https://x/c.md"]},
    ])
    assert questions.source_urls_for_badge("atlas-search") == {
        "https://x/a.md",
        "https://x/b.md",
    }


def test_a_badge_with_no_questions_has_used_no_pages(fake_questions):
    """
    Intent: The first walk of a badge must not be blocked by an empty result being
        mistaken for an error. An empty set is the correct answer, not a missing one.
    Success: A badge with no questions returns an empty set.
    Feature: Question coverage — a badge that has never been walked.
    """
    assert questions.source_urls_for_badge("atlas-search") == set()


def test_a_cross_filed_question_counts_for_every_badge_it_tests(fake_questions):
    """
    Intent: Questions are deliberately filed under every badge they test. The question the
        coverage screen answers is "does this badge have enough", so a question serving
        two badges has to count towards both — counting it once would understate every
        badge it was cross-filed into.
    Success: A question filed under two badges counts for both.
    Feature: Question coverage — cross-filed questions count for each badge.
    """
    fake_questions.docs.append({"skill_badges": ["atlas-search", "indexing"]})
    counts = questions.counts_by_badge()
    assert counts["atlas-search"] == 1
    assert counts["indexing"] == 1


# --- no review workflow ---


def test_a_stored_question_carries_no_review_state(fake_questions):
    """
    Intent: Replaces a test requiring new questions to land as drafts behind a human
        approval gate. At thousands of questions nobody works a queue of drafts, so the
        gate was a bottleneck rather than a safeguard, and a question nobody had blessed
        was indistinguishable from one nobody wanted. A question that passes the format
        check is usable.
    Success: A freshly stored question has no status field at all.
    Feature: Question lifecycle — no review state.
    """
    questions.insert_questions([make()])
    assert "status" not in fake_questions.docs[0]


def test_the_fields_the_screen_filters_on_are_still_indexed(fake_questions):
    """
    Intent: Replaces a test that also required a status index. Every listing filters on
        skill_badges or categories and identity lookups hit question_id; unindexed those
        become collection scans as the collection grows, and a duplicate question_id would
        break identity. The status index is now an index on a field nothing writes.
    Success: Indexes exist for question_id, skill_badges and categories, with question_id
        unique, and no status index is created.
    Feature: Question storage — queryable by badge and category.
    """
    questions.ensure_indexes()
    by_name = {index["name"]: index for index in fake_questions.indexes}
    assert {"question_id_unique", "skill_badges", "categories"} <= by_name.keys()
    assert "status" not in by_name
    assert by_name["question_id_unique"]["unique"] is True


def test_badge_and_category_filters_combine(fake_questions):
    """
    Intent: Replaces a test that intersected a status filter as well. The screen still
        offers badge and category together, and if they did not intersect a filtered
        export would silently contain questions the author had excluded.
    Success: A question matching only one of two filters is not returned.
    Feature: Question filtering — filters intersect.
    """
    questions.insert_questions([make(skill_badges=["atlas-search"], categories=["search"])])
    assert questions.list_questions(skill_badge="atlas-search", category="search")
    assert questions.list_questions(skill_badge="atlas-search", category="indexing") == []


def test_questions_are_counted_per_badge(fake_questions):
    """
    Intent: Replaces a test that split the counts by status. With no review state there is
        one number that matters per badge — how many questions it has — and the coverage
        screen reads it to decide where the next run should go.
    Success: Counts come back as one number per badge.
    Feature: Question coverage — per-badge counts.
    """
    fake_questions.docs.extend([
        {"skill_badges": ["atlas-search"]},
        {"skill_badges": ["atlas-search"]},
        {"skill_badges": ["indexing"]},
    ])
    counts = questions.counts_by_badge()
    assert counts == {"atlas-search": 2, "indexing": 1}


def test_a_legacy_status_field_can_be_stripped(fake_questions):
    """
    Intent: Questions written before the workflow was dropped still carry `status`, and it
        rides along in the JSON export — telling whoever consumes it that a question is an
        unfinished draft when no such state exists. Left in place it is a lie in the
        deliverable.
    Success: The field is removed and the number of changed documents reported.
    Feature: Question storage — the legacy review field can be cleaned up.
    """
    fake_questions.docs.extend([
        {"question_id": "a", "status": "draft"},
        {"question_id": "b"},
    ])
    assert questions.drop_status_field() == 1
    assert all("status" not in doc for doc in fake_questions.docs)


def test_stripping_the_legacy_field_twice_changes_nothing(fake_questions):
    """
    Intent: A maintenance action that is unsafe to repeat is one an operator has to
        remember the state of. This one is run from a button, so running it again must be
        a no-op rather than an error.
    Success: A second call reports nothing changed.
    Feature: Question storage — the cleanup is idempotent.
    """
    fake_questions.docs.append({"question_id": "a", "status": "draft"})
    questions.drop_status_field()
    assert questions.drop_status_field() == 0


# --- finding one question by its identifier ---


def test_a_question_is_findable_by_the_id_the_program_keys_on(fake_questions):
    """
    Intent: An author refers to one question in a message or a ticket by its identifier, and
        needs to get back to it afterwards. `question_id` is what every endpoint takes, so it
        is the one that has to resolve.
    Success: Looking up a question_id returns that question.
    Feature: Question lookup — by question_id.
    """
    ids = questions.insert_questions([make("One?"), make("Two?")])["question_ids"]
    found = questions.find_by_identifier(ids[0])
    assert len(found) == 1 and found[0]["question_id"] == ids[0]


def test_a_question_is_findable_by_the_id_atlas_shows(fake_questions):
    """
    Intent: MongoDB's `_id` is projected out of every listing, so it is the identifier an
        author only ever sees in Atlas or Compass — which is exactly the moment they want the
        question it belongs to. Accepting only the program's own id would mean knowing which
        kind of identifier you are holding before you can use it.
    Success: Looking up a document's ObjectId returns that question.
    Feature: Question lookup — by MongoDB's ObjectId.
    """
    questions.insert_questions([make("One?")])
    object_id = fake_questions.docs[0]["_id"]
    found = questions.find_by_identifier(str(object_id))
    assert len(found) == 1 and found[0]["stem"] == "One?"


def test_a_malformed_identifier_finds_nothing_rather_than_raising(fake_questions):
    """
    Intent: A search box is exactly where malformed input arrives — a truncated paste, a
        stray quote. Constructing an ObjectId from a non-ObjectId raises, and a 500 on a
        mistyped search is a worse answer than "nothing found".
    Success: An unparseable identifier returns no results and does not raise.
    Feature: Question lookup — malformed identifiers are handled.
    """
    assert questions.find_by_identifier("not-an-id") == []
    assert questions.find_by_identifier("") == []
    assert questions.find_by_identifier("zzzzzzzzzzzzzzzzzzzzzzzz") == []


def test_an_identifier_is_told_apart_from_a_search_phrase(fake_questions):
    """
    Intent: One box serves both purposes — paste an id to find one question, type a phrase to
        find several — because making the reader pick the right box first is friction for no
        gain. Both identifiers are hex of a fixed length, and neither is anything a person
        would type as a search.
    Success: Identifier-shaped values are recognised and ordinary phrases are not.
    Feature: Question lookup — identifiers are recognised by shape.
    """
    assert questions.looks_like_an_identifier("a" * 32) is True
    assert questions.looks_like_an_identifier("a" * 24) is True
    assert questions.looks_like_an_identifier("joining collections") is False
    assert questions.looks_like_an_identifier("a" * 20) is False
    # Hex-looking but the wrong length: a partial paste must not be treated as an id, or
    # the reader gets "no such question" for something that was never a question id.
    assert questions.looks_like_an_identifier("abc123") is False


def test_a_question_can_be_unfiled_from_one_of_its_badges(fake_questions):
    """
    Intent: A question may be filed under several badges, and being filed under one it does
        not really test is a smaller mistake than the question being wrong — correctable in
        place rather than by deleting and paying to generate a replacement.
    Success: remove_skill_badge pulls the named badge and leaves the rest of the question,
        including its other badges, untouched.
    Feature: Question lifecycle — a skill badge can be removed from a question.
    """
    question_id = questions.insert_questions(
        [make(skill_badges=["atlas-search", "vector-search"])]
    )["question_ids"][0]
    assert questions.remove_skill_badge(question_id, "atlas-search") == "removed"
    stored = questions.list_questions()[0]
    assert stored["skill_badges"] == ["vector-search"]
    assert stored["stem"] == make().stem


def test_the_only_badge_on_a_question_cannot_be_removed(fake_questions):
    """
    Intent: A question filed under nothing is unreachable from every screen that lists by
        badge and absent from export — still stored, still paid for, never seen again. That is
        worse than the wrong badge, and worse than deleting the question outright, which at
        least says what happened.
    Success: remove_skill_badge refuses when the badge is the only one, reports which refusal
        it was, and leaves the badge in place.
    Feature: Question lifecycle — a question keeps at least one skill badge.
    """
    question_id = questions.insert_questions([make(skill_badges=["atlas-search"])])[
        "question_ids"
    ][0]
    assert questions.remove_skill_badge(question_id, "atlas-search") == "last"
    assert questions.list_questions()[0]["skill_badges"] == ["atlas-search"]


def test_removing_a_badge_a_question_does_not_have_is_reported(fake_questions):
    """
    Intent: A stale page can ask to remove a badge another tab already removed, or name a
        question that has since been deleted. Reporting that as a success would tell the
        reader an edit landed that never did.
    Success: remove_skill_badge distinguishes "no such question or badge" from the refusal to
        remove the last one.
    Feature: Question lifecycle — an impossible badge removal is reported, not ignored.
    """
    question_id = questions.insert_questions(
        [make(skill_badges=["atlas-search", "vector-search"])]
    )["question_ids"][0]
    assert questions.remove_skill_badge(question_id, "data-modeling") == "missing"
    assert questions.remove_skill_badge("nope", "atlas-search") == "missing"
