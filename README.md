# mongodb-skill-badge-questions-generator

A tool to generate questions for use with MongoDB Credly Skill Badge assessments.

Internal authoring tool for the MongoDB teams who build skill badge quizzes. It
generates, validates, stores, filters, and exports quiz questions. It is not a
quiz-taking platform — no learners, no scoring, no attempts.

## Stack

Python 3.14 (stdlib `venv` + `pip`), FastAPI + Jinja2 server-rendered HTML,
MongoDB Atlas, Claude via the `anthropic` SDK, Bootstrap 5 / vanilla JS from CDN.
No Node, no bundler, no Docker, no task queue.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

export PTM_HACKATHON_CONNECTION_STRING='mongodb+srv://...'   # Atlas: PTM-Hackathon cluster

# Claude access — either a direct key:
export ANTHROPIC_API_KEY='sk-ant-...'                        # or run `ant auth login`
# …or an internal gateway fronting the Anthropic Messages API:
export GROVE_PRIMARY_KEY='...'                               # secondary key used as fallback
export GROVE_ANTHROPIC_BASE_URL='https://.../anthropic'      # part before /v1/messages
```

`~/.profile` is read only by login shells, so a variable exported there is not
visible to cron, systemd, or anything started as a service.

Optional overrides: `ANTHROPIC_MODEL`, `WEB_SEARCH_TOOL_TYPE`, `WEB_FETCH_TOOL_TYPE`,
`SKILL_BADGE_CATALOG_URL`, `CREDLY_COLLECTION_URL`, `VECTOR_INDEX_NAME`,
`QUESTIONS_VECTOR_INDEX_NAME`, `LOG_DIR`, `LOG_LEVEL`, `VOYAGE_API_KEY`,
`VOYAGE_RERANK_MODEL`, `VOYAGE_BASE_URL`.

Storage target is the `skill-badge-questions` database on the `PTM-Hackathon`
cluster (Atlas project "Barry Anderson").

## Run

```bash
.venv/bin/uvicorn app.main:app --reload
```

Then open **<http://127.0.0.1:8000/>** — the questions screen, which is the main
screen. The badge catalog is at `/admin`. API docs are at `/docs`.

If MongoDB is unreachable the screen still loads and names the problem rather
than returning a stack trace.

## Tests

Every feature and function has automated tests. See **[tests/README.md](tests/README.md)**
for how to run them, the suite layout, and the documentation convention.

```bash
.venv/bin/python -m pytest
.venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report
```

Each test carries an `Intent` / `Success` / `Feature` block recording why it
exists, what passing proves, and which feature it protects. Those blocks are
never edited — to change behavior, change the program.

Tests never touch Atlas or the Claude API: an autouse fixture strips real
credentials from the environment, MongoDB is an in-memory fake
(`tests/fakes.py`), and the Anthropic client is a scripted double.

> `mongomock` is deliberately not used — as of mongomock 4.3.0 / pymongo 4.17.0
> its `bulk_write` path breaks on pymongo's newer `add_update()` signature.

## Features

### Two areas

| Area | What it is for |
|---|---|
| `/` | **Authoring** — write, review, filter and export questions |
| `/admin` | **Curation** — maintain the badge catalog the questions are scoped by |

The split is a logical boundary between two kinds of work, so each screen has one
audience. **It is not a permission boundary**: there are no authorizations
anywhere in this program, and anyone who can reach the service can reach both
areas. Their JSON follows the same split — `/api/questions` and `/api/admin/...`.

### Questions

The main screen (`/`) is where questions are viewed and written.

**Generating.** A run is scoped to one or more skill badges: the badge decides
what is in subject matter. Two Claude passes, as with badge discovery — an
authoring pass that reads the selected badges (title, coverage, topic areas and
curated reference links), any material the author pasted in, and MongoDB's
documentation via server-side web search and fetch; then an extraction pass that
turns the draft into validated records. The draft is kept on the run summary so a
weak batch can be diagnosed.

Runs happen in the background with the page polling for status and showing an
elapsed timer, because an authoring turn takes minutes.

The badges scope the questions but are not the whole source. Deeper material —
internal training content, live cluster behaviour — is pasted into **Source
material** and preferred over anything Claude finds itself: the server has no
Glean or MongoDB MCP access of its own.

**Questions are filed under every badge they test.** A question written for one
badge often tests others — skills overlap. So each finished question is reviewed
against the whole badge catalog in a third pass, reading the stem, all four
options and the explanation, because what a question really tests is often only
visible in what separates the correct option from the wrong ones. Any badge whose
subject matter it genuinely tests is added.

That pass can only widen a question's reach, never narrow it: the badges it was
written for are always kept, and a slug matching no stored badge is dropped. The
run reports how many questions were cross-filed and why, so an over-eager pass is
visible rather than silent. If the pass itself fails, the questions are still
stored under the badges they were written for and the screen says the review did
not run — an authoring turn is never discarded over a follow-up step.

**The format is enforced, not requested.** Every stored question is multiple
choice with exactly four options, exactly one correct, no repeated or empty
options, and a non-empty stem. A question failing any of those is discarded and
reported with the reason rather than stored for a reviewer to find. A malformed
question never fails the batch it came in — the rest are kept.

Badge attribution is also enforced: a badge slug outside the run's selection is
dropped, and a question the model left untagged is attributed to the badges the
run was scoped to. `skill_badges` is what the collection is filtered by, so a
wrong value there would make a question unfindable.

**Reviewing.** Questions arrive as **drafts**. The screen shows the stem, all four
options with the correct one marked, each option's rationale, the explanation, the
difficulty, the badges and categories, and the sources — everything needed to
judge quality. Per question: approve, reject, re-open, delete.

**Embedding text for vector search.** Every stored question carries
`embedding_text`, the stem and explanation as one labelled block:

```
Question: Which stage filters documents first in an aggregation pipeline?
Explanation: $match placed before $project lets the index be used.
```

That is the path to point an Atlas Vector Search index at. Use **autoEmbed**, so
Atlas embeds both the stored text and the query — this program stores no vectors,
the same arrangement as the badge `description` index. The labels are part of the
embedded string deliberately: an unlabelled concatenation reads as one run-on
sentence and loses the cue about which part is the question and which is the
reasoning behind its answer. An absent explanation drops its section rather than
embedding a dangling label.

The field is composed on write, so a question is embeddable the moment it lands.
For questions written before the field existed, `POST
/api/questions/backfill-embedding-text` composes it — safe to re-run, and it only
writes documents whose text is missing or has drifted from the current stem and
explanation, so autoEmbed is not asked to re-embed unchanged text.

The index in use is **`questions_embedding_text_vector`** (autoEmbed,
`voyage-4-large`), overridable with `QUESTIONS_VECTOR_INDEX_NAME`. It drives two
things: duplicate screening at generation time, and the search box on the main
screen.

**Duplicate detection — an ad-hoc sweep, no LLM.** **Find duplicates** on the main
screen compares the questions already stored. Generation runs do not screen: an LLM
judging every candidate pair was accurate but slow and expensive, and the cost fell
on authoring, the one step a person waits for. A duplicate costs nothing until
someone builds a quiz, so it is found on request instead.

Two stages, neither a language model:

1. **Shortlist** — vector search over `embedding_text` proposes each question's
   nearest neighbours. Recall matters here and precision does not, so the floor
   (`question_duplicate_score_threshold`, 0.70) sits *below* the 0.765-0.783 band
   measured for genuinely distinct questions. A generous shortlist costs one cheap
   rerank call; a tight one costs a missed duplicate.
2. **Decide** — Voyage `rerank-2.5` scores each shortlisted pair. A cross-encoder
   reads both texts together, which is what lets it separate "same question
   reworded" from "same topic" — something two independently embedded vectors
   cannot do.

Pairs at or above `question_rerank_delete_threshold` (0.95) have one question
deleted; the rest are reported with their scores. The survivor is chosen by review
value: approved beats draft, then more badges, then older.

**Calibrate before trusting it to delete.** The delete threshold was set without any
real duplicates to measure against, so it is deliberately high. **Dry run** reports
exactly what a sweep would delete without deleting it. Do that first.

On MongoDB **8.3+** this becomes the native `$rerank` aggregation stage — no API key
and no second round trip. This cluster runs 8.0, so `app/services/reranker.py` calls
the same model over HTTP; it is the only place that knows how a score is obtained,
so the swap is a change there and nowhere else. Set `VOYAGE_API_KEY` (optionally
`VOYAGE_RERANK_MODEL`, `VOYAGE_BASE_URL`).

**Search by meaning.** The box on the main screen ranks questions by similarity to
what you type, so "joining data from another collection" finds the `$lookup`
questions whether or not they use that word. Scores are shown, so a weak match
reads as one. The badge, category and status filters then narrow those matches
rather than the whole collection, and the query lives in the URL. An index that is
not queryable yet is reported as such rather than returning an empty result that
reads as "we have nothing on that".

**Filtering and export.** Filter by skill badge, category and status; the filters
intersect and live in the URL, so a view can be shared. **Export JSON** returns
exactly the filtered set from the same endpoint the screen reads.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The main screen; `?status=`, `?skill_badge=`, `?category=` |
| `POST` | `/api/questions/generate` | Start a generation run |
| `GET` | `/api/questions/generate/status` | Poll a run |
| `GET` | `/api/questions` | List / export questions, same filters |
| `GET` | `/api/questions/search?q=&limit=` | Questions ranked by similarity to `q` |
| `POST` | `/api/questions/duplicates/sweep?dry_run=` | Find duplicates; delete the clear ones |
| `POST` | `/api/questions/backfill-embedding-text` | Compose `embedding_text` where missing or stale |
| `POST` | `/api/questions/{id}/status` | Approve, reject or re-open |
| `DELETE` | `/api/questions/{id}` | Delete a question |

### Logging

Everything the service logs goes to `logs/app.log`, rotating at **100 MB** and
keeping **10 files** (the active file plus nine backups, ~1 GB at most). `LOG_DIR`
and `LOG_LEVEL` override the location and verbosity.

Logging is configured from the environment rather than from `Settings`, because
`Settings` requires a MongoDB connection string — and "cannot reach Atlas" is
exactly what someone will come to the log to find out.

**Log viewer** at `/admin/logs` shows the tail of the current file, with a
selectable line count and a **Follow** toggle that refreshes every few seconds.
Only the active file is served and the endpoint accepts no path, so the viewer
cannot be turned into a way to read arbitrary files off the server — there are no
authorizations here. Rotated files are listed by name and size so it is clear what
history exists on disk.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/logs` | The log viewer |
| `GET` | `/api/admin/logs?lines=` | Tail of the active log, plus rotated history |

