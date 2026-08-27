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
    body = client.get(PAGE, params={"category": "nothing-uses-this"}).text
    assert 'data-empty="true"' in body
    assert "match these filters" in body


# --- filtering ---


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


def test_the_generate_form_takes_a_walk_size_and_instructions(
    client, fake_collection, fake_questions
):
    """
    Intent: Replaces a test requiring a question-count box and a pasted-source-material
        box. A run is now sized in pages of documentation walked and questions taken per
        page, and the source material is the corpus rather than something pasted in — so
        those are the controls that have to be reachable on the screen.
    Success: The page cap, questions-per-page and instruction inputs are all present.
    Feature: Question generation — author controls on the screen.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'id="max-pages"' in body
    assert 'id="per-page"' in body
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
    assert 'data-last-result="true"' in body
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
    shared = client.get("/static/app.js").text
    # The clock itself moved to /static/app.js when five copies of it were consolidated,
    # so the behaviour now spans the page and that file: the page adopts the server's
    # clock, and app.js is what reads started_at and server_time. Both are asserted, and
    # the requirement recorded above is unchanged.
    assert "adoptServerClock(state)" in body
    assert "state.started_at" in shared
    assert "state.server_time" in shared
    # The old behaviour: starting the count from whenever the browser noticed.
    assert "startedAt = Date.now()" not in body
    assert "startedAt = Date.now()" not in shared


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
    params = {"q": "stage filters documents", "category": "nothing-uses-this"}
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


# --- the page walk on screen ---


def test_a_walk_reports_the_pages_it_wrote_from_and_what_is_left(
    client, fake_collection, fake_questions
):
    """
    Intent: After a walk the author needs two things: which pages produced these questions,
        and whether running it again would find anything new. Without the second, deciding
        whether to run again is guesswork.
    Success: The alert reports pages walked, per-page question counts, and pages remaining.
    Feature: Question generation — a walk's result on screen.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk",
        "inserted": 6,
        "pages_done": 2,
        "pages_available": 41,
        "rejected": [],
        "source_pages": [
            {"url": "https://x/a.md", "title": "Replication", "questions": 3},
            {"url": "https://x/b.md", "title": "Failover", "questions": 3},
        ],
    }
    body = client.get(PAGE).text
    assert "6 question(s) stored from" in body
    assert 'data-pages-left="true"' in body
    # The unit this counts is a chunk — a heading and the passage under it — and calling
    # it a page overstated the work by about four times, a page being a few chunks. The
    # requirement recorded above is unchanged: what produced these questions, and whether
    # running again would find anything new. Only the word for the unit is corrected.
    assert "41 chunk(s) not yet written from" in body
    assert "https://x/a.md" in body


def test_a_badge_whose_material_is_used_up_says_so_on_screen(
    client, fake_collection, fake_questions
):
    """
    Intent: An exhausted badge and a badge that simply produced nothing look identical
        without this. The actionable fact is that another run will not help — the corpus
        needs widening — and an author who is not told will keep pressing the button.
    Success: The alert says every page has been written from already.
    Feature: Question generation — exhausted material is visible.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk",
        "inserted": 0,
        "pages_done": 0,
        "pages_available": 0,
        "rejected": [],
        "exhausted": True,
        "source_pages": [],
    }
    body = client.get(PAGE).text
    assert 'data-exhausted="true"' in body
    assert "already been written from" in body


def test_a_walk_that_researched_instead_says_so_on_screen(
    client, fake_collection, fake_questions
):
    """
    Intent: A run that researched the web is slower and not repeatable, and the fix —
        refresh the corpus — is only actionable if the author is told rather than left
        assuming the documentation was walked.
    Success: The alert reports the fallback and links to the corpus screen.
    Feature: Question generation — the research fallback is visible.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk",
        "inserted": 3,
        "pages_done": 0,
        "pages_available": 0,
        "rejected": [],
        "fell_back_to_research": True,
        "source_pages": [],
    }
    body = client.get(PAGE).text
    assert 'data-fell-back="true"' in body
    assert "/admin/docs" in body


def test_pages_that_produced_nothing_are_listed(client, fake_collection, fake_questions):
    """
    Intent: A walk steps over a page that refuses or truncates rather than failing. Left
        unreported those pages are invisible, and a badge whose material is systematically
        refused would look like a badge with thin documentation.
    Success: The alert reports how many pages produced nothing, with their reasons.
    Feature: Question generation — pages that produced nothing are reported.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk",
        "inserted": 3,
        "pages_done": 3,
        "pages_available": 10,
        "rejected": [],
        "source_pages": [],
        "failure_count": 1,
        "failures": [{"url": "https://x/bad.md", "error": "refused"}],
    }
    body = client.get(PAGE).text
    assert 'data-page-failures="true"' in body
    assert "https://x/bad.md" in body and "refused" in body


# --- the status panel ---


def test_the_screen_carries_a_run_progress_panel(client, fake_collection, fake_questions):
    """
    Intent: A walk of 25 pages runs for many minutes. A spinner cannot distinguish a slow
        run from a stuck one, so the screen needs the same kind of panel the documentation
        refresh has: phase, a bar, and the numbers behind it.
    Success: The panel and its progress bar are on the page, with cells for pages,
        questions, rate, elapsed and remaining.
    Feature: Question generation — a detailed run status panel.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'data-progress-panel="true"' in body
    assert 'data-progress-bar="true"' in body
    for stat in ("pages", "questions", "rate", "elapsed", "eta"):
        assert 'data-stat="' + stat + '"' in body


