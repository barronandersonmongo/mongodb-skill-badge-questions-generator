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
`QUESTIONS_VECTOR_INDEX_NAME`, `DOC_PAGES_VECTOR_INDEX_NAME`, `DOCS_INDEX_URL`, `LOG_DIR`, `LOG_LEVEL`.

Page-walk tuning lives in `app/config.py` rather than the environment, since each
number is a measured judgement documented next to it: `doc_page_set_score_floor`
(0.70), `doc_page_set_size` (400), `doc_reference_url_pattern`, `questions_per_page`
(3), `max_pages_per_run` (25) and `page_author_effort` (`medium`).

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

## Implementation strategies

These are the recurring decisions behind the features below. They are written down
because each one has already been re-derived at least once, and because a change that
quietly breaks one of them looks fine in review.

### 1. Every view is a URL

State that decides what you are looking at lives in the URL, not in the page. Filters,
searches, tabs and drill-downs are all query parameters, so any view can be linked,
bookmarked, shared in Slack, or cited from a question.

| View | Address |
|---|---|
| Questions, filtered | `/?status=&skill_badge=&category=` |
| Questions, ranked by meaning | `/?q=joining+collections` |
| Export of exactly that view | `/api/questions?status=&skill_badge=&category=` |
| Badge review, by state | `/admin/skill-badges?status=candidate` |
| One documentation source | `/admin/docs/source?source=…&q=` |
| One documentation page | `/admin/docs/page?url=…` |

Consequences that are deliberate: switching a status tab preserves the badge and
category filters; the export link is built from the same parameters as the screen, so
it cannot disagree with what is displayed; and changing a filter navigates rather than
mutating a hidden variable.

The rule for new screens: if a reader could want to send someone else *this*, it needs
to be in the address bar.

### 2. Server-rendered, with JavaScript only where it earns its place

Jinja2 templates and Bootstrap from a CDN, no build step, no framework. Every list is
rendered server-side so it is readable without JavaScript. JavaScript is used for four
things only: polling a background run, posting a review decision, navigating when a
filter changes, and rendering Markdown.

Tests therefore assert on **markup** — an element, a class, a `data-` attribute — never
on a bare word, because the templates ship JavaScript containing the same labels they
render. Where no stable hook exists, add a `data-` attribute rather than matching prose.

### 3. Long work runs in the background and is timed on the server

Anything that calls a model or crawls a site runs as a background task with its own run
state, and the page polls for status. Each job — question generation, badge sync,
duplicate sweep, documentation refresh — holds separate state, so one never reports or
blocks another.

Run state carries `started_at`, `finished_at`, and the endpoint returns `server_time`
alongside it. The page computes elapsed time from those, correcting for clock skew.
This is why the timer survives a reload or a trip to another screen: the browser is not
the thing that remembers when the run began.

### 4. Failures are reported, never swallowed

A background failure has nowhere to surface on its own. Every run captures the error
and its traceback into run state, and the screen shows both — an operator should not
have to read server logs to discover that a credential is missing.

Per-item failures are collected rather than raised: one unreachable page must not lose
the thousands that fetched cleanly. Long failure lists are capped for display while the
true count is reported, so a bad network does not put thousands of entries into memory
and onto a page.

The state that must never be silently produced is a **clean result that was not
actually checked** — "screening did not run" and "screened and found nothing" are
different, and the screen says which.

### 5. Never lose work that has already been paid for

By the time a follow-up step runs, the expensive part is done. So:

- a malformed question is discarded and reported, but never fails the batch it arrived in;
- if cross-badge attribution fails, the questions are still stored under the badges they
  were written for;
- if duplicate screening cannot run, the questions are still stored, unscreened, and the
  screen says so;
- if a documentation crawl stores nothing, the previous corpus is left intact rather than
  swept away.

### 6. Cheap and deterministic first, expensive and probabilistic second

Where a model or a paid service makes a judgement, something cheap narrows the field
first, and the narrowing is never allowed to make the decision:

