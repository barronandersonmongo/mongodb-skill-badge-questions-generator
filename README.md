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
`SKILL_BADGE_CATALOG_URL`, `CREDLY_COLLECTION_URL`, `VECTOR_INDEX_NAME`.

Storage target is the `skill-badge-questions` database on the `PTM-Hackathon`
cluster (Atlas project "Barry Anderson").

## Run

```bash
.venv/bin/uvicorn app.main:app --reload
```

Then open **<http://127.0.0.1:8000/admin>** — the admin area. `/` and `/admin`
both redirect to the skill badges screen. API docs are at `/docs`.

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

Server-rendered screens at `/admin` (Jinja2 + Bootstrap 5 from CDN, no build step):

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
app/db.py                            Mongo client (one per process)
app/models/skill_badge.py            Pydantic schemas (Claude output + stored doc)
app/services/badge_discovery.py      the two Claude passes
app/services/discover_cli.py         shell entry point
app/repositories/skill_badges.py     upsert / list / status, indexes
app/routers/admin_skill_badges.py    JSON endpoints under /api/admin
app/routers/admin_pages.py           server-rendered pages under /admin
app/templates/base.html              nav shell shared by admin screens
app/templates/admin/skill_badges.html  badge review screen
tests/                               pytest suite + fakes (see tests/README.md)
```

## Known limitations

- Run state is in-process; it needs to move into MongoDB before running multiple
  uvicorn workers.
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
