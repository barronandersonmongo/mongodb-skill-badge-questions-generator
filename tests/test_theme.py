"""Tests for the shared look — app/static/theme.css and templates/_ui.html.

Before these existed, each template laid out its own header and its own row of
buttons, and the screens drifted apart: two different heading sizes, control
sizes mixed inside one row, and five badge colours on a single line. These tests
hold the screens to one shared vocabulary.

Assertions are on markup, never on a bare word: the templates ship JavaScript
containing the same labels they render.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"

# Every screen a user can navigate to, and the route that renders it. Sub-pages
# that need seeded data are covered in their own modules; these are the four the
# nav offers, which is where drift between screens is visible.
SCREENS = ("/", "/duplicates", "/admin/skill-badges", "/admin/docs", "/admin/logs")


def extract_toolbar(body: str) -> str:
    """The markup of the header toolbar alone.

    Slicing to the next </div> would stop at the first nested one — a segmented
    button group is a div — and slicing to the end of the page would drag in
    unrelated controls, so the div nesting is counted.
    """
    start = body.index('<div class="toolbar">')
    depth = 0
    for match in re.finditer(r"<div\b|</div>", body[start:]):
        depth += 1 if match.group().startswith("<div") else -1
        if depth == 0:
            return body[start : start + match.end()]
    raise AssertionError("the toolbar div is not closed")


@pytest.fixture(autouse=True)
def isolated_screens(monkeypatch, tmp_path, fake_collection, fake_questions, fake_doc_pages):
    """Every screen renders against in-memory storage and a temporary log file.

    All four read from MongoDB to render, so without this each request would sit
    waiting on a connection that cannot be made.
    """
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_the_theme_is_served_as_one_stylesheet(client):
    """
    Intent: The look has to be defined in one place. Inline styles per template are how
        the screens drifted apart in the first place, and they cannot be cached or
        reviewed as a whole.
    Success: /static/theme.css is served, as CSS, and defines the brand colours.
    Feature: Shared look — one stylesheet.
    """
    response = client.get("/static/theme.css")
    assert response.status_code == 200
    assert "css" in response.headers["content-type"]
    # The token was renamed when the theme stopped being a repaint of Bootstrap and
    # became its own layer; the recorded requirement — that the brand colours are
    # defined here rather than per template — is unchanged.
    assert "--forest: #00684a" in response.text


def test_the_theme_loads_after_bootstrap(client):
    """
    Intent: The theme works by overriding Bootstrap's own CSS variables. Loaded first it
        would be overridden instead, and every screen would silently revert to
        Bootstrap's defaults with no error to notice.
    Success: The theme's stylesheet link appears after Bootstrap's in the document.
    Feature: Shared look — override order.
    """
    body = client.get("/").text
    assert body.index("bootstrap") < body.index("/static/theme.css")


@pytest.mark.parametrize("screen", SCREENS)
def test_every_screen_leads_with_the_shared_page_header(client, screen):
    """
    Intent: Screens that each write their own header drift: this program had an h3 on the
        four main screens and an h4 on the three you reach by drilling into them, so the
        type scale changed as you went deeper for no reason a reader could act on.
    Success: Each screen renders exactly one page title, from the shared macro.
    Feature: Shared look — one page header per screen.
    """
    body = client.get(screen).text
    assert body.count('class="page-title"') == 1


@pytest.mark.parametrize("screen", SCREENS)
def test_no_screen_sizes_its_own_headings(client, screen):
    """
    Intent: A template that reaches for Bootstrap's heading utilities directly is
        re-deciding the type scale locally, which is exactly how the scale came to differ
        between screens. The scale belongs to the theme.
    Success: No screen renders a top-level heading carrying a Bootstrap size utility.
    Feature: Shared look — the type scale is not set per screen.
    """
    body = client.get(screen).text
    assert not re.search(r'<h1[^>]*class="[^"]*\bh[1-6]\b', body)


@pytest.mark.parametrize("screen", SCREENS)
def test_a_screens_actions_sit_in_one_toolbar(client, screen):
    """
    Intent: Each screen's actions were a hand-rolled div of buttons, which let sizes mix
        inside a single row — a btn-sm beside a btn — and left nothing separating a
        destructive action from a safe one.
    Success: Each screen renders a toolbar, and none of its buttons sets its own size.
    Feature: Shared look — one toolbar per screen, at one size.
    """
    toolbar = extract_toolbar(client.get(screen).text)
    assert "<button" in toolbar or "<a " in toolbar
    assert "btn-sm" not in toolbar


def test_the_admin_area_is_labelled_once_rather_than_per_link(client):
    """
    Intent: Three of the four nav links each carried their own "admin" pill. Crossing
        into curation work is one boundary, so saying it three times is repetition rather
        than information — but it must still be said, because nothing else marks the
        boundary and there are no authorizations to enforce it.
    Success: The nav carries exactly one visible admin group label, and each admin link
        still declares itself as admin in the markup.
    Feature: Navigation — the admin area is marked once, as a group.
    """
    body = client.get("/").text
    nav = body[body.index("<nav"):body.index("</nav>")]
    section = nav[nav.index('class="nav-section"'):]
    assert nav.count("nav-group-label") == 1
    assert section.count("nav-link") == section.count('data-admin-area="true"')
    assert 'data-admin-area="true"' not in nav[: nav.index('class="nav-section"')]


@pytest.mark.parametrize("screen", SCREENS)
def test_every_screen_renders_inside_the_app_shell(client, screen):
    """
    Intent: Navigation is furniture, not content. A nav that scrolls away with the work,
        or that each screen redraws, is what made this read as a sequence of pages rather
        than one application.
    Success: Every screen renders the shell with its persistent sidebar.
    Feature: Shell — one persistent frame around every screen.
    """
    body = client.get(screen).text
    assert '<div class="app">' in body
    assert body.count("app-sidebar") == 1


def test_authoring_and_curation_are_not_peers_in_the_navigation(client):
    """
    Intent: All four screens sat in one flat list, which said they were the same kind of
        work. They are not: writing questions is the job, and the other three are curation
        you visit when something about the material is wrong. A flat list makes an author
        weigh three irrelevant destinations every time they look at the nav.
    Success: The questions link sits outside the admin section, and all three curation
        links sit inside it, under the admin group heading.
    Feature: Navigation — authoring is separated from curation.
    """
    body = client.get("/").text
    nav = body[body.index("<nav"):body.index("</nav>")]
    section = nav[nav.index('class="nav-section"'):]
    assert 'href="/"' not in section
    assert section.count('data-admin-area="true"') == section.count("nav-link")
    assert nav.count("nav-group-label") == 1


def test_the_admin_boundary_is_drawn_in_the_layout(client):
    """
    Intent: Nothing enforces the boundary — there are no authorizations, and both areas
        are reachable by anyone who can reach the service — so the layout is the only
        thing that can tell an author which side of it they are on.
    Success: The theme separates the admin section visually rather than relying on a
        repeated label on each link.
    Feature: Navigation — the boundary is visible.
    """
    css = client.get("/static/theme.css").text
    rule = css[css.index(".nav-section {"):]
    rule = rule[: rule.index("}")]
    assert "border-top" in rule


def test_a_tag_colour_means_exactly_one_thing(client):
    """
    Intent: A question's metadata line could show five differently coloured chips at
        once, so colour carried no meaning a reader could learn. The theme fixes one
        meaning per colour: gold for a skill badge, green for a topic area, flat grey for
        anything that is only an identifier.
    Success: The theme defines those three tag colours and no other tag colour.
    Feature: Shared look — tag colours are a closed set.
    """
    css = client.get("/static/theme.css").text
    # The third kind, a flat grey chip for an identifier, no longer exists: an
    # identifier is not something a question belongs to, so it is rendered as plain
    # labelled text rather than as a chip at all. The recorded requirement — that the
    # tag colours are a closed set with one meaning each — is what is asserted, and
    # the set is now the two kinds that remain.
    assert set(re.findall(r"\.tag-([a-z]+)\b", css)) == {"badge", "category"}


def test_no_template_hand_rolls_a_page_header(client):
    """
    Intent: A macro only enforces consistency while every screen uses it. A new screen
        that copies the old pattern instead would drift, and the drift would not show up
        as a failure anywhere else.
    Success: No template renders the header layout the macros replaced.
    Feature: Shared look — the macros cannot be bypassed.
    """
    offenders = [
        path.name
        for path in TEMPLATES.rglob("*.html")
        if "justify-content-between align-items-start mb-3" in path.read_text()
    ]
    assert offenders == []


def test_a_questions_facts_are_set_apart_from_the_question(client):
    """
    Intent: A question's identifier, date and source are about the question rather than part
        of it. Run together with the stem in the same white space they read as the first
        lines of the question itself.
    Success: The theme gives the facts block its own grey ground.
    Feature: Shared look — a question's facts are visually a separate block.
    """
    css = client.get("/static/theme.css").text
    rule = css[css.index(".question-head {"):]
    rule = rule[: rule.index("}")]
    assert "background: var(--wash)" in rule


def test_the_correct_option_is_marked_by_more_than_weight(client):
    """
    Intent: A reviewer scans a long list looking for one thing per question — which option is
        the right one. Bold alone means reading each option to find it, and bold is also what
        the stem uses, so the two compete.
    Success: The correct option carries a background of its own, in the accent colour the
        theme already uses for success, and a distinguishing marker so the signal is not
        colour alone.
    Feature: Shared look — the correct option stands out at a glance.
    """
    css = client.get("/static/theme.css").text
    rule = css[css.index("li[data-correct] {"):]
    assert "var(--forest-wash)" in rule[: rule.index("}")]
    assert "li[data-correct]::marker" in css


def test_one_word_is_used_for_the_unit_a_run_reads(client):
    """
    Intent: The unit a walk consumes was called a page on the generate form and the coverage
        screen, a section in the progress panel and the run history, and an article on the
        material screen — four words for one thing, which is a piece of a page rather than a
        page. The form's number is what decides what a run costs, and calling it pages
        overstated the material by about four times, a page being a few chunks on average.
    Success: No screen labels that unit a section or an article; the pages that remain named
        are the pages a chunk came from and the pages of a list.
    Feature: Shared vocabulary — one word for a chunk.
    """
    for screen in ("/", "/coverage", "/runs", "/admin/material"):
        body = client.get(screen).text
        content = body[body.index("<main"):]
        assert "Sections" not in content, screen
        assert "Articles" not in content, screen


def test_the_shared_helpers_live_in_one_file(client):
    """
    Intent: Five screens each carried their own copy of the run helpers — showAlert five
        times, the elapsed clock four, setStat three — and they had already drifted: two
        copies had dropped the timed-elapsed branch, and the comment explaining why the clock
        comes from the server survived in only two of the four. Duplicated code does not stay
        identical, and the divergence is invisible until a screen behaves differently.
    Success: /static/app.js is served and defines the helpers, and no template defines them
        again.
    Feature: Shared look — one copy of the shared JavaScript.
    """
    shared = client.get("/static/app.js")
    assert shared.status_code == 200
    for helper in ("formatElapsed", "formatDuration", "showAlert", "setStat", "RunClock"):
        assert helper in shared.text

    for screen in SCREENS + ("/duplicates",):
        body = client.get(screen).text
        page = body[body.index("<main"):]
        for helper in ("function showAlert(", "function formatElapsed(",
                       "function setStat(", "function formatDuration("):
            assert helper not in page, (screen, helper)


def test_every_screen_loads_the_shared_helpers_before_its_own_script(client):
    """
    Intent: The per-screen scripts call into the shared file at parse time — they bind its
        functions to their own elements — so loading it afterwards would fail on the first
        line of every screen that watches a run.
    Success: The shared script tag appears before the per-screen block on every screen.
    Feature: Shared look — the shared script loads first.
    """
    for screen in SCREENS + ("/duplicates",):
        body = client.get(screen).text
        assert "/static/app.js" in body, screen