- duplicate detection shortlists with `$vectorSearch` and decides with `$rerank`;
- the score floor exists to trim cost, not to classify — a pair below it is simply never
  put to the reranker;
- badge attribution runs after the format check, so a question about to be discarded is
  never catalogued;
- nothing calls out at all when there is nothing to compare.

### 7. Machine runs never overwrite human decisions

Review is the product of this tool, so a re-run must not undo it. Status is set on
insert only; a corrected badge title and curated links are locked against later syncs;
and when a merge or a duplicate sweep must choose a survivor, it prefers the record
carrying review work — approved over draft, curated over machine-written.

### 8. Model output is validated deterministically

Schemas are permissive on arrival and the rules are enforced afterwards, in code:
exactly four options, exactly one correct, no repeated or empty options, a non-empty
stem, badge slugs that name a real badge, positional answers bounds-checked against
what was sent. A schema strict enough to reject one bad question would fail the whole
batch and lose the good ones.

Refusals, truncated structured output and missing parsed results are errors — never
read as "the model found nothing", which is indistinguishable from a correct empty
answer.

### 9. Fetched content is data, never instructions or markup

Documentation pages, search results and log lines are content this program did not
write. They go into a prompt as clearly-labelled reference material, and into a page as
data: the Markdown viewer receives page text as JSON and sanitises it before it reaches
the document; the log viewer and every traceback are written with `textContent`. No
endpoint accepts a file path — the log viewer serves one known file and takes no path
parameter at all, because there are no authorizations here to fall back on.

### 10. Configuration from the environment; external contracts as named constants

Every setting resolves from the environment through one frozen `Settings` object, and
nothing that identifies infrastructure is defaulted in code that lives in a public
repository. Logging is deliberately configured *without* `Settings`, because `Settings`
requires a database connection string and "cannot reach Atlas" is exactly what someone
comes to the log to find out.

Anything named outside this repository is a constant, not a literal: the vector index
name and the `embedding_text` field path are referenced by an Atlas index definition
created by hand, so renaming either would silently stop the index matching anything
with no error raised here.

### 11. Storage shapes follow the way things are read

- Many-to-many is an array: `categories` and `skill_badges` on a question, so one
  question is findable under every badge it serves.
- Identity is explicit and stable — a badge is its slug, a question its generated
  `question_id`, a documentation page its URL. Nothing derives identity from content.
- Every field a screen filters on is indexed; identity fields are unique.
- Listings project away bulk (`image_data`, page `text`) so a screen render never
  carries megabytes it will not display.
- A refresh that replaces a collection stamps each document with its run and sweeps
  what the run did not touch, rather than emptying first — the same end state, without a
  window where the data is gone.
- `content_hash` distinguishes "unchanged" from "updated", which is what makes a
  re-crawl cheap and the reported counts meaningful.

### 12. Atlas does the embedding and the reranking

The vector index uses `autoEmbed`, and reranking uses the native `$rerank` stage in the
same aggregation. This program stores no vectors, needs no model API key for retrieval,
and makes no second round trip. The consequence to preserve: retrieval quality is a
property of the index definition and the pipeline, not of client code.

### 13. Tests are the recorded requirements

Every test carries an `Intent` / `Success` / `Feature` block, and those blocks are never
edited. When a requirement genuinely changes — as several have — the test is **replaced**
with a new block that records the new requirement and says what it supersedes, rather
than quietly relaxed to match the code.

The suite is hermetic: no network, no Atlas, no model API. Credentials are stripped by
an autouse fixture, MongoDB is an in-memory fake that implements real operator
semantics, the Anthropic client is a scripted double, and HTTP is a local stub server.
When a fake diverges from real behaviour it gets fixed — a fake that returns the wrong
shape lets a test pass while the code asks for fields it will never receive.

### 14. Two areas, separated by audience rather than permission

Authoring lives at the root; curation lives under `/admin`. There are no authorizations
anywhere in this program, and both are reachable by anyone who can reach the service.
The split exists so each screen has one audience, and it is enforced only by tests: the
questions screen is not served under `/admin`, and no questions endpoint is served under
`/api/admin`.