### Badge synchronisation

**Sync from catalog** reads the published Credly collection
(`.../collections/mongodb-skill-badges/badge_templates`, JSON) — that is the
authoritative list of badges. Each sync then:

1. **Reads the title from each badge's artwork** using Claude vision. The artwork
   title is the canonical name: Credly, learn.mongodb.com and the collection API
   all name these badges differently, and only the artwork is consistent.
2. **Derives the slug from that title** (lowercase, hyphenated, punctuation
   dropped), so badge identity follows the name a reviewer sees.
3. **Reconciles with existing records** — identical canonical URLs settle identity
   outright; otherwise Claude compares descriptions so a renamed or hand-corrected
   badge is updated rather than duplicated.
4. **Downloads the artwork** into the badge document (served back by this app, not
   hotlinked).
5. **Verifies the Credly page title** from that page's own markup.
6. **Looks up the learn.mongodb.com title** via site-restricted search — those
   course pages render in the browser, so no fetcher can read them directly.

**Research with Claude** is the older path: web search plus fetch, for badges the
collection may not list yet. Anything without evidence on the catalog domain is
rejected and reported.

### Duplicate detection

**Find duplicates** uses the Atlas Vector Search index (`autoEmbed` on
`description`, so Atlas embeds both documents and queries — this app stores no
vectors). For each badge it takes the nearest neighbours, drops anything below the
score floor, and asks Claude whether the pair is the same badge. Confident
duplicates merge automatically; the rest surface with a **Merge** button. Pairs
sharing a canonical URL are merged without a model call.

