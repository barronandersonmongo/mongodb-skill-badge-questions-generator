"""Tests for the coverage screen at /coverage.

It used to be a dialog on the questions screen. It answers "what should I run
next", which is asked before looking at questions rather than while looking at
them, and its rows lead into the list.

Assertions are on markup, never on a bare word: the screens ship JavaScript
containing the same labels they render.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

PAGE = "/coverage"
THEME = Path(__file__).parent.parent / "app" / "static" / "theme.css"


def _theme() -> str:
    """The stylesheet as text. The screens are styled from it and nowhere else, so a
    rule the heat map depends on is asserted where it actually lives."""
    return THEME.read_text()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://test")


@pytest.fixture(autouse=True)
def isolated_storage(fake_collection, fake_questions):
    """The questions screen is rendered here too, and it reads from storage."""


def test_the_screen_offers_the_coverage_panel(client, fake_collection, fake_questions):
    """
    Intent: Coverage is proportional to how much documentation a badge has, so some badges
        come out thin. That is only a workflow rather than a defect if the author can see
        which ones — otherwise the imbalance is invisible until someone builds a quiz.
    Success: The screen offers a coverage panel.
    Feature: Question coverage — reachable from the authoring screen.
    """
    # The panel became a screen, so what the authoring screen offers is the route to it.
    # The requirement recorded above is unchanged: which badges are thin has to be
    # visible from where questions are written.
    assert 'href="/coverage"' in client.get("/").text
    assert 'data-coverage-body="true"' in client.get(PAGE).text


def test_the_rows_are_fetched_after_the_page_paints(client):
    """
    Intent: Resolving every badge's page set is dozens of vector searches — seconds to tens of
        seconds. Rendered with the page it would hold the whole screen blank for all of them,
        which reads as broken rather than as slow. This is the one list in the program not
        rendered server-side, and it is a deliberate exception.
    Success: The screen renders a container and a script that fills it, and says what the
        wait is for.
    Feature: Coverage screen — the slow part does not block the page.
    """
    body = client.get(PAGE).text
    assert 'data-coverage-body="true"' in body
    assert 'const API = "/api/questions"' in body
    assert 'fetch(API + "/coverage")' in body
    assert "takes a few seconds" in body


def test_a_badge_row_leads_to_its_questions(client):
    """
    Intent: "This badge is thin" is only actionable next to what it already has — the next
        thing anyone does with a thin badge is look at its questions, and copying a slug into
        a filter by hand is the step in the way.
    Success: Each row links to the questions list filtered to that badge.
    Feature: Coverage screen — rows lead into the list.
    """
    body = client.get(PAGE).text
    assert '"/?skill_badge=" + encodeURIComponent(row.skill_badge)' in body


def test_the_screen_offers_a_chart_above_the_table(client):
    """
    Intent: The table answers "which badge is thinnest" one row at a time, which is the
        question asked while reading down it. The shape of the whole bank — a few badges
        holding most of it, a tail holding almost none — is not in any single row, and
        reading it off a column of numbers is work the screen can do instead.
    Success: The screen renders a chart container and its canvas, and they precede the
        table's container.
    Feature: Coverage screen — the bank's shape above its numbers.
    """
    body = client.get(PAGE).text
    assert 'data-coverage-heatmap="true"' in body
    assert 'data-coverage-chart="true"' in body
    assert body.index('data-coverage-heatmap="true"') < body.index('data-coverage-body="true"')


def test_the_chart_places_a_badge_by_material_and_by_questions(client):
    """
    Intent: A bubble at a position says two things at once, and the two worth saying about
        a badge are the ones the table already lists: how much documentation it has left to
        walk, and how many questions have come out of it. Placed that way the corners are
        the answer to "what next" — far right and low is a badge with material and nothing
        written from it; far left and low is one whose material is spent and needs the
        corpus widening instead.
    Success: The chart is a bubble chart with chunks left on x and questions created on y,
        both axes titled and starting at zero so distances are comparable.
    Feature: Coverage chart — position carries material and output.
    """
    body = client.get(PAGE).text
    assert 'type: "bubble"' in body
    # The x axis gained a second reading, so what is asserted is the count reading and
    # that it is what the chart opens on. The requirement recorded above is unchanged.
    assert "value: (row) => row.pages_available" in body
    assert "x: X_MODES.count.value(row)" in body
    assert "y: row.total" in body
    assert 'axis: "Chunks left to walk"' in body
    assert "text: X_MODES.count.axis" in body
    assert 'text: "Questions created"' in body
    assert body.count("beginAtZero: true") == 2


def test_a_bubble_is_sized_by_the_questions_it_holds(client):
    """
    Intent: The chart only says anything if size means one thing, and the thing to compare
        is how much of the bank each badge holds. Chart.js takes a radius, so sizing it by
        the count directly would show a badge with four times the questions as sixteen
        times the badge — overstating the very imbalance the chart exists to report.
    Success: The radius is the square root of the badge's share of the fullest badge,
        between a floor and a ceiling.
    Feature: Coverage chart — area carries the count.
    """
    body = client.get(PAGE).text
    assert "Math.sqrt(total / largest)" in body
    assert "BUBBLE_MIN + (BUBBLE_MAX - BUBBLE_MIN)" in body
    assert "r: bubbleRadius(row.total, largest)" in body


def test_a_bubble_leads_to_that_badges_questions(client):
    """
    Intent: A chart that reports an imbalance and cannot be acted on leaves the author back
        at the table to find the same badge again. The next thing anyone does with a badge
        the chart singles out is read what it already holds.
    Success: Clicking a bubble goes to the questions list filtered to that badge — the same
        address the table's rows use — and the pointer says the bubbles can be clicked.
    Feature: Coverage chart — bubbles lead into the list.
    """
    body = client.get(PAGE).text
    assert 'window.location.href = "/?skill_badge=" + encodeURIComponent(point.slug)' in body
    assert 'elements.length ? "pointer" : "default"' in body


def test_a_badge_with_no_questions_is_still_visible(client):
    """
    Intent: An empty badge is the most useful thing the chart can report — it is the one
        that should be run next. Sized honestly it would be a dot too small to see or aim
        at, so the reading the chart is most worth having is the one it would lose.
    Success: Bubbles have a floor radius, and a badge holding nothing is drawn hollow and
        in grey rather than as a very small filled bubble.
    Feature: Coverage chart — an empty badge is not a dot.
    """
    body = client.get(PAGE).text
    assert "const BUBBLE_MIN" in body
    # The fill is now scaled per bubble, so the hollow case is asserted on its own branch
    # rather than on a whole expression. The requirement recorded above is unchanged.
    assert 'p.y === 0 ? "transparent" : withAlpha(forest' in body
    assert "p.y === 0 ? ink3 : forest" in body


def test_a_badge_whose_material_is_unknown_is_left_off(client):
    """
    Intent: Coverage returns no chunk count for a badge whose material could not be
        resolved, and the table prints an em dash for it. Charting that as zero would place
        it in the corner that means "material spent", which is the opposite of not knowing
        — a chart that states something false is worse than one that omits a badge the
        table still lists.
    Success: Only badges with a chunk count are plotted, and the panel is hidden when that
        leaves nothing.
    Feature: Coverage chart — an unknown is not a zero.
    """
    body = client.get(PAGE).text
    assert "rows.filter((row) => row.pages_available !== null)" in body
    assert "rows.some((row) => row.pages_available !== null)" in body


def test_the_chart_is_not_shown_when_there_is_nothing_to_draw(client):
    """
    Intent: An empty panel above an explanation reads as a panel that failed to load. With
        no badges at all, when the fetch fails, and when no badge has a chunk count, the
        table's own message is the whole story and a blank grid above it contradicts it.
    Success: The chart's panel is hidden in each of those three cases.
    Feature: Coverage chart — nothing to draw is not an empty box.
    """
    body = client.get(PAGE).text
    assert body.count('heatmap.closest(".panel").hidden = true') == 3


def test_the_chart_takes_its_look_from_the_theme(client):
    """
    Intent: Every screen here is built from the shared macros and styled from theme.css
        alone. A chart drawn by a library is the easiest place for a one-off palette and a
        hand-picked size to get in, and one screen with its own look is how the screens
        drifted apart before the theme existed.
    Success: The chart's colours are read from the stylesheet's custom properties rather
        than written into the script, and its height comes from a themed frame rather than
        from canvas attributes.
    Feature: Coverage chart — the shared look, not a library's own.
    """
    body = client.get(PAGE).text
    assert "getPropertyValue(name)" in body
    for name in ('themeColor("--forest")', 'themeColor("--ink-3")', 'themeColor("--line")'):
        assert name in body
    assert 'class="chart-frame"' in body
    assert "width=" not in body[body.index("<canvas") : body.index("</canvas>")]
    assert ".chart-frame" in _theme()


def test_the_charting_library_is_loaded_only_where_it_is_used(client):
    """
    Intent: This is the only screen with a chart. Loaded in the shell, every other screen
        would fetch 200 kB it never draws with, and the shell is shared by all of them.
    Success: Chart.js is requested from the coverage screen and not from the shell, and it
        is pinned with an integrity hash like every other thing loaded from a CDN here.
    Feature: Coverage chart — the library is this screen's cost, not the shell's.
    """
    body = client.get(PAGE).text
    assert "chart.js@4.4.3" in body
    assert 'integrity="sha384-' in body[body.index("chart.js@4.4.3") - 200 : body.index("chart.js@4.4.3") + 400]
    assert "chart.js" not in client.get("/").text


def test_each_bubble_carries_its_badges_name(client):
    """
    Intent: A bubble that has to be hovered to say which badge it is makes the chart a
        lookup rather than a glance — and the reading it exists for, "which badge is that
        one out on its own", is exactly the one hovering cannot give, because it is about
        several bubbles at once.
    Success: The chart draws each badge's name against its bubble, set in the theme's own
        type rather than a charting library's default.
    Feature: Coverage chart — a bubble says which badge it is.
    """
    body = client.get(PAGE).text
    assert 'id: "badgeLabels"' in body
    assert "plugins: [badgeLabels]" in body
    assert "ctx.fillText(text, element.x, top)" in body
    assert 'themeColor("--font")' in body


def test_a_name_is_set_beside_the_bubble_at_one_size(client):
    """
    Intent: The largest bubble here is about 68px across and most badge names are three
        words, so a name written inside its bubble has to shrink to fit — and a name set
        small enough to fit a small bubble is not a name anyone reads. The chart would then
        have labels that are only decoration.
    Success: Names are drawn under the bubbles at a single size, and one too long for the
        space is trimmed with an ellipsis rather than set smaller.
    Feature: Coverage chart — names are read, not decoration.
    """
    body = client.get(PAGE).text
    assert "element.y + element.options.radius + LABEL_GAP" in body
    assert "const LABEL_MAX_WIDTH" in body
    assert 'text.trimEnd() + "…"' in body


def test_names_that_would_collide_are_dropped(client):
    """
    Intent: With 34 badges and a tail of thin ones clustered together, labelling every
        bubble overprints the cluster into a smear that names none of them — worse than no
        labels at all, because it also obscures the bubbles underneath.
    Success: Names are drawn largest bubble first, and one that would land on a name already
        drawn, or outside the plotting area, is dropped. The tooltip still names any bubble
        under the pointer.
    Feature: Coverage chart — labels give way rather than overprint.
    """
    body = client.get(PAGE).text
    assert ".sort((a, b) => b.element.options.radius - a.element.options.radius)" in body
    assert "if (clash) return;" in body
    assert "chart.chartArea.right" in body
    assert "tooltip: {" in body


def test_the_labels_do_not_add_a_dependency(client):
    """
    Intent: The stack is deliberately small — no bundler, no framework, and every CDN script
        is a thing to keep pinned and current. Chart.js has no labels for a scatter and the
        usual answer is a second plugin from the CDN, which is a dependency for what the
        canvas already does in a dozen lines.
    Success: The labels are drawn by a plugin defined in this screen, and no second charting
        script is loaded.
    Feature: Coverage chart — labels without a second library.
    """
    body = client.get(PAGE).text
    assert "afterDatasetsDraw(chart, args, opts)" in body
    assert "datalabels" not in body
    assert body.count("cdn.jsdelivr.net/npm/chart.js") == 1


def test_a_fuller_badge_is_drawn_darker(client):
    """
    Intent: Area alone is a weak signal at the small end of a scale spanning an order of
        magnitude — the difference between a badge with four questions and one with nine is
        a few pixels of radius, and the eye reads a pale disc as an absence. Depth of colour
        is the reading that survives at a glance and at presentation distance.
    Success: The fill's opacity rises with the questions the badge holds, following the same
        square root the radius does so colour and size say one thing rather than two, and it
        stops short of solid because bubbles overlap.
    Feature: Coverage chart — the fill deepens with the bubble.
    """
    body = client.get(PAGE).text
    assert "FILL_MIN + (FILL_MAX - FILL_MIN) * Math.sqrt(total / largest)" in body
    assert "withAlpha(forest, bubbleFill(p.y, largest))" in body
    assert "const FILL_MAX = 0.8;" in body


def test_the_x_axis_can_be_read_as_a_count_or_as_a_share(client):
    """
    Intent: The two questions asked of "chunks left" are different and neither answers the
        other. A count is what a run is booked against — 300 chunks left is worth a walk
        whatever fraction of the badge that is. A share is how far through a badge the work
        has got, which the count cannot say: 40 left is nearly done on a large badge and
        barely started on a small one, and a count puts those two in the same place.
    Success: A segmented control switches the x axis between the two, and the axis title
        and the sentence explaining the chart change with it.
    Feature: Coverage chart — chunks left, as a count or as a share.
    """
    body = client.get(PAGE).text
    assert 'data-x-mode="count"' in body
    assert 'data-x-mode="share"' in body
    assert 'axis: "Chunks left to walk"' in body
    assert "axis: \"Share of the badge's chunks left to walk\"" in body
    assert 'data-chart-legend="true"' in body
    assert 'document.getElementById("chart-legend").textContent = mode.legend' in body


def test_the_share_is_of_the_badges_whole_chunk_set(client):
    """
    Intent: A share needs a denominator, and the only honest one is everything that badge
        resolves to — what has been walked plus what is left. Taken against the corpus, or
        against the largest badge, the figure would answer a question nobody asked.
    Success: The share is chunks left over walked plus left, as a percentage, and its axis
        is pinned to 100 so a screen where no badge is above 60% left does not stretch that
        to the right-hand edge and read as "nothing walked yet".
    Feature: Coverage chart — the share is of the badge itself.
    """
    body = client.get(PAGE).text
    assert "(row.pages_used || 0) + row.pages_available" in body
    assert "(row.pages_available / whole) * 100" in body
    assert "max: 100," in body
    assert 'tick: (value) => value + "%"' in body


def test_a_badge_with_no_chunk_set_has_no_share(client):
    """
    Intent: Nought chunks walked and nought left is a badge that resolves to no
        documentation at all. Drawn at 0% it would sit at the left-hand edge among the
        badges whose material is spent, which is a claim about a badge nothing is known
        about — the same mistake as charting an unknown chunk count as zero.
    Success: Such a badge has no share, and is absent from that reading rather than placed
        at either end of it.
    Feature: Coverage chart — no chunk set is not nought per cent.
    """
    body = client.get(PAGE).text
    assert "if (!whole) return null;" in body
    assert "point.x = mode === X_MODES.share ? point.share : point.count;" in body


def test_switching_the_axis_keeps_the_same_bubbles(client):
    """
    Intent: The two readings are two views of one set of badges. Rebuilt from scratch, the
        chart animates every bubble in from nothing as though the data had changed, and the
        badge the author was looking at is lost in the redraw.
    Success: The toggle moves the existing points and updates the chart rather than
        constructing a new one, and the buttons say which reading is showing.
    Feature: Coverage chart — a toggle, not a redraw.
    """
    body = client.get(PAGE).text
    assert "chart.update();" in body
    assert body.count("new Chart(canvas") == 1
    assert 'button.classList.toggle("active", on)' in body
    assert 'button.setAttribute("aria-pressed"' in body


def test_the_tooltip_gives_both_readings(client):
    """
    Intent: The tooltip is where the figure behind a bubble is checked, and a percentage
        with no count behind it is exactly the number people mistrust — 50% left is four
        chunks on one badge and 120 on another.
    Success: The tooltip names the badge and gives the questions, the count of chunks left
        and its share, whichever axis is showing.
    Feature: Coverage chart — the tooltip does not depend on the axis.
    """
    body = client.get(PAGE).text
    assert 'point.count + " chunks left" + share' in body
    assert 'Math.round(point.share) + "%)"' in body


def test_every_column_of_the_table_can_be_sorted(client):
    """
    Intent: The rows arrive thinnest-first, which answers "what should I run next" and
        nothing else. The same list also answers "which badge has the most questions" and
        "where is the most material sitting unused", and both are a re-sort of what is
        already on the screen rather than another request.
    Success: Each of the three headers sorts the table by its own column, from the browser,
        without fetching again.
    Feature: Coverage table — every column sorts.
    """
    body = client.get(PAGE).text
    for key in ('key: "name"', 'key: "total"', 'key: "pages_available"'):
        assert key in body
    assert "sortRows(tbody, rows, column," in body
    # One fetch on the screen: sorting is a re-order of rows already held.
    assert body.count('fetch(API + "/coverage")') == 1


def test_a_header_is_a_button_and_says_how_it_is_sorted(client):
    """
    Intent: A header that only responds to a mouse leaves the table unsortable from the
        keyboard, and an arrow drawn on it says which way round it is to someone who can
        see the arrow and to nobody else.
    Success: Headers are buttons, the sorted one carries the direction as a data attribute
        for the stylesheet and as aria-sort on its cell, and the stylesheet draws the arrow
        rather than the script writing one into the label.
    Feature: Coverage table — the sort is visible and reachable.
    """
    body = client.get(PAGE).text
    assert 'button.type = "button"' in body
    assert 'button.className = "sort-header"' in body
    assert 'setAttribute("aria-sort"' in body
    assert ".sort-header::after" in _theme()
    assert '.sort-header[data-sorted="ascending"]::after' in _theme()


def test_a_column_starts_at_the_direction_it_is_wanted_in(client):
    """
    Intent: Sorting always ascending first means a click on a count gives the smallest
        numbers, which is not what a count is usually asked for, and every use costs two
        clicks. Names are the opposite: nobody asks for badges from Z.
    Success: A name column starts ascending and a count column starts descending, and
        clicking the column already sorted turns it round rather than restarting it.
    Feature: Coverage table — one click gives the order that was wanted.
    """
    body = client.get(PAGE).text
    assert "ascendingFirst: true," in body
    assert "ascendingFirst: false," in body
    assert "turn ? !sort.ascending : column.ascendingFirst" in body


def test_a_badge_with_no_chunk_count_sorts_to_the_end(client):
    """
    Intent: A badge whose material could not be resolved prints an em dash rather than a
        number. Treated as zero it would lead an ascending sort, putting the rows nothing
        is known about above the rows the screen exists to surface; treated as large it
        would lead the other. It is neither small nor large.
    Success: Rows with no chunk count sort to the end whichever way the column is turned.
    Feature: Coverage table — an unknown is not a zero.
    """
    body = client.get(PAGE).text
    assert "if (left === null && right === null) return 0;" in body
    assert "if (left === null) return 1;" in body
    assert "if (right === null) return -1;" in body


def test_the_table_opens_thinnest_first(client):
    """
    Intent: The screen's whole purpose is "what should I run next", and the answer is the
        thinnest badge. Opening on whatever order storage returned, or on an alphabetical
        list, would make the author sort before reading every time.
    Success: The table is sorted by questions created, ascending, before it is first shown,
        and the header shows that as a sort like any other rather than as an implicit order.
    Feature: Coverage table — it opens on the question it answers.
    """
    body = client.get(PAGE).text
    assert "sortRows(tbody, rows, COLUMNS[1], true);" in body