### 15. Destructive actions are reversible, confirmed, or both

Retiring is reversible and deleting is not, so a badge cannot be deleted until it is
retired. The duplicate sweep has a dry run, because it deletes with no human judgement
behind it. A sweep never removes both halves of a pair. Every irreversible button
confirms first, and says what cannot be undone.

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

**Generating: a badge is walked, not prompted.** A run is scoped to a skill badge,
and the badge is first resolved to the set of documentation pages it is *about* —
typically a few hundred. The run then walks that set one page at a time, asking for
a few questions per page, storing each page's questions as they are written.

This is the opposite direction from the obvious design. Cramming a badge's best
pages into one prompt and asking for a batch of questions caps the badge at
whatever fits in a single request — about fifteen pages — and asking the same badge
again re-reads the same fifteen, so the second batch is variations on the first.
Walking the badge's pages instead makes each page read exactly once, worth several
questions, and makes coverage a counter against an enumerable list rather than a
guess. A badge with 300 pages can support hundreds of genuinely distinct questions;
the same badge in one prompt cannot.

It also spreads the cost. Questions arrive per badge, when somebody asks for that
badge, rather than as one sweep of the whole corpus — so the first badge tells you
whether the output is any good before the other 33 are paid for.

**Drawing the page set.** The same per-topic semantic searches as before, but much
wider, because the job changed: not "the best few pages that fit in one prompt" but
"this badge's material". One search for the badge overall, then one per topic area
with the badge name attached — "indexes" matches most of the corpus while "Atlas
Search indexes" matches what the badge means by it.

Candidates are then filtered three ways, and the filters matter more than the
search:

- **A relevance floor.** Topic areas come from Credly's skill tags, which are
  marketing metadata rather than a syllabus. Measured on the live Cluster
  Reliability badge, the tag "Cluster IP" — a Kubernetes term, and a tagging
  artifact — pulled VPC peering and IP access lists into a reliability badge at
  scores of 0.64-0.69, while pages plainly about the badge scored 0.70-0.86. The
  floor sits in that gap.
- **Reference material is excluded.** Measured 2026-08-19, 3,318 of 7,162 stored
  pages sit under `reference`, `cli`, `api` or `command` paths. A question written
  from a parameter list tests whether a candidate can look up a flag, which is not
  a skill the badges certify.
- **Pages already written from are dropped.** This is what makes a walk resumable:
  the set is derived from the `source_urls` of the badge's existing questions, so
  running a badge again covers new material instead of re-mining the same pages.

The set comes back in relevance order, so a run that walks only part of it walks
the most relevant part.

**One page, one call.** Each page is authored in a single structured-output call —
not the draft-then-extract pair the badge-wide path uses. That pair earns its keep
for a research turn, where thinking in prose first helps; reading one page needs no
tools and no research, so a second pass would only pay output tokens again to
restate questions already written. Badge attribution is folded into the same call
for the same reason: the catalog is small, the model is already holding the
question, and a separate pass re-sends every question to decide something it could
have decided while writing.

Effort is tuned separately for page authoring (`page_author_effort`, default
`medium`) rather than inherited from the research path's `high`. Output tokens —
thinking most of all — dominate the cost of a walk, so this is the single largest
cost lever in the program.

**Nothing is lost to one bad page.** Questions are stored page by page, so a
failure on page eighteen keeps the questions from the first seventeen. A page that
refuses, truncates, or has vanished from the corpus is recorded with its reason and
stepped over — one bad page says nothing about the next. A walk can also be stopped,
and keeps what it has written.

Progress is reported per page: which page of how many, questions written so far, the
rate and the time left. A walk of 25 pages is far too long for a spinner.

**When a badge has no pages.** Two different situations, with two different answers.
A badge that has *never* been walked and resolves to nothing has no material in the
corpus — the run falls back to the older single-prompt path, which researches with
server-side web search, and says so on screen. A badge whose pages have *all* been
written from is exhausted: another run will not help, and the honest answer is to
say so rather than research around it. The fix there is a wider corpus or a lower
relevance floor, not another press of the button.