A merge keeps the record carrying review work, fills gaps from the other, and
remembers the dropped slug as an alias so a later sync does not re-create it.

### Admin area

The badge catalog, server-rendered at `/admin/skill-badges` (Jinja2 + Bootstrap 5
from CDN, no build step):

- Review table with badge artwork, and every name source labelled — **Credly
  name**, **MongoDB name**, **Artwork name**, **Catalog name** — plus the
  description and cited links.
- Tabs and counts per review state; long-running actions show an elapsed timer.
- Per badge: edit the title, curate the reference links, approve / retire /
  re-open, and delete (retired badges only).
- Hand edits are protected: a corrected title or curated links are never
  overwritten by a later sync.
- **Normalise slugs** re-applies the artwork-title slug rule to existing records.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/skill-badges` | The review screen, `?status=` to filter |
| `POST` | `/api/admin/skill-badges/sync-catalog` | Sync from the Credly collection |
| `POST` | `/api/admin/skill-badges/discover` | Research with Claude |
| `POST` | `/api/admin/skill-badges/duplicates/scan` | Find and merge duplicates |
| `POST` | `/api/admin/skill-badges/merge` | Merge one badge into another |
| `POST` | `/api/admin/skill-badges/normalise-slugs` | Re-derive slugs from artwork titles |
| `GET` | `/api/admin/skill-badges/discover/status` | Poll a run |
| `GET` | `/api/admin/skill-badges?status=` | List badges |
| `GET` | `/api/admin/skill-badges/{slug}/image` | Badge artwork |
| `POST` | `/api/admin/skill-badges/{slug}/name` | Correct a title (locks it) |
| `POST` | `/api/admin/skill-badges/{slug}/sources` | Curate links (locks them) |
| `POST` | `/api/admin/skill-badges/{slug}/status` | Set review status |
| `DELETE` | `/api/admin/skill-badges/{slug}` | Delete a retired badge |

## Layout

```
app/config.py                        environment-driven settings
app/logging_config.py                rotating file log, and reading it back
app/db.py                            Mongo client (one per process)
app/models/skill_badge.py            Pydantic schemas (Claude output + stored doc)
app/models/question.py               question schemas (Claude output + stored doc)
app/services/badge_discovery.py      the two Claude passes
app/services/question_generation.py  the Claude passes for questions
app/services/question_duplicates.py  the ad-hoc duplicate sweep
app/services/reranker.py             Voyage rerank client (native $rerank on 8.3+)
app/services/discover_cli.py         shell entry point
app/repositories/skill_badges.py     upsert / list / status, indexes
app/repositories/questions.py        insert / filter / status, indexes
app/routers/questions.py             question JSON endpoints under /api/questions
app/routers/pages.py                 the questions screen, served at /
app/routers/admin_skill_badges.py    badge JSON endpoints under /api/admin
app/routers/admin_pages.py           server-rendered pages under /admin
app/routers/admin_logs.py            log tail endpoint under /api/admin
app/templates/base.html              nav shell shared by every screen
app/templates/questions.html         the main screen: review and generate
app/templates/admin/skill_badges.html  badge review screen
app/templates/admin/logs.html        log viewer
tests/                               pytest suite + fakes (see tests/README.md)
```

## Known limitations

- Run state is in-process; it needs to move into MongoDB before running multiple
  uvicorn workers. Badge runs and question runs hold separate state, so one of
  each can run at a time. Run start and finish times are recorded on the server,
  so the elapsed timer is correct across a page reload — but a restart still loses
  the state.
- The log viewer shows only the active file. Reading a rotated file still means
  going to the disk.
- Nothing checks whether a newly generated question duplicates an existing one.
  The vector-search machinery used for badge duplicates would transfer, but is
  not wired up for questions.
- The generation path has never run against the live Claude API; the tests prove
  the control flow, not that the prompt produces good questions.
- `learn.mongodb.com` course pages are client-rendered, so their titles come from
  the search index rather than the page. Three badges have no title available by
  any headless method; a rendering browser (e.g. Playwright) would be needed.
- One badge has no readable artwork title, so its name and slug fall back to the
  reviewed title.
- A wipe-and-rebuild reproduces everything machine-derived (badges, slugs, all
  four name sources, artwork, links) but **not** reviewed titles, approvals, or
  curated links. Back up before testing that.

## Open questions

- Which skill badge to target first (determines the doc corpus).
- Whether the MongoDB MCP server exposes documentation search. The MCP server is
  configured read-only in `.mcp.json` and is not yet authorized.
- Embedding provider for duplicate detection (Voyage AI is the current lean).