def test_the_status_panel_shows_what_the_run_is_spending(
    client, fake_collection, fake_questions
):
    """
    Intent: Cost is the reason to stop a walk, so it has to be visible while there is
        still something to stop. Spend and a projection together are what make that an
        informed decision rather than a guess.
    Success: The panel has cells for spend, the projected total and the token counts.
    Feature: Question generation — run cost on screen.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'data-stat="spent"' in body
    assert 'data-stat="projected"' in body
    assert 'data-stat="tokens"' in body


def test_the_status_panel_offers_a_stop_button(client, fake_collection, fake_questions):
    """
    Intent: A 200-page walk is a long commitment, and an author who started the wrong one
        should be able to stop the spending without restarting the server. The button says
        "after this page" because the page in flight is already paid for.
    Success: A stop control is present on the panel.
    Feature: Question generation — a run can be stopped from the screen.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'data-stop-run="true"' in body
    assert "Stop after this chunk" in body


def test_a_finished_run_reports_what_it_cost(client, fake_collection, fake_questions):
    """
    Intent: Cost per badge is how an author decides whether the next 33 badges are worth
        it. Shown only while running, the number is gone by the time there is a decision
        to make.
    Success: The run alert reports the dollars the walk spent.
    Feature: Question generation — a finished run reports its cost.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk",
        "inserted": 9,
        "pages_done": 3,
        "pages_available": 20,
        "rejected": [],
        "source_pages": [],
        "cost": {"dollars": 0.184},
    }
    body = client.get(PAGE).text
    assert 'data-run-cost="true"' in body
    assert "0.184" in body


def test_a_stopped_run_is_labelled_as_one(client, fake_collection, fake_questions):
    """
    Intent: A stopped walk looks like a walk that ran out of material — both end with
        fewer questions than asked for. Without a label the author cannot tell that the
        remaining pages are still there to walk.
    Success: A run that stopped early is marked as stopped on screen.
    Feature: Question generation — a stopped run is visible as stopped.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk",
        "inserted": 3,
        "pages_done": 1,
        "pages_available": 20,
        "rejected": [],
        "source_pages": [],
        "stopped_early": True,
    }
    body = client.get(PAGE).text
    assert 'data-stopped-early="true"' in body


# --- no review workflow, guarded deletion ---


def test_a_question_offers_only_deletion(client, fake_collection, fake_questions):
    """
    Intent: Replaces a test requiring approve, reject and re-open buttons. With no review
        state there is nothing to approve and nothing to re-open, and every extra control
        is one more thing to read on a screen holding thousands of questions. Deletion is
        the one editorial act left, and it must be bound to the question it appears on
        rather than the first on the page.
    Success: The row carries its question id and a delete control, and offers no status
        actions.
    Feature: Question review screen — deletion is the only action.
    """
    question_id = seed_question()
    body = client.get(PAGE).text
    assert f'data-question-id="{question_id}"' in body
    assert 'data-delete="true"' in body
    assert "js-status" not in body


def test_the_screen_no_longer_offers_status_tabs(client, fake_collection, fake_questions):
    """
    Intent: Replaces a test requiring Drafts, Approved and Rejected tabs with counts. Tabs
        over states that no longer exist would filter on a field nothing writes, so every
        tab but the first would read as empty — the screen would claim the collection was
        empty when it was not.
    Success: The list is headed by a single count of what is in view, with no status tabs.
    Feature: Question review screen — one list, no review states.
    """
    seed_question()
    body = client.get(PAGE).text
    assert 'data-question-count="true"' in body
    assert "1 question" in body
    assert "nav-tabs" not in body


def test_deleting_asks_twice_before_it_happens(client, fake_collection, fake_questions):
    """
    Intent: Deletion is the only irreversible act on this screen — nothing can re-create a
        question — and it now sits next to no other button, so a misplaced click has
        nowhere else to land. A single confirm is not enough for an action that is one
        click from a list of thousands.
    Success: The delete control opens a dialog that shows the question again and requires
        the word to be typed before the confirming button is enabled.
    Feature: Question deletion — guarded against an accidental click.
    """
    seed_question()
    body = client.get(PAGE).text
    assert 'id="delete-modal"' in body
    assert 'data-delete-stem="true"' in body
    assert 'data-delete-confirm="true"' in body
    assert 'data-confirm-delete="true"' in body
    assert "disabled" in body


def test_the_delete_dialog_shows_the_question_it_will_delete(
    client, fake_collection, fake_questions
):
    """
    Intent: A confirmation that does not show what is about to be lost is a confirmation
        nobody reads. The stem in the dialog is what makes the second click a decision
        rather than a reflex — and it must be the stem of the row that was clicked.
    Success: The delete control carries its own question's stem.
    Feature: Question deletion — the dialog names the question.
    """
    seed_question("Which stage filters documents?")
    body = client.get(PAGE).text
    assert 'data-stem="Which stage filters documents?"' in body


# --- run history and dismissing on screen ---