**Coverage.** Because a badge's questions come from its documentation, a badge with
little documentation gets few questions. The **Coverage** panel makes that a
workflow rather than a defect: every badge, thinnest first, with draft and approved
counts and how many pages it has left to walk. Few questions and many pages left
means run it again; few questions and no pages left means the material is spent.
Resolving every badge's page set is dozens of vector searches, so the panel is
fetched on demand rather than rendered with the screen.

**Questions must be independent, not merely distinct.** The bank is meant to be
large enough that leaking the answers to a full quiz does not compromise the
quizzes built from the rest of it — and that only holds if knowing one question's
answer does not give away another. Same concept in a different scenario is fine:
answering both requires the understanding the badge tests. The same question
reworded is not. The prompt states this, and the walk's structure helps — questions
written from different pages are unlikely to paraphrase one another.

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

**Duplicate detection — an ad-hoc sweep, no LLM, no API key.** **Find duplicates** on
the main screen compares the questions already stored. Generation runs do not
screen: an LLM judging every candidate pair was accurate but slow and expensive, and
the cost fell on authoring, the one step a person waits for. A duplicate costs
nothing until someone builds a quiz, so it is found on request instead.

One aggregation per question does both stages on the cluster:

```
$vectorSearch  index: questions_embedding_text_vector, path: embedding_text
$rerank        path: embedding_text, query: {text: …}, model: rerank-2.5
$project       score: {$meta: "score"}
```

`$vectorSearch` shortlists cheaply; `$rerank` re-scores each candidate with a
cross-encoder that reads both texts together — which is what separates "the same
question reworded" from "the same topic", something two independently embedded
vectors cannot do. Native reranking needs MongoDB **8.3+**; this cluster runs 8.3.8.

**The threshold is measured.** Against `rerank-2.5` on the live collection:

| pair | rerank score |
|---|---|
| genuinely distinct questions | 0.379 – 0.512 |
| deliberately reworded copy | 0.945 |
| identical text | 0.941 |

`question_rerank_delete_threshold` is **0.85**, inside that gap. Note the reranker
does not return 1.0 for identical text, so a threshold near 1.0 would never fire.
Pairs at or above it lose one question — approved beats draft, then more badges, then
older — and the rest are reported with their scores. **Dry run** reports what a sweep
would delete without deleting it; re-check that way as the collection grows to span
more badges.

Cost is bounded by `question_duplicate_neighbours` (5), since comparing every pair
grows as the square of the collection.

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
| `POST` | `/api/questions/generate` | Start a walk: `skill_badges`, `max_pages`, `questions_per_page` |
| `GET` | `/api/questions/generate/status` | Poll a run — reports the page it is on |
| `GET` | `/api/questions/coverage` | Per-badge question counts and pages left to walk |
| `GET` | `/api/questions` | List / export questions, same filters |
| `GET` | `/api/questions/search?q=&limit=` | Questions ranked by similarity to `q` |
| `POST` | `/api/questions/duplicates/sweep?dry_run=` | Find duplicates; delete the clear ones |
| `POST` | `/api/questions/backfill-embedding-text` | Compose `embedding_text` where missing or stale |
| `POST` | `/api/questions/{id}/status` | Approve, reject or re-open |
| `DELETE` | `/api/questions/{id}` | Delete a question |

### Documentation corpus

`/admin/docs` keeps a stored copy of MongoDB's documentation. Question authoring
reads its source material from here rather than fetching the web mid-run — which is
where most of a run's wall-clock time used to go, and why two runs on the same badge
saw different source text. See **Generating** above for how a run selects from it.

Pages come from MongoDB's published agent index (`llms.txt`), which names each page
and serves it as Markdown. That is the only enumerable route to the corpus: the MCP
server's `search-knowledge` answers a query with its best few chunks and cannot be
asked for everything, which makes it the right tool at authoring time and the wrong
one for building a cache.

