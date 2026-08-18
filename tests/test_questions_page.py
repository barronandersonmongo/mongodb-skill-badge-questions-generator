"""Tests for the questions screen at / — the tool's main screen.

Assertions are on markup (elements, classes, data- attributes), never on a bare
word: the page ships JavaScript containing the same labels it renders, so a
substring check on a label passes whether or not the element was rendered.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import re

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError

from app.main import app
from app.models.question import GeneratedQuestion, QuestionOption
from app.models.skill_badge import DiscoveredBadge
from app.repositories import questions, skill_badges
from app.routers import questions as api_module

PAGE = "/"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    """The page footer names the storage target, so settings must resolve."""
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def reset_run_state():
    api_module._run_state.update(
        running=False, last_result=None, last_error=None, last_traceback=None
    )
    yield
    api_module._run_state.update(
        running=False, last_result=None, last_error=None, last_traceback=None
    )


def seed_badge(**overrides) -> None:
    skill_badges.upsert_badges(
        [
            DiscoveredBadge(
                **{
                    "slug": "atlas-search",
                    "name": "Atlas Search",
                    "description": "Covers Atlas Search indexes and queries.",
                    "confidence": "high",
                    "categories": ["search"],
                    "source_urls": ["https://learn.mongodb.com/atlas-search"],
                    **overrides,
                }
            )
        ]
    )


def seed_question(stem: str = "Which stage filters documents?", **overrides) -> str:
    result = questions.insert_questions(
        [
            GeneratedQuestion(
                **{
                    "stem": stem,
                    "options": [
                        QuestionOption(text="$match", is_correct=True, rationale="it filters"),
                        QuestionOption(text="$project", is_correct=False, rationale="it reshapes"),
                        QuestionOption(text="$sort", is_correct=False, rationale="it orders"),
                        QuestionOption(text="$limit", is_correct=False, rationale="it truncates"),
                    ],
                    "explanation": "Only $match filters documents.",
                    "difficulty": "intermediate",
                    "categories": ["aggregation"],
                    "skill_badges": ["atlas-search"],
                    "source_urls": ["https://mongodb.com/docs/match"],
                    **overrides,
                }
            )
        ]
    )
    return result["question_ids"][0]


# --- where the screen lives ---


def test_the_site_root_serves_the_questions_screen(client, fake_collection, fake_questions):
    """
    Intent: Writing and reviewing questions is the work this tool exists for, so it
        is the service's front door — reached directly, not via a redirect out of
        some other area.
    Success: GET / returns the questions screen itself, with no redirect.
    Feature: Author surface — questions are the root of the service.
    """
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert 'data-stem="true"' in response.text or 'data-empty="true"' in response.text


def test_the_questions_screen_is_not_inside_the_admin_area(
    client, fake_collection, fake_questions
):
    """
    Intent: /admin is for curating the badge catalog; questions are the authoring
        surface. Serving the questions screen from both places would blur a boundary
        that exists to give each screen one audience — and would leave two URLs to
        keep working.
    Success: The questions screen is not served under /admin.
    Feature: Author surface — separated from the admin area.
    """
    assert client.get("/admin/questions", follow_redirects=False).status_code == 404


# --- the shell ---


def test_the_page_renders_the_shared_shell(client, fake_collection, fake_questions):
    """
    Intent: The questions screen is one of several admin screens; it must render
        inside the shared nav shell so navigation between them exists and is
        consistent.
    Success: The page carries the shared navbar with the Questions link marked active.
    Feature: Admin area — shared page shell.
    """
    response = client.get(PAGE)
    assert response.status_code == 200
    assert '<nav class="navbar' in response.text
    assert re.search(r'class="nav-link active"[^>]*\n?\s*href="/"', response.text)


def test_the_navigation_offers_the_badge_screen_marked_as_admin(
    client, fake_collection, fake_questions
):
    """
    Intent: Generation depends on badges, so an author who needs to fix a badge must be
        able to reach that screen from here rather than typing a URL. Marking it as the
        admin area is what tells them they are crossing into curation work — nothing
        else does, since there are no authorizations to stop them.
    Success: The page links to /admin/skill-badges and labels it as admin.
    Feature: Author surface — signposted route into the admin area.
    """
    body = client.get(PAGE).text
    assert 'href="/admin/skill-badges"' in body
    assert 'data-admin-area="true"' in body


def test_an_unreachable_database_is_explained_rather_than_crashing(
    client, monkeypatch, fake_questions
):
    """
    Intent: A wrong or unlisted connection string is the likeliest setup mistake. A
        stack trace tells an operator nothing about which variable to fix, and a blank
        page reads as "no questions yet".
    Success: The page still returns 200 and shows a danger alert naming the failure.
    Feature: Question review screen — storage failures are explained.
    """
    def explode(*args, **kwargs):
        raise ServerSelectionTimeoutError("no route to host")

    monkeypatch.setattr(questions, "list_questions", explode)
    response = client.get(PAGE)
    assert response.status_code == 200
    assert 'class="alert alert-danger"' in response.text
    assert "no route to host" in response.text


# --- viewing questions ---


def test_a_stored_question_is_shown_with_its_options(client, fake_collection, fake_questions):
    """
    Intent: Reviewing a question means reading the stem and all four options together;
        a listing that showed only stems would make the screen useless for its purpose.
    Success: The stem and every option text appear in the rendered markup.
    Feature: Question review screen — questions are readable in full.
    """
    seed_question()
    body = client.get(PAGE).text
    assert 'data-stem="true"' in body
    assert "Which stage filters documents?" in body
    for option in ("$match", "$project", "$sort", "$limit"):
        assert option in body


def test_the_correct_answer_is_marked(client, fake_collection, fake_questions):
    """
    Intent: A reviewer cannot judge a question without knowing which option is intended
        to be right. This is an authoring tool, not a quiz, so the answer is shown
        rather than hidden.
    Success: Exactly one option is marked correct in the markup.
    Feature: Question review screen — the intended answer is visible.
    """
    seed_question()
    assert client.get(PAGE).text.count('data-correct="true"') == 1


def test_each_options_rationale_is_shown(client, fake_collection, fake_questions):
    """
    Intent: The point of the tool is question quality. A distractor is judged by the
        misconception it claims to catch, so the rationale must be on screen next to
        the option, not buried in the export.
    Success: The rationale text of a wrong option is rendered.
    Feature: Question review screen — per-option rationale is visible.
    """
    seed_question()
    body = client.get(PAGE).text
    assert "it reshapes" in body
    assert 'data-explanation="true"' in body


def test_a_questions_badges_categories_and_difficulty_are_shown(
    client, fake_collection, fake_questions
):
    """
    Intent: These three fields are what the collection is organised by. Shown on the
        question, a mis-tagged question is obvious during review — which is the only
        point at which it is cheap to fix.
    Success: The badge slug, category and difficulty each render as tagged markup.
    Feature: Question review screen — classification is visible.
    """
    seed_question()
    body = client.get(PAGE).text
    assert 'data-badge-tag="atlas-search"' in body
    assert 'data-category-tag="aggregation"' in body
    assert 'data-difficulty="true"' in body


def test_an_empty_collection_says_what_to_do_next(client, fake_collection, fake_questions):
    """
    Intent: A blank screen is indistinguishable from a broken one. The first-run state
        must name the action that fills it.
    Success: With no questions the page renders its empty state.
    Feature: Question review screen — first-run guidance.
    """
    assert 'data-empty="true"' in client.get(PAGE).text


def test_filtered_emptiness_is_distinguished_from_an_empty_collection(
    client, fake_collection, fake_questions
):
    """
    Intent: "No questions match these filters" and "no questions exist" call for
        different actions. Conflating them sends an author to generate questions they
        already have.
    Success: With a filter applied and no match, the empty state mentions the filters.
    Feature: Question review screen — filtered empty state.
    """
    seed_question()
    body = client.get(PAGE, params={"status": "approved"}).text
    assert 'data-empty="true"' in body
    assert "match these filters" in body


# --- filtering ---


def test_the_status_tabs_count_the_questions_in_each_state(
    client, fake_collection, fake_questions
):
    """
    Intent: The counts are how an author sees there is review work waiting without
        clicking every tab.
    Success: With one draft, the All and Drafts tabs both show 1 and Approved shows 0.
    Feature: Question review screen — status tabs with counts.
    """
    seed_question()
    body = client.get(PAGE).text
    tabs = [
        (label.strip(), count)
        for label, count in re.findall(
            r'>([^<>]+?)<span class="badge text-bg-secondary">(\d+)</span>', body
        )
    ]
    assert ("All", "1") in tabs
    assert ("Drafts", "1") in tabs
    assert ("Approved", "0") in tabs


def test_filtering_by_status_narrows_the_list(client, fake_collection, fake_questions):
    """
    Intent: The tabs must actually filter. A tab that changed only the highlight would
        misrepresent what state the collection is in.
    Success: Filtering to approved hides the draft question.
    Feature: Question review screen — status filter.
    """
    seed_question()
    assert "Which stage filters documents?" not in client.get(
        PAGE, params={"status": "approved"}
    ).text


def test_filtering_by_badge_and_category_narrows_the_list(
    client, fake_collection, fake_questions
):
    """
    Intent: Badge and category are the two axes an author browses by — usually "show me
        what exists for this badge before I generate more". Filters that did not
        intersect with the tabs would show a misleading view.
    Success: Each filter returns only the matching question.
    Feature: Question review screen — filter by badge and category.
    """
    seed_question("Search question?", skill_badges=["atlas-search"], categories=["search"])
    seed_question("Agg question?", skill_badges=["aggregation"], categories=["aggregation"])
    by_badge = client.get(PAGE, params={"skill_badge": "atlas-search"}).text
    assert "Search question?" in by_badge and "Agg question?" not in by_badge
    by_category = client.get(PAGE, params={"category": "aggregation"}).text
    assert "Agg question?" in by_category and "Search question?" not in by_category


def test_the_tabs_keep_the_badge_and_category_filters(client, fake_collection, fake_questions):
    """
    Intent: An author working through one badge switches status tabs constantly. If a
        tab dropped the badge filter they would land in the whole collection and lose
        their place.
    Success: A tab link rendered under a badge filter carries that filter too.
    Feature: Question review screen — filters survive tab changes.
    """
    seed_badge()
    seed_question()
    body = client.get(PAGE, params={"skill_badge": "atlas-search"}).text
    assert 'href="/?status=draft&skill_badge=atlas-search' in body


def test_the_filter_menus_offer_the_badges_and_categories_in_use(
    client, fake_collection, fake_questions
):
    """
    Intent: Filters an author has to type are filters they cannot discover. The menus
        must be built from the data actually stored, not a hardcoded list.
    Success: The badge menu offers the stored badge and the category menu the stored
        category.
    Feature: Question review screen — filter menus built from stored data.
    """
    seed_badge()
    seed_question()
    body = client.get(PAGE).text
    assert '<option value="atlas-search"' in body
    assert '<option value="aggregation"' in body


# --- exporting ---


def test_the_export_link_carries_the_current_filters(client, fake_collection, fake_questions):
    """
    Intent: Export is a stated requirement, and the author expects to get what they are
        looking at. An export link that ignored the filters would quietly hand over the
        whole collection.
    Success: Under a badge filter, the export link requests that filter from the API.
    Feature: Question export — exports what is on screen.
    """
    seed_badge()
    seed_question()
    body = client.get(PAGE, params={"status": "approved", "skill_badge": "atlas-search"}).text
    assert 'id="export-btn"' in body
    assert "/api/questions?status=approved&skill_badge=atlas-search" in body


# --- generating ---


def test_the_generate_form_offers_every_badge(client, fake_collection, fake_questions):
    """
    Intent: A run is scoped by badge, so the picker must list the badges that exist —
        including candidates, since an author may want questions for a badge still
        under review.
    Success: The picker is a multiple-select containing the stored badge.
    Feature: Question generation — badge selection.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'id="badge-picker"' in body and "multiple" in body
    assert "Atlas Search" in body