def test_the_run_summary_can_be_closed(client, fake_collection, fake_questions):
    """
    Intent: The summary is rendered from run state, so it stays on the screen until somebody
        starts another run — permanently, for anyone who is not currently generating. It has
        to be closable, and nothing is lost by closing it because the run is recorded.
    Success: The run summary carries a dismiss control.
    Feature: Question generation — a finished run's notice can be closed.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk", "inserted": 3, "pages_done": 1, "rejected": [],
        "source_pages": [],
    }
    body = client.get(PAGE).text
    assert 'data-last-result="true"' in body
    assert 'data-dismiss-result="true"' in body


# --- skill level, rate and unit cost on screen ---


def test_the_form_offers_a_skill_level(client, fake_collection, fake_questions):
    """
    Intent: A quiz for people who own the deployment is a different artefact from one for
        people who installed MongoDB last week, so the level is a choice the author has to be
        able to make — and mixed has to be offered, since spreading the levels is the sensible
        default for filling a bank.
    Success: The form offers the three levels and a mixed option.
    Feature: Question generation — skill level is choosable on the screen.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'id="difficulty"' in body
    for level in ("foundational", "intermediate", "advanced"):
        assert 'value="' + level + '"' in body
    assert "Mixed" in body


def test_the_panel_shows_throughput_and_unit_cost(client, fake_collection, fake_questions):
    """
    Intent: Spend so far does not say whether a run is going well — a large number is fine if
        it is producing a lot. Questions per minute and cost per question are the two figures
        that make "let it run or stop it" an informed decision.
    Success: The panel has cells for the rate and the cost per question.
    Feature: Question generation — throughput and unit cost on the panel.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'data-stat="qpm"' in body
    assert 'data-stat="per-question"' in body


def test_a_finished_run_reports_its_unit_cost_and_rate(client, fake_collection, fake_questions):
    """
    Intent: Total spend cannot be compared between runs because it depends on how many pages
        were walked. Cost per question and questions per minute are the comparable figures,
        and they are what a later decision about the other badges turns on.
    Success: The run summary reports both alongside the total.
    Feature: Question generation — a finished run reports its unit cost and rate.
    """
    api_module._run_state["last_result"] = {
        "source": "badge-page-walk",
        "inserted": 9,
        "pages_done": 3,
        "pages_available": 20,
        "rejected": [],
        "source_pages": [],
        "questions_per_minute": 2.5,
        "cost": {"dollars": 0.184, "dollars_per_question": 0.0204},
    }
    body = client.get(PAGE).text
    assert "0.0204" in body and "per question" in body
    assert "2.5" in body and "per minute" in body


# --- telling the tags apart ---


def test_badge_and_category_tags_are_coloured_differently(
    client, fake_collection, fake_questions
):
    """
    Intent: Three kinds of tag sit side by side on a question — difficulty, the badges it is
        filed under, and the topic areas it exercises. Rendered identically they read as one
        undifferentiated row of chips, and the reader has to know the order to tell which is
        which.
    Success: Badge tags and category tags carry different colour classes, and neither matches
        the other.
    Feature: Question review screen — tag kinds are visually distinct.
    """
    seed_question(skill_badges=["atlas-search"], categories=["search"])
    body = client.get(PAGE).text
    # The two colour treatments now come from the theme's tag classes rather than
    # Bootstrap's subtle-background utilities. The requirement recorded above is
    # unchanged — the two kinds of tag must carry different colour classes — only
    # where those classes are defined has moved.
    assert "tag-badge" in body
    assert "tag-category" in body


def test_a_tags_kind_is_also_stated_in_words(client, fake_collection, fake_questions):
    """
    Intent: Colour alone is not a label — it fails for anyone who cannot distinguish the two
        hues, and it fails in a screenshot pasted into a document. The distinction has to exist
        in text as well as in colour.
    Success: Each tag says what kind of tag it is.
    Feature: Question review screen — tag kinds are named, not only coloured.
    """
    seed_question(skill_badges=["atlas-search"], categories=["search"])
    body = client.get(PAGE).text
    assert "Skill badge: atlas-search" in body
    assert "Topic area: search" in body


def test_a_badge_tag_links_to_the_badge_definition(client, fake_collection, fake_questions):
    """
    Intent: A slug is not self-explanatory — "secure-mongodb-self-managed-authn-authz" does not
        say what it covers. Checking should be one click, not a hunt through a 34-row admin
        table for a row whose name differs from its slug.
    Success: A badge tag is a link to that badge's row on the badge screen.
    Feature: Question review screen — badge tags link to their definitions.
    """
    seed_question(skill_badges=["atlas-search"])
    body = client.get(PAGE).text
    assert '/admin/skill-badges#badge-atlas-search' in body


def test_category_tags_are_not_links(client, fake_collection, fake_questions):
    """
    Intent: A topic area is a free-text label with no definition anywhere, so linking one would
        promise a page that does not exist. Only the tags that lead somewhere should look like
        they do.
    Success: A category tag is not rendered as an anchor.
    Feature: Question review screen — only badge tags are links.
    """
    seed_question(skill_badges=["atlas-search"], categories=["search"])
    body = client.get(PAGE).text
    start = body.rindex("<", 0, body.index('data-category-tag="search"'))
    assert not body[start:].startswith("<a")


def test_a_citation_links_to_the_rendered_page(client, fake_collection, fake_questions):
    """
    Intent: A citation exists so a reviewer can check a question against its source. MongoDB
        serves these pages as raw Markdown, which a browser shows as unformatted text — so the
        link went somewhere technically correct and unpleasant to read.
    Success: The citation points at the renderer while still showing the canonical URL.
    Feature: Question review screen — citations open rendered.
    """
    seed_question(source_urls=["https://www.mongodb.com/docs/manual/replication.md"])
    body = client.get(PAGE).text
    assert "/admin/docs/render?url=" in body
    # The reader still sees the canonical URL: it is what identifies the page.
    assert "https://www.mongodb.com/docs/manual/replication.md" in body


# --- when a question was written ---


def test_a_question_shows_when_it_was_written(client, fake_collection, fake_questions):
    """
    Intent: The list is newest first, but without a time an author cannot tell which questions
        came out of the run they just watched — and once a prompt has been changed, cannot tell
        which side of that change a question is from. That is the comparison the whole run
        history exists to support, and it needs to be visible on the question itself.
    Success: The question card shows its creation time.
    Feature: Question review screen — questions show when they were written.
    """
    seed_question()
    body = client.get(PAGE).text
    assert 'data-created-at="true"' in body


def test_the_screen_watches_for_newly_written_questions(client, fake_collection, fake_questions):
    """
    Intent: A walk stores questions section by section over many minutes, so a list left alone
        goes stale while its reader watches it being filled — the questions exist in the
        database and not on the screen.
    Success: The page polls the count endpoint and reloads when it grows.
    Feature: Question review screen — new questions appear as they are stored.
    """
    body = client.get(PAGE).text
    assert "showNewQuestions" in body
    assert '/count' in body


def test_the_watch_counts_only_the_current_view(client, fake_collection, fake_questions):
    """
    Intent: The screen reloads when its count grows. Counting the whole collection would reload
        a badge-filtered list whenever any other badge gained a question — during a multi-badge
        run, constantly, and each time showing the reader the same list they already had.
    Success: The polled count carries the screen's badge filter.
    Feature: Question review screen — the watch matches the filtered view.
    """
    seed_badge()
    seed_question()
    body = client.get(PAGE, params={"skill_badge": "atlas-search"}).text
    assert 'skill_badge: "atlas-search"' in body


# --- identifiers on screen ---


def test_a_question_shows_its_identifier(client, fake_collection, fake_questions):
    """
    Intent: An author refers to one question in a message or a ticket, and the only way to do
        that is by identifier — so it has to be on the card. Shortened and click-to-copy,
        because 32 hex characters is not something anyone should retype.
    Success: The card carries a copyable identifier chip.
    Feature: Question review screen — questions show their identifier.
    """
    question_id = seed_question()
    body = client.get(PAGE).text
    assert 'data-copy-id="' + question_id + '"' in body
    assert question_id[:8] in body


def test_pasting_an_identifier_finds_that_question(client, fake_collection, fake_questions):
    """
    Intent: One box serves both purposes — paste an id to find one question, type a phrase to
        find several. A hex string has no meaning to embed, so semantically searching for one
        returns whatever happens to be nearest, which reads as "that question does not exist".
    Success: Searching for an identifier returns exactly that question, and says it was looked
        up rather than searched for.
    Feature: Question search — identifiers are looked up exactly.
    """
    question_id = seed_question("Which stage filters documents?")
    seed_question("Something else entirely?")
    body = client.get(PAGE, params={"q": question_id}).text
    assert "Which stage filters documents?" in body
    assert "Something else entirely?" not in body
    assert 'data-id-summary="true"' in body


def test_an_unknown_identifier_says_so_plainly(client, fake_collection, fake_questions):
    """
    Intent: An identifier that finds nothing means something specific — deleted, or from
        another collection — and that is more useful than an empty result reading as "we have
        nothing on that". It also tells the reader their paste was understood as an id.
    Success: An identifier-shaped query with no match explains itself.
    Feature: Question search — an unknown identifier is explained.
    """
    seed_question()
    body = client.get(PAGE, params={"q": "a" * 32}).text
    assert 'data-no-such-id="true"' in body
    assert "No question with that identifier" in body


def test_a_phrase_is_still_searched_semantically(client, fake_collection, fake_questions):
    """
    Intent: The identifier check must not swallow ordinary searches — a query is only treated
        as an id when its shape says so. Otherwise a search for a hex-looking phrase would
        silently return nothing.
    Success: A worded query is reported as a similarity search, not an identifier lookup.
    Feature: Question search — phrases are still searched by meaning.
    """
    seed_question("Which stage filters documents?")
    body = client.get(PAGE, params={"q": "stage filters documents"}).text
    assert 'data-search-summary="true"' in body
    assert 'data-id-summary="true"' not in body


# --- a question's own facts, above the question ---


def test_the_identifier_date_and_source_sit_above_the_stem(
    client, fake_collection, fake_questions
):
    """
    Intent: Which question this is, when it was written, and what it was written from are
        how an author refers to, dates and checks a question — they are read before the
        question, not after it. Below the options they were also out of sight on any
        question long enough to need scrolling.
    Success: All three render above the stem, in that order.
    Feature: Question review screen — a question's facts are stated before the question.
    """
    seed_question(source_urls=["https://www.mongodb.com/docs/manual/aggregation.md"])
    body = client.get(PAGE).text
    head = body.index("question-head")
    assert head < body.index('data-stem="true"')
    assert head < body.index("data-copy-id=") < body.index('data-created-at="true"') \
        < body.index("data-source-url=")


def test_each_of_those_facts_is_labelled(client, fake_collection, fake_questions):
    """
    Intent: A hex string, a date and a URL say nothing about what they are on their own,
        and the identifier in particular was previously an unlabelled grey chip in a row
        of tags — indistinguishable from a category to anyone who had not been told.
    Success: Each of the three carries a label naming what it is.
    Feature: Question review screen — the facts above a question are named.
    """
    seed_question(source_urls=["https://www.mongodb.com/docs/manual/aggregation.md"])
    body = client.get(PAGE).text
    head = body[body.index("question-head"):body.index('data-stem="true"')]
    assert "<dt>Question ID</dt>" in head
    assert "<dt>Generated</dt>" in head
    assert "<dt>Source</dt>" in head


def test_a_question_with_no_source_omits_the_source_row(
    client, fake_collection, fake_questions
):
    """
    Intent: Questions written before citations were recorded, and any written by research
        rather than from a stored page, have no source. An empty labelled row would state
        a fact the program does not have.
    Success: The source label is not rendered for a question with no source.
    Feature: Question review screen — absent facts are omitted, not shown empty.
    """
    seed_question(source_urls=[])
    body = client.get(PAGE).text
    head = body[body.index("question-head"):body.index('data-stem="true"')]
    assert "<dt>Source</dt>" not in head


def test_the_identifier_is_not_rendered_as_a_chip(client, fake_collection, fake_questions):
    """
    Intent: Chips on this screen mean "this question belongs to that" — a skill badge or a
        topic area. The identifier is the question's own name, so a pill put it in the wrong
        category of thing, and it now has a label saying what it is instead.
    Success: The identifier renders as plain text, carrying no tag class.
    Feature: Question review screen — the identifier reads as text, not as a tag.
    """
    seed_question()
    body = client.get(PAGE).text
    element = body[body.rindex("<button", 0, body.index("data-copy-id=")):]
    assert "tag" not in element[: element.index(">")]


# --- paging a bank of thousands ---


def test_only_one_page_of_questions_is_rendered(client, fake_collection, fake_questions):
    """
    Intent: The bank is meant to hold thousands of questions. Rendering all of them builds a
        document the browser is slow to lay out and slow to scroll, from a cursor that read
        every match to produce it — the screen gets worse exactly as the collection gets more
        valuable.
    Success: A page shows at most the requested number of questions, not the whole
        collection.
    Feature: Question review screen — the list is paged.
    """
    for index in range(12):
        seed_question(stem=f"Question {index}?")
    body = client.get(PAGE, params={"per_page": 5}).text
    assert body.count('data-question-id="') == 5


def test_fifty_questions_is_the_default_page(client, fake_collection, fake_questions):
    """
    Intent: A default that has to be chosen before the screen is useful is a decision pushed
        onto the reader. Fifty fills a screen or two — enough to scan a run's output without
        the page becoming a burden.
    Success: With no size asked for, the page offers fifty.
    Feature: Question review screen — a default page size.
    """
    seed_question()
    body = client.get(PAGE).text
    assert '<option value="50" selected>' in body


def test_a_page_size_that_is_not_offered_is_refused(client, fake_collection, fake_questions):
    """
    Intent: The size reaches the database as a limit. A hand-edited URL asking for a hundred
        thousand would render the very page this exists to prevent, and nothing in the
        request is worth trusting for that.
    Success: An unoffered size falls back to the default rather than being honoured.
    Feature: Question review screen — the page size is validated, not trusted.
    """
    seed_question()
    body = client.get(PAGE, params={"per_page": 100000}).text
    assert '<option value="50" selected>' in body


def test_a_later_page_shows_the_questions_after_the_first(
    client, fake_collection, fake_questions
):
    """
    Intent: Paging is only useful if the pages differ. Newest first is the order, so page two
        must continue where page one stopped rather than starting over.
    Success: The second page shows the next questions, and none of the first page's.
    Feature: Question review screen — pages continue rather than repeat.
    """
    for index in range(12):
        seed_question(stem=f"Question {index}?")
    first = client.get(PAGE, params={"per_page": 5}).text
    second = client.get(PAGE, params={"per_page": 5, "page": 2}).text
    first_ids = set(re.findall(r'data-question-id="([^"]+)"', first))
    second_ids = set(re.findall(r'data-question-id="([^"]+)"', second))
    assert len(first_ids) == 5 and len(second_ids) == 5
    assert not (first_ids & second_ids)


def test_a_page_past_the_end_shows_the_last_page(client, fake_collection, fake_questions):
    """
    Intent: Deleting the last question on the last page, or narrowing a filter, leaves a URL
        pointing past the end of the result. An error there is a dead end where showing the
        last page is plainly what was meant.
    Success: A page number beyond the end renders the last page rather than failing.
    Feature: Question review screen — an out-of-range page is clamped.
    """
    for index in range(12):
        seed_question(stem=f"Question {index}?")
    response = client.get(PAGE, params={"per_page": 5, "page": 99})
    assert response.status_code == 200
    assert 'data-page-position="true"' in response.text
    assert "Page 3 of 3" in re.sub(r"\s+", " ", response.text)


def test_the_pager_keeps_the_filters_and_the_size(client, fake_collection, fake_questions):
    """
    Intent: A pager that dropped the filters would move through a different collection from
        the one being read — page two of everything, having asked for one badge. The current
        view is the URL, so the URL has to carry all of it.
    Success: The next-page link preserves the badge filter, the search and the page size.
    Feature: Question review screen — paging preserves the view.
    """
    for index in range(6):
        seed_question(stem=f"Question {index}?", skill_badges=["atlas-search"])
    body = client.get(PAGE, params={"skill_badge": "atlas-search", "per_page": 5}).text
    link = body[body.index('data-page-next="true"'):]
    link = body[body.rindex("<a", 0, body.index('data-page-next="true"')):][: link.index(">") + 200]
    assert "skill_badge=atlas-search" in link
    assert "per_page=5" in link
    assert "page=2" in link


def test_the_screen_says_which_questions_are_on_it(client, fake_collection, fake_questions):
    """
    Intent: A page of fifty out of three thousand looks like the whole collection unless it
        says otherwise — the difference between "that is all there is" and "that is all that
        fits" is the whole point of a count.
    Success: The screen shows the range on this page alongside the total.
    Feature: Question review screen — the count distinguishes the page from the whole.
    """
    for index in range(12):
        seed_question(stem=f"Question {index}?")
    body = re.sub(r"\s+", " ", client.get(PAGE, params={"per_page": 5, "page": 2}).text)
    assert "12 questions" in body
    assert "Showing 6–10" in body


def test_a_search_result_is_paged_too(client, fake_collection, fake_questions, monkeypatch):
    """
    Intent: A result that behaved differently from the list would be a second set of rules to
        learn — and a search that matches everything is as long as the list is.
    Success: A search shows one page of its matches.
    Feature: Question review screen — search results are paged.
    """
    ids = [seed_question(stem=f"Question {index}?") for index in range(12)]
    monkeypatch.setattr(
        questions,
        "similar_by_embedding_text",
        lambda *a, **k: [{"question_id": qid, "stem": "x", "options": [],
                          "skill_badges": [], "categories": [],
                          "difficulty": "intermediate"} for qid in ids],
    )
    body = client.get(PAGE, params={"q": "aggregation stages", "per_page": 5}).text
    assert body.count('data-question-id="') == 5


# --- what a run would cost before starting it ---


def test_the_form_says_the_budget_is_per_badge(client, fake_collection, fake_questions):
    """
    Intent: The page budget and the questions-per-page are passed to each selected badge in
        full, so a run over several badges walks that many times as much. The hint said "25
        pages at 3 questions each is up to 75 questions", which is true of one badge and wrong
        by a factor of however many were selected — understating the one number on the form
        that decides what a run costs.
    Success: The form states that the numbers apply per badge.
    Feature: Question generation — the form says what the budget applies to.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert "Per badge, not per run" in body


