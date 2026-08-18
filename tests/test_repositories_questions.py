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


def test_new_questions_land_as_drafts(fake_questions):
    """
    Intent: Nothing Claude writes is publishable on arrival — a human decides. New
        questions must therefore enter review as drafts rather than being usable
        immediately.
    Success: A freshly stored question has status "draft".
    Feature: Question lifecycle — human approval gate.
    """
    questions.insert_questions([make()])
    assert fake_questions.docs[0]["status"] == "draft"


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


def test_filters_combine_rather_than_replace_each_other(fake_questions):
    """
    Intent: The screen offers status, badge and category at once. If they did not
        intersect, a filtered export would silently contain questions the author had
        excluded.
    Success: A question matching only one of two filters is not returned.
    Feature: Question filtering — filters intersect.
    """
    questions.insert_questions([make(skill_badges=["atlas-search"], categories=["search"])])
    assert questions.list_questions(skill_badge="atlas-search", category="search")
    assert questions.list_questions(skill_badge="atlas-search", category="indexing") == []
    assert questions.list_questions(status="approved", skill_badge="atlas-search") == []


def test_no_filter_returns_everything(fake_questions):
    """
    Intent: The unfiltered view must not accidentally apply a filter built from empty
        strings, which would return nothing and read as an empty collection.
    Success: Calling list_questions with no arguments returns both stored questions.
    Feature: Question filtering — unfiltered listing.
    """
    questions.insert_questions([make("First?"), make("Second?")])
    assert len(questions.list_questions()) == 2


def test_approving_a_question_records_the_decision(fake_questions):
    """
    Intent: Approval is the human judgement the tool exists to capture; it must
        persist on the question rather than living in the UI.
    Success: set_status stores the new status and reports the question was found.
    Feature: Question lifecycle — approve and reject.
    """
    question_id = questions.insert_questions([make()])["question_ids"][0]
    assert questions.set_status(question_id, "approved") is True
    assert fake_questions.docs[0]["status"] == "approved"


def test_setting_the_status_of_an_unknown_question_reports_failure(fake_questions):
    """
    Intent: A stale page acting on a deleted question must be told so, not silently
        succeed — otherwise the author believes a decision was recorded when nothing
        was written.
    Success: set_status on an unknown id returns False.
    Feature: Question lifecycle — unknown question is reported, not ignored.
    """
    assert questions.set_status("nope", "approved") is False


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


def test_the_fields_the_screen_filters_on_are_indexed(fake_questions):
    """
    Intent: Every listing filters on status, skill_badges or categories, and identity
        lookups hit question_id. Unindexed, those become collection scans as the
        collection grows — and a duplicate question_id would break identity.
    Success: Indexes exist for all four fields, with question_id unique.
    Feature: Question storage — queryable by badge, category and status.
    """
    questions.ensure_indexes()
    by_name = {index["name"]: index for index in fake_questions.indexes}
    assert {"question_id_unique", "skill_badges", "categories", "status"} <= by_name.keys()
    assert by_name["question_id_unique"]["unique"] is True


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
