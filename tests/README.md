# Tests

Automated tests for every feature and function in this program. Changes are
expected to pass them.

## Running the tests

From the repository root, with the virtualenv created per the top-level
[README](../README.md):

```bash
.venv/bin/python -m pytest                 # whole suite
```

No setup beyond `pip install -r requirements-dev.txt` is required. The tests
never reach MongoDB Atlas or the Claude API — see [Isolation](#isolation).

### Narrower runs

```bash
.venv/bin/python -m pytest tests/test_badge_discovery.py          # one file
.venv/bin/python -m pytest -k discovery                           # by name
.venv/bin/python -m pytest tests/test_config.py::test_settings_are_immutable
.venv/bin/python -m pytest -x                                     # stop at first failure
.venv/bin/python -m pytest -vv                                    # show each test name
```

`pytest.ini` sets `testpaths = tests` and `-q`, so a bare `pytest` finds the
suite from the repository root with quiet output.

### Coverage

```bash
.venv/bin/python -m coverage run -m pytest
.venv/bin/python -m coverage report          # currently 100% of app/
.venv/bin/python -m coverage html            # browsable report in htmlcov/
```

`.coveragerc` scopes coverage to `app/` and excludes `if __name__ ==
"__main__":` lines.

## Layout

| File | Covers |
|---|---|
| `test_config.py` | `app/config.py` — credential resolution, defaults, immutability |
| `test_db.py` | `app/db.py` — Atlas client, pooling, fail-fast on missing credentials |
| `test_models.py` | `app/models/skill_badge.py` — badge schemas and constraints |
| `test_models_question.py` | `app/models/question.py` — question schemas and constraints |
| `test_question_generation.py` | `app/services/question_generation.py` — authoring, extraction, format validation, attribution |
| `test_question_duplicates.py` | `app/services/question_duplicates.py` — the ad-hoc duplicate sweep |
| `test_reranker.py` | `app/services/reranker.py` — Voyage rerank client (stubbed HTTP) |
| `test_repositories_questions.py` | `app/repositories/questions.py` — insert, filter, lifecycle |
| `test_questions_router.py` | `app/routers/questions.py` — question JSON API under `/api/questions` |
| `test_questions_page.py` | `app/routers/pages.py` + `questions.html` — the main screen at `/` |
| `test_badge_discovery.py` | `app/services/badge_discovery.py` — the two Claude passes |
| `test_repositories_skill_badges.py` | `app/repositories/skill_badges.py` — upsert, listing, lifecycle |
| `test_admin_skill_badges_router.py` | `app/routers/admin_skill_badges.py`, `app/main.py` — JSON API under `/api/admin` |
| `test_admin_pages.py` | `app/routers/admin_pages.py` + templates — the `/admin` badge screens |
| `test_discover_cli.py` | `app/services/discover_cli.py` — shell entry point |
| `test_logging_config.py` | `app/logging_config.py` — rotation budget, tailing the file |
| `test_admin_logs.py` | `app/routers/admin_logs.py` + `admin/logs.html` — the log viewer |
| `test_test_documentation.py` | The suite itself — enforces the convention below |
| `conftest.py` | Shared fixtures (credential scrubbing, in-memory collection) |
| `fakes.py` | Test doubles: in-memory MongoDB collection, scripted Anthropic client |

## Documentation convention

**Every test carries an `Intent` / `Success` / `Feature` block, and those blocks
are never edited.**

```python
def test_new_badges_land_as_candidates(fake_collection):
    """
    Intent: Nothing Claude discovers is trusted on arrival; a human promotes it.
        New badges must therefore enter review as candidates.
    Success: A newly inserted badge has status "candidate".
    Feature: Badge lifecycle — human approval gate.
    """
```

- **Intent** — why the test exists and what would go wrong without it.
- **Success** — what passing actually proves.
- **Feature** — the business feature or function under test.

The block is the recorded requirement, not commentary. When behavior needs to
change, change the program, or add a new test alongside with its own block. A
test is never loosened just to make a failing program pass — that discards the
requirement. If a requirement itself is genuinely wrong, say so explicitly and
state the new expected behavior before touching anything.

`test_test_documentation.py` enforces this mechanically: it walks every
`test_*.py` with the `ast` module and fails if any test function lacks a
docstring, is missing a section, or leaves one blank. A new test without a block
fails the suite.

## What earns a test

A test must encode a requirement of **this program**. A test that only
demonstrates Python, Pydantic, pymongo, or FastAPI working as documented adds
maintenance cost and dilutes the signal from real failures — those are not kept.

Concretely, keep a test that pins:

- a decision someone could plausibly reverse by accident (badge identity is the
  slug; approval survives re-discovery; absence never retires);
- an error path whose silent failure would be misread as an empty result
  (a refusal, a truncated extraction, a failed background run);
- a boundary contract another component relies on (no `_id` in listings, 404 on
  an unknown slug, JSON on stdout and diagnostics on stderr);
- wiring that would otherwise fail only in production (routes mounted, one Mongo
  client per process, credentials never hardcoded).

Do not keep a test that restates a type annotation, re-checks a stdlib guarantee
such as `frozen=True` or `uuid4()` uniqueness, or duplicates an assertion an
existing test already makes at a more meaningful layer.

## Asserting on rendered HTML

Assert on **markup** — an element, a class, or a `data-` attribute — not on a bare
word. Page templates ship JavaScript that contains the same labels the page
renders ("Approve", "edited", "Show stack trace"), so a substring check on the
label passes whether or not the element was rendered. Three tests in this suite
were initially wrong for exactly that reason. Where no stable hook exists, add a
`data-` attribute to the template rather than matching on prose.

## Isolation

Tests are hermetic — they pass with no network, no Atlas cluster, and no
Anthropic API key:

- **Credentials** — an autouse fixture in `conftest.py` deletes
  `PTM_HACKATHON_CONNECTION_STRING`, `MONGODB_URI`, `ANTHROPIC_API_KEY`, and
  `ANTHROPIC_AUTH_TOKEN`, so no test can silently depend on a developer's real
  environment. Tests that need a URI set a fake one explicitly.
- **MongoDB** — `fakes.FakeCollection` is an in-memory stand-in implementing only
  the operators this program uses (`$set`, `$setOnInsert`, equality filters,
  projection, sort). The `fake_collection` fixture points the repository at it.
- **Claude** — `fakes.FakeAnthropic` is a scripted double. Tests hand it the
  messages to return (including `pause_turn` and `refusal` turns) and then assert
  on the recorded request parameters.

`mongomock` is deliberately not used: as of mongomock 4.3.0 / pymongo 4.17.0 its
`bulk_write` path breaks on pymongo's newer `add_update()` signature.

## What the suite does not cover

The discovery path has never run against the live Claude API. The tests prove the
control flow — streaming, `pause_turn` resume, refusal handling, schema-validated
extraction — but not that the prompts return good badges. That needs a real run
with `ANTHROPIC_API_KEY` set:

```bash
.venv/bin/python -m app.services.discover_cli --dry-run
```