def test_the_form_projects_the_questions_a_run_would_write(
    client, fake_collection, fake_questions
):
    """
    Intent: The surprise was a multiplication the reader had to do themselves, from a hint that
        did it wrong. Selecting every badge with the default budget is thousands of questions
        and hours of work, and nothing on the form said so before the run started.
    Success: The form carries a projection that multiplies badges by pages by questions per
        page, and updates as those change.
    Feature: Question generation — the run's size is projected before it starts.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert 'data-projection="true"' in body
    script = body[body.index("function updateProjection"):]
    script = script[: script.index("\n}")]
    assert "badges * pages" in script
    assert "pagesTotal * perPage" in script


def test_the_projection_is_named_as_a_ceiling(client, fake_collection, fake_questions):
    """
    Intent: A projected number read as a promise would be wrong in the other direction: pages
        already written from are skipped, so a badge with little unused material contributes
        far fewer, and stopping ends the run. An author deciding whether to wait needs to know
        which way the number can move.
    Success: The projection says it is a ceiling and why the real figure can be lower.
    Feature: Question generation — the projection states its own uncertainty.
    """
    seed_badge()
    body = client.get(PAGE).text
    assert "A ceiling, not an estimate" in body
    assert "already " in body and "skipped" in body


def test_the_panel_shows_the_whole_job_only_when_there_is_one(
    client, fake_collection, fake_questions
):
    """
    Intent: With a single badge selected, the job and the badge are the same thing, and two
        identical bars reporting the same numbers would be worse than one — the reader would
        look for the difference between them.
    Success: The overall block is rendered but hidden, and shown only when more than one badge
        is being walked.
    Feature: Question generation — the whole-job panel appears only for a multi-badge run.
    """
    body = client.get(PAGE).text
    assert 'data-overall="true"' in body
    assert 'class="d-none" data-overall="true"' in body
    script = body[body.index("const all = p.overall"):]
    assert "(all.badge_count || 0) > 1" in script[: script.index("\n\n")]


def test_the_clock_is_reported_against_the_job_not_the_badge(
    client, fake_collection, fake_questions
):
    """
    Intent: The elapsed figure runs from the moment the run started and never restarts at a
        badge boundary, so it measures the job. Reported only in the badge block, it read as
        that badge's walk and overstated it by every badge already finished.
    Success: The whole-job stats carry an elapsed cell, the browser tick writes to it, and
        the badge cell is fed the server's per-badge figure once more than one badge is in
        flight.
    Feature: Question generation — the job's elapsed time sits with the job's figures.
    """
    body = client.get(PAGE).text
    overall = body[body.index('data-overall-stats="true"'):body.index('data-progress-stats="true"')]
    assert 'data-stat="all-elapsed"' in overall
    assert 'data-stat="elapsed"' not in overall
    script = body[body.index("function startPanelClock"):]
    assert 'setStat("all-elapsed", text)' in script[: script.index("\n}")]
    assert 'if (many || !clock.ticking())' in script


def test_the_job_and_badge_figures_stand_in_the_same_order(
    client, fake_collection, fake_questions
):
    """
    Intent: The two grids reported the same quantities in different positions, so a figure
        and its per-badge counterpart never lined up and the reader had to find each one
        again. Both are four-column grids, so equal ordering means equal columns.
    Success: Questions, chunks, spend, per-question, projected spend, questions/min, elapsed
        and remaining appear in the same order in the job grid as in the badge grid.
    Feature: Question generation — job and badge figures share one layout.
    """
    body = client.get(PAGE).text
    overall = body[body.index('data-overall-stats="true"'):body.index('data-progress-stats="true"')]
    badge = body[body.index('data-progress-stats="true"'):body.index('data-stat="tokens"')]
    shared = ("questions", "pages", "spent", "per-question", "projected", "qpm", "elapsed", "eta")
    positions = [overall.index('data-stat="all-' + name + '"') for name in shared]
    assert positions == sorted(positions)
    positions = [badge.index('data-stat="' + name + '"') for name in shared]
    assert positions == sorted(positions)


def test_the_two_grids_report_the_same_figures_under_the_same_labels(
    client, fake_collection, fake_questions
):
    """
    Intent: Reporting "Projected spend" for the job and "Projected" for the badge, a rate for
        the badge and none for the job, and a question projection for the job and none for the
        badge, made two panels that look alike say different things. A reader comparing a
        badge against the job it is part of has to be comparing like with like.
    Success: Every label in the whole-job grid appears in the badge grid, in the same order,
        including a rate for the job and a question projection for the badge.
    Feature: Question generation — job and badge report the same figures under the same names.
    """
    body = client.get(PAGE).text
    overall = body[body.index('data-overall-stats="true"'):body.index('data-progress-stats="true"')]
    badge = body[body.index('data-progress-stats="true"'):body.index('data-stat="tokens"')]
    labels = ("Questions created", "Chunks evaluated", "Spend", "Cost/question",
              "Questions projected", "Chunks/min", "Spend projected", "Questions/min",
              "Time elapsed", "Time remaining")
    for grid in (overall, badge):
        positions = [grid.index(">" + label + "</div>") for label in labels]
        assert positions == sorted(positions)
    assert 'data-stat="all-rate"' in overall
    assert 'data-stat="projected-questions"' in badge


def test_a_rate_label_names_the_unit_it_counts(client, fake_collection, fake_questions):
    """
    Intent: "Rate" sat beside Questions/min and Per chunk — three figures that are all
        rates — and named neither the thing it counted nor the interval, so the only way to
        learn it meant chunks per minute was to read the JavaScript.
    Success: The chunk rate is labelled Chunks/min in both grids, no cell is labelled just
        "Rate", and the value is a bare number because the label carries the unit.
    Feature: Question generation — rate labels name their unit.
    """
    body = client.get(PAGE).text
    panel = body[body.index('data-progress-panel="true"'):body.index('data-stat="tokens"')]
    assert panel.count(">Chunks/min</div>") == 2
    assert ">Rate</div>" not in panel
    script = body[body.index("setStat(\"all-rate\""):]
    assert '"/min"' not in script[: script.index("\n")]


def test_a_time_label_says_it_is_a_time(client, fake_collection, fake_questions):
    """
    Intent: "Elapsed" and "Remaining" sat in a grid of counts and amounts, where "Remaining"
        could as easily have meant chunks left as time left — and it is the only cell whose
        unit was ambiguous rather than merely unstated.
    Success: Both cells are labelled with the word time, in both grids.
    Feature: Question generation — time cells say they are times.
    """
    body = client.get(PAGE).text
    panel = body[body.index('data-progress-panel="true"'):body.index('data-stat="tokens"')]
    assert panel.count(">Time elapsed</div>") == 2
    assert panel.count(">Time remaining</div>") == 2
    assert ">Elapsed</div>" not in panel and ">Remaining</div>" not in panel


def test_every_ratio_label_names_both_of_its_terms(client, fake_collection, fake_questions):
    """
    Intent: "Per chunk" and "Per question" named the denominator and left the numerator to be
        guessed — one counts questions, the other counts dollars, and neither said which. The
        panel's other ratios already read numerator/denominator.
    Success: The yield is labelled Questions/chunk and the unit cost Cost/question, and
        neither reads as a bare "Per …".
    Feature: Question generation — ratio labels name what they divide.
    """
    body = client.get(PAGE).text
    panel = body[body.index('data-progress-panel="true"'):body.index('data-stat="tokens"')]
    assert ">Questions/chunk</div>" in panel
    assert panel.count(">Cost/question</div>") == 2
    assert ">Per chunk</div>" not in panel and ">Per question</div>" not in panel


def test_the_chunk_being_read_is_named_above_the_phase(client, fake_collection, fake_questions):
    """
    Intent: The phase line says what the walk is doing; the chunk name says what it is doing
        it to, which is what tells an author the budget is going on the right material. Below
        ten figures it was the last thing read rather than the first.
    Success: The current-chunk line comes before the phase line in the panel.
    Feature: Question generation — the chunk in flight is named first.
    """
    body = client.get(PAGE).text
    panel = body[body.index('data-progress-panel="true"'):body.index('data-stat="tokens"')]
    assert panel.index('data-stat="current-page"') < panel.index('data-phase="true"')


def test_a_label_leads_with_what_it_measures(client, fake_collection, fake_questions):
    """
    Intent: A grid is scanned down its first words, so the subject has to come first and the
        qualifier after it: "Questions created" and "Questions projected" sort together under
        the thing they both count, where "Projected questions" put the qualifier in the
        scanning position and separated a figure from its own projection.
    Success: The panel's labels lead with the noun — questions, chunks, spend — and no label
        leads with a qualifier or states a bare noun where a qualifier is needed.
    Feature: Question generation — labels lead with what they measure.
    """
    body = client.get(PAGE).text
    panel = body[body.index('data-progress-panel="true"'):body.index('data-stat="tokens"')]
    for label in ("Questions created", "Questions projected", "Chunks evaluated",
                  "Spend", "Spend projected"):
        assert ">" + label + "</div>" in panel
    for label in ("Questions", "Chunks", "Spent", "Projected questions", "Projected spend"):
        assert ">" + label + "</div>" not in panel


def test_the_headline_counts_chunks(client, fake_collection, fake_questions):
    """
    Intent: The headline said "12 / 25 sections" while every label, form field and log line
        around it said chunk. One unit, one word — and "section" overstated the material,
        because a chunk is a heading and its passage, not an article.
    Success: The headline counts chunks.
    Feature: Question generation — the panel says chunk.
    """
    body = client.get(PAGE).text
    assert 'p.pages_total + " chunks"' in body
    assert '" sections"' not in body


def test_the_page_size_picker_is_wider_than_its_widest_option(client):
    """
    Intent: Shrunk to its content, the picker was exactly as wide as "500" and no wider, so the
        chosen figure sat against the arrow and the open menu was the same width as the closed
        control. Half again is the room that makes it read as a control rather than a number
        with a chevron beside it — the same treatment the line count and skill level pickers
        were given.
    Success: The picker carries a class that states a width.
    Feature: Questions screen — the page size picker is given room.
    """
    body = client.get(PAGE).text
    assert 'class="form-select page-size-select"' in body
    css = client.get("/static/theme.css").text
    rule = css[css.index(".page-size-select {"):]
    assert "width: 7.5rem" in rule[: rule.index("}")]


def test_a_badge_can_be_taken_off_a_question_from_its_tag(client, fake_collection, fake_questions):
    """
    Intent: A mis-filed question is noticed here, reading it, next to the badges it is filed
        under — so this is where it has to be correctable. The alternative was deleting the
        question and paying to generate a replacement, which loses a good question over a
        wrong label.
    Success: Each badge tag on a question filed under more than one carries a control that
        names the badge it would remove.
    Feature: Question review screen — a skill badge can be removed from its tag.
    """
    seed_question(skill_badges=["atlas-search", "vector-search"])
    body = client.get(PAGE).text
    assert 'data-remove-badge="atlas-search"' in body
    assert 'data-remove-badge="vector-search"' in body
    assert body.count('data-badge-tag-group="') == 2


def test_the_only_badge_on_a_question_offers_no_way_to_remove_it(client, fake_collection, fake_questions):
    """
    Intent: A question filed under nothing is unreachable from every screen that lists by
        badge and absent from export. Offering a control that would always refuse teaches the
        reader to click it and read an error, which is a worse way of saying "not allowed"
        than not offering it.
    Success: A question filed under one badge renders its tag without a remove control.
    Feature: Question review screen — the last badge cannot be removed.
    """
    seed_question(skill_badges=["atlas-search"])
    body = client.get(PAGE).text
    assert 'data-badge-tag="atlas-search"' in body
    # The attribute with a value is the control; the bare name also appears in the script
    # that binds every one of them, which is not one.
    assert 'data-remove-badge="' not in body
    assert 'class="tag tag-badge removable-tag"' not in body


def test_removing_a_badge_does_not_reload_the_list(client, fake_collection, fake_questions):
    """
    Intent: Deleting a question reloads, because the row it was on is gone. Unfiling one is an
        edit to a single tag — reloading a long list for it would throw away the reader's
        place, which is exactly where they were working.
    Success: The removal takes the tag out of the page in the browser, and drops the last
        remaining question's remove control rather than reloading.
    Feature: Question review screen — removing a badge is an edit in place.
    """
    seed_question(skill_badges=["atlas-search", "vector-search"])
    body = client.get(PAGE).text
    script = body[body.index('document.querySelectorAll("[data-remove-badge]")'):]
    script = script[: script.index("// A run may already be going")]
    assert '"/skill-badges/" + encodeURIComponent(slug)' in script
    assert 'button.closest("[data-badge-tag-group]").remove()' in script
    assert "window.location.reload" not in script
