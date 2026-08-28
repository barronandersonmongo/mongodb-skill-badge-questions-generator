# MongoDB Skill Badge Questions Generator

An internal authoring tool for MongoDB teams who build skill badge quizzes. It
generates, validates, stores, filters and exports quiz questions.

It is **not** a quiz-taking platform: no learners, no scoring, no attempts. The hard
part — and the point of the tool — is question *quality*, not volume.

## Working style

- **Keep responses short.** One or two paragraphs. Lead with the result, then only what
  changes a decision. Details on request.
- Keep the stack minimal and mainstream. No esoteric frameworks.
- Say what is uncertain rather than presenting a guess as a fact.

## Before you touch anything

**The app is running on this machine** under `uvicorn app.main:app --reload`
(`logs/uvicorn.out`, `logs/app.log`), and Barron starts real generation runs from that
browser tab.

- Editing a template or CSS is safe — Jinja reloads per request.
- Editing anything under `app/**.py` restarts the server. **Mid-run that stalls it at
  "Waiting for background tasks to complete"** and the page serves nothing until the run
  ends; `SIGTERM` will not clear it, only `SIGKILL`.
- So **check for a run first**: `curl -s localhost:8000/api/questions/generate/status`.
  If one is going, batch the Python edits or ask.
- A `200` from the page does not prove the Python is current: templates reload
  independently. Verify the reloader parent is alive, not just the socket.

**Runs cost money and time** — roughly 8–9 minutes and a few dollars per badge. Never
start one to "test" something. Use the test suite.

## The two units, and their words

- A **page** is a documentation page. 7,158 stored.
- A **chunk** is a heading and the passage under it. 27,399 stored — about 3.8 per page,
  up to 253.

A run reads **one chunk per Claude call**, so the chunk is the unit of every count, every
budget and every projection. Say *chunk*, never "section" or "article". Say *page* only
for a real page, or a page of a list.

This matters beyond wording: the generate form's number is chunks, and calling it pages
overstated the available material by about 4×. A walk takes one chunk per page before a
second from any of them, so **distinct pages, not chunks, is the ceiling on new
material** — one badge has 252 chunks across 25 pages.

## Screens

Author surface: `/` questions · `/duplicates` · `/coverage` · `/runs` · `/export`
Admin: `/admin/skill-badges` · `/admin/docs` · `/admin/material` · `/admin/logs`

One shell, `base.html`: sidebar with the author screens above the rule, admin below it.
Every screen is built from the macros in `app/templates/_ui.html` and styled only from
`app/static/theme.css` — never inline. Bootstrap is loaded for its JavaScript; almost
none of its appearance survives.

## Rules

**Tests.** Every feature and function has tests, and every test carries a docstring block
with `Intent:` (why it exists), `Success:` (what passing proves) and `Feature:` (the
feature under test). **Those blocks are never edited** — they are the recorded
requirement. To change behaviour, change the program; or add a new test alongside with
its own block, saying what it replaces and why. Never weaken a test to make the program
pass. `tests/test_test_documentation.py` enforces the blocks; see `tests/README.md`.

Assert on **markup** — an element, a class, a `data-` attribute — never a bare word: the
templates ship JavaScript containing the same labels they render.

Tests never reach Atlas or the Claude API. A test that renders a screen must hold that
screen's collections in memory, or the request sits on a connection that cannot be made
and the suite goes from 10 seconds to minutes.

**Git.** Pull requests, never a direct push to `main`.

**README.** Keep it in sync as changes land. It explains *why*, not what changed.

## Stack

Python 3.14, stdlib `venv` + `pip`. FastAPI + Jinja2, server-rendered, no build step.
MongoDB Atlas for storage, vector search and reranking. Claude via the `anthropic` SDK
through the Grove gateway. Bootstrap 5 + Chart.js from CDN.

No Node, no npm, no bundler, no Docker, no task queue, no RAG framework.

## Data model

- `categories` and `skill_badges` are **arrays** — one question may belong to several of
  each, and must be queryable by both and exportable as JSON.
- A question stores the `source_chunk_ids` it was written from. That is what makes a
  re-run skip material already used, and deleting a question releases its chunks.
- `generation_runs` records every finished run, including the chunk-by-chunk detail. Its
  keys still say `pages_*` while holding chunk counts; renaming them would orphan every
  run already recorded.
- Output must be easy to copy and paste.

## Settled, do not re-open

- **Duplicate detection**: one aggregation per question, `$vectorSearch` to shortlist and
  `$rerank` to score, both on the cluster. No separate embedding provider, no Claude call
  per pair. The 0.85 threshold is a calibrated default, adjustable per report.
- **Documentation corpus**: fetched and stored directly, not through the MCP server.
- **Badge catalog**: synced from MongoDB's published collection, which is authoritative;
  research fills what it does not list. The badge artwork is the canonical name.
- **No review workflow**: every stored question is in use. Deleting is the only *irreversible*
  editorial act. The one reversible one is unfiling a badge from a question, and a question
  always keeps at least one badge.

## Open questions

None outstanding. When one arises, record it here with the decision that would close it.