**Refresh documentation** replaces the corpus with what MongoDB publishes now — about
**7,000 pages** (~72 MB), roughly twelve minutes.

**Fill gaps** fetches only pages the corpus does not already have, and removes nothing.
This is the recovery path: the docs are served through CloudFront, which starts answering
**403** when a crawl asks for too much too fast, and re-crawling seven thousand pages to
recover the few hundred that were refused wastes twelve minutes and invites another
block.

Being refused is handled rather than merely reported:

- A 403, 429 or 5xx is retried with a growing pause, and a `Retry-After` header wins over
  our own backoff — it is the server saying how long it wants to be left alone.
- After `docs_block_threshold` (25) consecutive refusals the crawl **stops**. Continuing
  would produce thousands of identical failures and prolong the block. What was fetched is
  kept, and the screen says to use **Fill gaps** later.
- A refused crawl never sweeps. Pages it never reached are not treated as withdrawn —
  otherwise a crawl blocked a third of the way through would delete two thirds of the
  corpus and report a successful replacement.

- Pages are stamped with the run that wrote them, and anything left from an earlier
  run is deleted at the end. Same end state as emptying the collection first, except a
  crawl that dies half way through leaves the previous corpus in place rather than
  nothing.
- If a crawl stores no pages at all — a moved index, no network — nothing is swept and
  the run says so. That is the one case where a blind replace would destroy the corpus
  and report success.
- A page is only rewritten when its content hash changed, so most of a re-run is
  cheap and `updated` means something.
- Per-page failures are collected, not fatal: across 10,000 requests to a site this
  program does not own, some will fail, and aborting would mean the crawl never
  finishes. The reported list is capped at 50 with a true count.
- Oversized pages (>2 MB) are skipped — generated API dumps, not prose.
- **Navigation stubs are skipped.** A page that is a title and a list of links is not
  something a question can be written from: `drivers/csharp-drivers.md` is 108 bytes and
  only points at the real C# driver docs. Anything under `docs_min_page_bytes` (500) is
  excluded, counted separately from failures, since skipping one is the intended
  outcome. The floor is deliberately conservative — stubs continue up to roughly 900
  bytes, but so do a few genuinely short pages, and dropping real content is the worse
  mistake. `POST /api/admin/docs/prune-stubs` removes ones stored before the floor
  existed, without waiting for a full crawl.
- **Progress is reported in detail.** The crawl reads every index first, so before
  fetching a single page it knows how many there are. The panel then shows the phase,
  a percentage, pages done of the total, the rate in pages per second, elapsed time and
  the time remaining at that rate, plus new/updated/unchanged/failed counts. During the
  planning phase no percentage or estimate is shown — the crawl does not know yet, and a
  confident "0%, 0s remaining" reads as a stalled run.
- The run is timed on the server, so leaving the screen and returning does not restart
  the timer.
- A page named by two indexes is fetched and counted once, so the total is a real
  denominator.

**Search the whole corpus.** `/admin/docs/search` searches every stored page by meaning,
ranked, with an excerpt around the match. Corpus-wide on purpose: which of the 74
sources holds a topic is not something an author knows — the C# driver's real
documentation is not under the `drivers` index, it is under its own, 81 pages of it —
so a per-source search would only work for someone who already knew where to look.

Semantic, not keyword: an author knows the topic and not the wording. "How do I model a
one-to-many relationship" has to reach the embedded-versus-referenced page, which never
uses that phrase — a keyword search returns nothing for it. The cost is that a
term-for-term match is no longer guaranteed to rank first, which is the right trade for
a corpus read for meaning rather than grepped.

It runs on the `doc_pages_text_vector` Atlas Vector Search index, configured with
autoEmbed on `text`. Atlas embeds both the stored pages and the query, so this program
stores no vectors and needs no embedding key — the same arrangement as the badge and
question indexes. The definition lives in Atlas, not here: `ensure_indexes` creates no
text index, and the index name is overridable with `DOC_PAGES_VECTOR_INDEX_NAME`.

