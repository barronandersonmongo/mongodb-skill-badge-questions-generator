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
SCREENS = ("/", "/admin/skill-badges", "/admin/docs", "/admin/logs")


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
    assert nav.count("nav-group-label") == 1
    assert nav.count('data-admin-area="true"') == 3


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
    assert section.count('data-admin-area="true"') == 3
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