def test_the_generate_form_takes_a_count_material_and_instructions(
    client, fake_collection, fake_questions
):
    """
    Intent: Batch size, pasted training material and free-text steering are the three
        controls an author has over a run. Missing from the form, they are unreachable
        however well the API supports them.
    Success: All three inputs are present on the page.
    Feature: Question generation — author controls on the screen.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'id="count"' in body
    assert 'id="source-material"' in body
    assert 'id="instructions"' in body


def test_generation_is_not_offered_when_there_are_no_badges(
    client, fake_collection, fake_questions
):
    """
    Intent: With no badges a run cannot be scoped, so offering the button would produce
        a run that fails. Saying why, and linking to the badge screen, is the useful
        response.
    Success: The generate button is disabled and the page explains, linking to badges.
    Feature: Question generation — blocked without a badge scope.
    """
    body = client.get(PAGE).text
    assert 'data-no-badges="true"' in body
    assert re.search(r'id="generate-btn"[^>]*disabled', body, re.S)


def test_a_run_in_progress_is_reported_when_the_page_loads(
    client, fake_collection, fake_questions
):
    """
    Intent: A run outlives the page — an author reloading, or opening a second tab,
        must see the run is going rather than starting a second one.
    Success: With a run marked running, the page starts polling on load.
    Feature: Question generation — run state survives a page load.
    """
    api_module._run_state["running"] = True
    assert "if (true) { pollStatus(); }" in client.get(PAGE).text


def test_a_finished_runs_result_is_shown(client, fake_collection, fake_questions):
    """
    Intent: After a reload the run alert is gone, so the page itself must report what
        the last run produced — otherwise a completed run looks like it did nothing.
    Success: A success alert reports the number of questions stored and requested.
    Feature: Question generation — the last run's result is reported.
    """
    api_module._run_state["last_result"] = {
        "inserted": 3,
        "requested": 5,
        "run_id": "abcdef1234",
        "rejected": [],
    }
    body = client.get(PAGE).text
    assert 'class="alert alert-success"' in body
    assert "3 question(s) stored" in body
    assert "of 5 requested" in body


def test_discarded_questions_are_reported_with_their_reason(
    client, fake_collection, fake_questions
):
    """
    Intent: A run that stored three of five questions must say so and say why, or a
        prompt or model problem looks like a model with little to say.
    Success: The rejected questions and their reasons are rendered.
    Feature: Question generation — discards are reported on screen.
    """
    api_module._run_state["last_result"] = {
        "inserted": 1,
        "requested": 2,
        "run_id": "abcdef1234",
        "rejected": [{"stem": "Broken?", "problem": "2 options, expected 4"}],
    }
    body = client.get(PAGE).text
    assert 'data-rejected="true"' in body
    assert "2 options, expected 4" in body


def test_a_failed_run_is_explained_with_its_trace(client, fake_collection, fake_questions):
    """
    Intent: A background failure has nowhere else to surface. Without the message and
        trace on the page, the author has to go and read server logs to learn that a
        credential is missing.
    Success: A danger alert carries the error message and the stack trace.
    Feature: Question generation — failures are explained on screen.
    """
    api_module._run_state.update(
        last_error="No Anthropic credentials found.",
        last_traceback="Traceback (most recent call last): RuntimeError",
    )
    body = client.get(PAGE).text
    assert 'class="alert alert-danger"' in body
    assert "No Anthropic credentials found." in body
    assert "<details" in body and "RuntimeError" in body


def test_the_page_names_where_questions_are_stored(client, fake_collection, fake_questions):
    """
    Intent: This tool writes to a shared Atlas cluster. Naming the target on screen is
        what stops an author generating into the wrong database and not noticing.
    Success: The footer names the configured database and collection.
    Feature: Question review screen — storage target is visible.
    """
    body = client.get(PAGE).text
    assert "skill-badge-questions.questions" in body


# --- review actions ---


def test_each_question_offers_the_review_decisions(client, fake_collection, fake_questions):
    """
    Intent: Approving, rejecting and deleting are the review actions the screen exists
        to provide, and each must be bound to the question it appears on — not to the
        first one on the page.
    Success: The row carries the question's id and the three action buttons.
    Feature: Question review screen — review actions.
    """
    question_id = seed_question()
    body = client.get(PAGE).text
    assert f'data-question-id="{question_id}"' in body
    assert 'class="btn btn-sm btn-outline-success js-status" data-status="approved"' in body
    assert 'class="btn btn-sm btn-outline-secondary js-status" data-status="rejected"' in body
    assert 'data-delete="true"' in body


def test_the_current_state_is_not_offered_as_an_action(client, fake_collection, fake_questions):
    """
    Intent: Offering "Approve" on an approved question invites a click that changes
        nothing and makes the current state harder to read.
    Success: An approved question offers Reject and Re-open but not Approve.
    Feature: Question review screen — actions reflect current state.
    """
    question_id = seed_question()
    questions.set_status(question_id, "approved")
    body = client.get(PAGE).text
    assert 'data-status="approved"' not in body
    assert 'data-status="rejected"' in body
    assert 'data-status="draft"' in body


def test_the_elapsed_timer_is_driven_by_the_servers_start_time(
    client, fake_collection, fake_questions
):
    """
    Intent: This is the reported bug: leaving the screen and coming back restarted the
        timer at 00:00 while the run continued, because the start time lived only in the
        page's memory. The page must take it from the run state instead, so any load of
        the screen shows the true elapsed time.
    Success: The page's polling adopts the server's start time and clock, and never seeds
        the timer from the browser's own clock.
    Feature: Question generation — elapsed time survives leaving the page.
    """
    body = client.get(PAGE).text
    assert "adoptServerClock(state)" in body
    assert "state.started_at" in body
    assert "state.server_time" in body
    # The old behaviour: starting the count from whenever the browser noticed.
    assert "startedAt = Date.now()" not in body


def test_a_run_already_in_progress_shows_its_true_elapsed_time(
    client, fake_collection, fake_questions
):
    """
    Intent: The case that exposed the bug — arriving at the screen while a run started
        earlier is still going. The page must ask the server for status before it draws
        the timer, rather than drawing a zero and correcting it later.
    Success: With a run in progress the page polls on load, and the timer's text comes
        from the elapsed-time helper rather than a literal zero.
    Feature: Question generation — elapsed time survives leaving the page.
    """
    api_module._run_state["running"] = True
    body = client.get(PAGE).text
    assert "if (true) { pollStatus(); }" in body
    assert "elapsedSinceStart" in body


def test_cross_badge_attribution_is_reported_after_a_run(client, fake_collection, fake_questions):
    """
    Intent: Filing a question under badges the author did not ask for is a judgement they
        may disagree with. Reporting how many were cross-filed, and why, is what makes it
        reviewable rather than something that quietly happens to their collection.
    Success: The run alert reports the cross-tagged count, the badges added and the reason.
    Feature: Question attribution — cross-filing is reported on screen.
    """
    api_module._run_state["last_result"] = {
        "inserted": 2,
        "requested": 2,
        "run_id": "abcdef1234",
        "rejected": [],
        "cross_tagged": 1,
        "attribution_reasons": [
            {"stem": "Which stage?", "added": ["indexing"], "reason": "tests index selection"}
        ],
    }
    body = client.get(PAGE).text
    assert 'data-cross-tagged="true"' in body
    assert "1 question(s) also belong to other skill badges" in body
    assert "tests index selection" in body


def test_a_skipped_cross_badge_review_is_reported(client, fake_collection, fake_questions):
    """
    Intent: When attribution fails the questions are still stored, but only under the
        badges they were written for. Silently returning a narrower result would leave the
        author believing the cross-badge review had found nothing.
    Success: The page says the review did not run, and names the reason.
    Feature: Question attribution — a skipped review is visible, not silent.
    """
    api_module._run_state["last_result"] = {
        "inserted": 1,
        "requested": 1,
        "run_id": "abcdef1234",
        "rejected": [],
        "cross_tagged": 0,
        "attribution_error": "overloaded_error",
    }
    body = client.get(PAGE).text
    assert 'data-attribution-error="true"' in body
    assert "overloaded_error" in body


# --- semantic search on the screen ---


def test_the_screen_offers_a_search_box(client, fake_collection, fake_questions):
    """
    Intent: An author's first instinct before generating is "what do we already have on
        this?". Without a search box that question can only be answered by scrolling, so
        duplicates get generated.
    Success: The page renders a search form posting the q parameter back to the root.
    Feature: Question search — reachable from the main screen.
    """
    body = client.get(PAGE).text
    assert 'data-search-form="true"' in body
    assert 'name="q"' in body


def test_a_search_ranks_the_list_by_similarity(client, fake_collection, fake_questions):
    """
    Intent: The value of the search is the ranking, and a ranking is only trustworthy if the
        reader can see how close each match is — otherwise the weakest match looks as
        authoritative as the best.
    Success: A search renders the matching question with its similarity score and says the
        list is ranked.
    Feature: Question search — ranked results with visible scores.
    """
    seed_question("Which stage filters documents?")
    body = client.get(PAGE, params={"q": "stage filters documents"}).text
    assert 'data-search-summary="true"' in body
    assert 'data-score="true"' in body
    assert "Which stage filters documents?" in body


def test_a_search_result_can_still_be_narrowed_by_the_filters(
    client, fake_collection, fake_questions
):
    """
    Intent: An author searching within one badge means "of these matches, show me that
        badge". If the filters were ignored during a search the screen would contradict
        itself, showing questions the filter excludes.
    Success: A filter that excludes the match leaves it out of the search result.
    Feature: Question search — filters narrow the matches.
    """
    seed_question("Which stage filters documents?")
    params = {"q": "stage filters documents", "status": "approved"}
    assert "Which stage filters documents?" not in client.get(PAGE, params=params).text


def test_a_search_with_no_match_says_so_without_claiming_the_collection_is_empty(
    client, fake_collection, fake_questions
):
    """
    Intent: "Nothing like that here" and "no questions exist" call for different actions,
        and the difference matters most right before someone generates more.
    Success: A search with no match names the query in the empty state.
    Feature: Question search — distinct empty state.
    """
    body = client.get(PAGE, params={"q": "quantum tunnelling in kubernetes"}).text
    assert 'data-empty="true"' in body
    assert "quantum tunnelling in kubernetes" in body


def test_an_index_that_is_not_queryable_yet_is_explained_on_the_screen(
    client, fake_collection, fake_questions, monkeypatch
):
    """
    Intent: A newly created Atlas index takes minutes to build, and searching it before
        then fails. Showing a stack trace, or an empty result that reads as "we have
        nothing on that", would both be misleading during exactly the window when someone
        is trying out the new search.
    Success: The page returns 200 and explains that the index is unavailable.
    Feature: Question search — an unbuilt index is explained, not hidden.
    """
    from pymongo.errors import OperationFailure

    def explode(*args, **kwargs):
        raise OperationFailure("index not found")

    monkeypatch.setattr(questions, "similar_by_embedding_text", explode)
    response = client.get(PAGE, params={"q": "anything"})
    assert response.status_code == 200
    assert 'data-search-error="true"' in response.text


def test_a_search_can_be_cleared(client, fake_collection, fake_questions):
    """
    Intent: A search replaces the whole list, so without an obvious way back an author is
        stuck in a filtered view and may think the rest of the collection has gone.
    Success: While searching, the page offers a link back to the unsearched list.
    Feature: Question search — reversible.
    """
    body = client.get(PAGE, params={"q": "anything"}).text
    assert 'data-clear-search="true"' in body


def test_the_screen_offers_the_duplicate_sweep(client, fake_collection, fake_questions):
    """
    Intent: The sweep is an on-demand action, so it needs a control. Without one it exists
        only as an API call and would never be run.
    Success: The page offers sweep and dry-run buttons.
    Feature: Question duplicate sweep — reachable from the main screen.
    """
    body = client.get(PAGE).text
    assert 'id="sweep-btn"' in body
    assert 'id="sweep-dry-run-btn"' in body


def test_sweep_results_are_reported_with_scores(client, fake_collection, fake_questions):
    """
    Intent: A sweep deletes questions with no judge behind it, so what it did and how sure
        it was must both be visible — otherwise a wrong threshold is invisible until an
        author notices something missing.
    Success: The page reports deleted pairs and remaining suspects with their rerank scores.
    Feature: Question duplicate sweep — outcomes are reported on screen.
    """
    api_module._run_state["last_result"] = {
        "source": "question-duplicate-sweep",
        "compared": 2,
        "dry_run": False,
        "deleted": [
            {
                "keep": "k1",
                "keep_stem": "The one kept?",
                "drop": "d1",
                "drop_stem": "The one deleted?",
                "vector_score": 0.91,
                "rerank_score": 0.98,
            }
        ],
        "possible_duplicates": [
            {
                "keep": "k2",
                "keep_stem": "A survivor?",
                "drop": "d2",
                "drop_stem": "A suspect?",
                "vector_score": 0.80,
                "rerank_score": 0.61,
            }
        ],
        "errors": [],
    }
    body = client.get(PAGE).text
    assert 'data-sweep-deleted="true"' in body
    assert "0.98" in body
    assert 'data-sweep-possible="true"' in body
    assert "0.61" in body


def test_a_dry_run_is_labelled_as_one(client, fake_collection, fake_questions):
    """
    Intent: A dry run and a real sweep report the same pairs. If the screen did not
        distinguish them, an author could believe duplicates were removed when nothing was.
    Success: A dry-run result is labelled, and says nothing was deleted.
    Feature: Question duplicate sweep — a dry run is visibly a dry run.
    """
    api_module._run_state["last_result"] = {
        "source": "question-duplicate-sweep",
        "compared": 1,
        "dry_run": True,
        "deleted": [],
        "possible_duplicates": [
            {
                "keep": "k",
                "keep_stem": "Kept?",
                "drop": "d",
                "drop_stem": "Would go?",
                "vector_score": 0.9,
                "rerank_score": 0.97,
                "would_delete": True,
            }
        ],
        "errors": [],
    }
    body = client.get(PAGE).text
    assert 'data-sweep-dry-run="true"' in body


def test_sweep_errors_are_surfaced(client, fake_collection, fake_questions):
    """
    Intent: A sweep that could not rerank part of the collection has not cleared it. Silence
        would be read as "no duplicates found", which is the one conclusion that must never
        be assumed.
    Success: Reported errors appear on the page.
    Feature: Question duplicate sweep — partial failures are visible.
    """
    api_module._run_state["last_result"] = {
        "source": "question-duplicate-sweep",
        "compared": 0,
        "dry_run": False,
        "deleted": [],
        "possible_duplicates": [],
        "errors": ["rerank failed for q1: VOYAGE_API_KEY not set"],
    }
    body = client.get(PAGE).text
    assert 'data-sweep-errors="true"' in body
    assert "VOYAGE_API_KEY" in body


# --- where a run's questions came from ---


def test_the_pages_a_run_wrote_from_are_listed(client, fake_collection, fake_questions):
    """
    Intent: A question is only worth as much as it is checkable. Naming the documentation
        pages a run read lets a reviewer open the source instead of re-researching the
        question to find out whether it is right.
    Success: The run alert reports how many pages were used and links each one.
    Feature: Question generation — the source material of a run is shown on screen.
    """
    api_module._run_state["last_result"] = {
        "inserted": 1,
        "requested": 1,
        "rejected": [],
        "source_pages": [{"url": "https://x/a.md", "title": "Atlas Search indexes"}],
        "researched_the_web": False,
    }
    body = client.get(PAGE).text
    assert 'data-source-pages="true"' in body
    assert "1 stored documentation page(s)" in body
    assert "https://x/a.md" in body


def test_a_run_that_fell_back_to_the_web_says_so(client, fake_collection, fake_questions):
    """
    Intent: A run that researched the web is slower and not repeatable, and the remedy —
        refresh the corpus — is only actionable if the author is told the fallback
        happened rather than left assuming the stored documentation was used.
    Success: The run alert says the web was researched and points at the corpus screen.
    Feature: Question generation — the fallback to web research is visible on screen.
    """
    api_module._run_state["last_result"] = {
        "inserted": 1,
        "requested": 1,
        "rejected": [],
        "source_pages": [],
        "researched_the_web": True,
    }
    body = client.get(PAGE).text
    assert 'data-researched-the-web="true"' in body
    assert "/admin/docs" in body