**Reading what is stored.** The source table is not just an inventory — open a source
to list its pages, then open a page to read it. Long sources are capped at 500 rows
with a filter over titles and URLs, and the screen says how many of the total it is
showing rather than quietly truncating.

A page is rendered as Markdown (`marked`, sanitised through `DOMPurify`, both pinned
by SRI), with a **Markdown** toggle for the stored source text and a link to the page
on MongoDB's own site — the corpus can be stale, and comparing against what is
published now is the point of that link. The page text is passed into the template as
JSON and sanitised before it reaches the document: it is fetched content, so nothing
in it gets to be parsed as part of this application.

Nothing schedules this; it is refreshed on demand. `DOCS_INDEX_URL` overrides the
index location.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/docs` | The corpus screen |
| `GET` | `/admin/docs/search?q=` | Keyword search across every stored page |
| `GET` | `/api/admin/docs/search?q=&limit=` | The same search, as JSON |
| `POST` | `/api/admin/docs/prune-stubs` | Delete navigation stubs already stored |
| `GET` | `/admin/docs/source?source=&q=` | Pages in one source, filterable |
| `GET` | `/admin/docs/page?url=` | One page, rendered as Markdown |
| `POST` | `/api/admin/docs/refresh?mode=replace` | Replace the corpus with a fresh crawl |
| `POST` | `/api/admin/docs/refresh?mode=fill` | Fetch only missing pages; remove nothing |
| `GET` | `/api/admin/docs/refresh/status` | Poll a crawl, with progress |
| `GET` | `/api/admin/docs/sources` | Stored sources, upstream sources, totals |
| `GET` | `/api/admin/docs/pages?source=` | Stored pages, without their text |
| `GET` | `/api/admin/docs/page?url=` | One page, with its text |

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
app/services/doc_retrieval.py        resolves a badge to the pages it is about
app/services/question_duplicates.py  the ad-hoc duplicate sweep ($vectorSearch + $rerank)
app/services/discover_cli.py         shell entry point
app/repositories/skill_badges.py     upsert / list / status, indexes
app/repositories/questions.py        insert / filter / status, indexes
app/repositories/doc_pages.py        the stored documentation corpus
app/routers/questions.py             question JSON endpoints under /api/questions
app/routers/pages.py                 the questions screen, served at /
app/routers/admin_skill_badges.py    badge JSON endpoints under /api/admin
app/routers/admin_pages.py           server-rendered pages under /admin
app/routers/admin_logs.py            log tail endpoint under /api/admin
app/routers/admin_docs.py            documentation corpus endpoints
app/services/doc_corpus.py           crawls the published docs index
app/templates/base.html              nav shell shared by every screen
app/templates/questions.html         the main screen: review and generate
app/templates/admin/skill_badges.html  badge review screen
app/templates/admin/logs.html        log viewer
app/templates/admin/docs.html        documentation corpus screen
app/templates/admin/docs_search.html corpus-wide semantic search
app/templates/admin/docs_source.html pages in one source
app/templates/admin/docs_page.html   Markdown viewer for one page
tests/                               pytest suite + fakes (see tests/README.md)
```

## Known limitations

- A walk runs one page at a time, in series, and only one run at a time — run state
  is a single in-process dict. Walking 34 badges means supervising it. The Message
  Batches API is the natural fit (independent requests, nobody waiting, half price);
  whether the Grove gateway exposes it is unprobed.

- The relevance floor and the reference exclusion are the only screening on a page
  set. A cheap per-candidate classification pass would judge relevance better than a
  similarity score can, and the page set should be reviewable on screen before a run
  spends anything against it.

- `page_author_effort` is set to `medium` on reasoning, not measurement. Output
  tokens dominate a walk's cost and thinking dominates output, so this is the
  program's largest untested cost assumption; twenty pages at each effort level would
  settle it.

- Retrieval selects whole pages, not passages. A page relevant in one paragraph
  spends its whole share of the context budget, so the material given to an
  authoring turn is broader and blunter than chunk-level retrieval would be.

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
