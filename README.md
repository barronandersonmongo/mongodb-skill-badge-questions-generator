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

**Atlas Search indexes are not created by this program** — their definitions live in
Atlas. Three are needed: `skill-badge-description-vector` (autoEmbed on
`description`), `questions_embedding_text_vector` (on `embedding_text`), and
`doc_chunks_embed_text_vector` (on `embed_text` in `doc_chunks`). Until the last of
those exists, documentation retrieval resolves nothing and every badge falls back to
researching the web.

Token prices (`cost_input_per_mtok`, `cost_output_per_mtok`,
`cost_cache_read_per_mtok`, `cost_cache_write_per_mtok`) and page-walk tuning live in
`app/config.py` rather than the environment, since each
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
| Questions, filtered | `/?skill_badge=&category=` |
| Questions, ranked by meaning | `/?q=joining+collections` |
| Export of exactly that view | `/api/questions?skill_badge=&category=` |
| Badge review, by state | `/admin/skill-badges?status=candidate` |
| One documentation source | `/admin/docs/source?source=…&q=` |
| One documentation page | `/admin/docs/page?url=…` |
| The live page behind a citation | `/admin/docs/render?url=…` |

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
carrying review work — curated over machine-written.

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
retired. The duplicate sweep deletes nothing at all — it reports, and deleting is a
separate act on a list somebody has read. Deleting a question, singly or as a batch,
requires typing the word. Every irreversible button confirms first, and says what
cannot be undone.

A control that is "the same thing but safe" is a smell: it makes the operator choose a
mode before they have the information to choose with, and the safe one is strictly more
informative. Where that pattern appeared — a dry run beside a real sweep — the
destructive mode was removed rather than kept as an option.

## Features

### Two areas

| Area | What it is for |
|---|---|
| `/` | **Authoring** — write, browse, filter and export questions |
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

**Questions are written from sections, not pages.** The corpus stores whole pages, and
a page turned out to be the wrong unit twice over. Sent whole, one 1.7 MB page —
a driver tutorial repeating every example in a dozen languages — cost **$2.58 for three
questions**. Capped, everything past the cap became unreachable, so a page's later
material could never produce a question however many times a badge was walked.

So each page is split into **sections**, and a section is what gets embedded, retrieved
and written from. Retrieval sharpens as a side effect: a section about `$search` stops
being buried inside a page about aggregation.

**The band was measured, not guessed.** On 2026-08-19, over the 3,844 non-reference
pages:

| split | sections | median | under 500 |
|---|---:|---:|---:|
| H1–H2 | 26,125 | 642 | 44% |
| H1–H3 | 40,561 | 545 | 47% |
| H1–H4 | 43,589 | 528 | 48% |

Sections are mostly *small*, so **merging matters more than splitting** — a naive
split-on-headings corpus would be mostly heading stubs, which embed badly and support no
question at all. Packing neighbours to a floor and a ceiling of 1,500/8,000 gives
**18,421 chunks**: median 2,133 characters (~530 tokens), p90 7,603, nothing over the
ceiling, 3.8% under 500. Enough material to write several distinct questions from, small
enough that no single call is expensive.

**What it actually produced**, on the live corpus on 2026-08-19: 7,158 pages became
**27,399 sections** — median 2,026 characters, p90 6,683, nothing over the ceiling, 5.0%
under 500. Of those, 8,965 (33%) are under reference paths and excluded from walks,
leaving **18,434 sections** to write from, which is within a few of the 18,421 the
simulation predicted from the non-reference pages.

Three passes, in order: cut at headings to `chunk_heading_depth` (3); cut anything still
over the ceiling on blank lines, and bluntly if a single paragraph is still too big —
needed because a handful of sections run to hundreds of kilobytes where the heading
structure gives out inside one giant code block; then pack neighbours until each chunk
clears the floor. When a small section is absorbed, the earlier, broader heading stays
the chunk's own.

**Every chunk says where it came from and what it is about**: its page and page title,
the heading it sits under and the full heading path above it, its source index, its
position in the page, its size, and a content hash. The heading path leads the text that
gets embedded — "Limitations" means nothing on its own and everything under "Atlas Vector
Search > Filtering > Limitations".

**Chunks are derived, not crawled.** They live in their own collection, rebuilt from
stored pages by **Re-chunk** on the corpus screen. The band is a judgement that will want
re-tuning against real question quality, and trying a different one should cost seconds
rather than an hour-long crawl and another round of CloudFront refusals. Chunk ids key
on the page URL and position, so a rebuild of an unchanged page produces the same ids and
questions written from it stay attributable. Chunks are stamped with the refresh that
wrote them and swept the same way pages are — a chunk outliving its page is invisible and
harmful, since retrieval keeps offering it and a question written from it cites a URL that
now 404s.

**This needs a new Atlas index.** Retrieval runs on `doc_chunks_embed_text_vector`, over
`embed_text`, with autoEmbed. It must be created in Atlas before any of this works; the
old `doc_pages_text_vector` is now used only by the single-prompt fallback.

**Drawing the chunk set.** The same per-topic semantic searches as before, but over
sections and much wider, because the job changed: not "the best few pages that fit in one
prompt" but "the sections that make up this badge's material". One search for the badge overall, then one per topic area
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
- **Sections already written from are dropped.** This is what makes a walk resumable:
  the set is derived from the `source_chunk_ids` of the badge's existing questions.
  Section-level rather than page-level, because excluding a whole page would mean one
  question written from a page's opening made the rest of that page unreachable
  forever — the reverse of what chunking is for.

The set comes back in relevance order, so a run that walks only part of it walks
the most relevant part.

**No page may crowd out the others.** Sections are then taken in rounds — the best
section from every page, then the second best — because relevance order alone is not
enough when one page scores well throughout. Measured on the live corpus: the 25 sections
a Vector Search Fundamentals run walked came from **six pages**, since 85 of that badge's
252 sections were hard-split slices of one 1.7 MB page, the same code sample repeated in a
dozen languages under one heading. Twenty of those 25 produced no question at all, while
`mongodb-overview`, spread across 24 pages, produced 72 from the same 25-section budget.
With the rounds, that badge's first 25 sections come from 25 distinct pages.

This is the same fairness the old page-level retrieval applied across topic queries. It
was not carried over when the unit became a section, and the run above is what that cost.
Sections held back by the per-page limit are appended rather than dropped: the limit
reorders the set, it does not shrink it, or a badge whose material is genuinely
concentrated would report itself exhausted with sections still unused.

**A page's contribution is capped.** The corpus holds documentation pages up to
**1.7 MB** — driver tutorials that repeat every example in a dozen languages — and one of
those sent whole is about half a million input tokens. Measured on a real run on
2026-08-19: 505,435 input tokens, **$2.58 for three questions**, roughly a hundred times
the expected cost per question. `doc_context_page_chars` (24,000) now bounds what any one
page contributes to a prompt, and the model is told the page was cut and not to assume
what the rest says. The run records which pages were trimmed, so a thin result from a
truncated page is not mistaken for thin documentation.

This is also the argument for chunking the corpus rather than storing whole pages: with
a cap, a 1.7 MB page contributes only its opening, and whatever is useful further down is
unreachable.

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

**The status panel — one window, not two.** The same shape as the documentation
refresh, because a walk is the same kind of job: a phase, a progress bar, and the
numbers behind it. The run's message sits inside the panel and the panel takes the
colour it would have carried, rather than sitting in a separate alert above it — two
windows for one run meant two elapsed times, and the one on the panel was stale
between polls. Elapsed now ticks in the browser against the server's start time, so it
advances every second instead of jumping when a poll answers; the server's figure is
still used for a run the browser never watched, since a page opened afterwards has no
start time to count from.

The panel carries: pages done of how many, questions written, pages per
minute, actual questions per page, elapsed, time remaining, and the name of the page
currently being read — that last one is what lets an author notice a walk spending its
budget on material that does not belong to the badge.

The bar shows no percentage while the badge is still being resolved to its page set.
The walk genuinely does not know how much work there is yet, and inventing a number
would be worse than admitting it — so the phase says what is happening instead.

**Throughput and unit cost.** Pages per minute describes the machinery; **questions per
minute** describes the output, and it is what an author plans a session against — "34
badges at this rate" is only answerable from it. **Cost per question** is the other
derived figure, and the one that makes runs comparable: total spend depends on how many
pages were walked, so it says nothing about whether a run went well, while cost per
question says whether a prompt or effort change paid for itself. During a run the
running average is also the best available projection of what the rest will cost. Both
are absent rather than zero until there is something to divide by, since a rate of zero
reads as a fact about a slow run rather than as "nothing has finished yet".

The history's figures are derived from the totals rather than averaged over runs: a
thirty-second run must not count for as much as an hour-long one.

**Cost is reported, not estimated.** Every response carries its token counts, so the
panel adds up exactly what the run consumed and prices it: spend so far, and the
projected total at the rate it is going. The projection is what makes stopping an
informed decision rather than a guess — "$0.31 spent, about $2.60 by the end" is
actionable in a way that a spinner is not. Nothing is projected until at least one
page has finished, because a projection from zero reads as "this run is free" at
exactly the wrong moment.

Prices live in `Settings` next to the model they apply to (`cost_input_per_mtok` and
friends, Claude Opus 5 list prices as published 2026-08-19, with cached reads at a
tenth and writes at 1.25x). Since the token counts are measured, the published price
is the only thing here that can be wrong — update it alongside `model`. The finished
run reports its cost too, because cost per badge is how an author decides whether the
other 33 are worth it.

**Stopping.** A **Stop after this page** button appears on the panel while a walk
runs. It is not a cancellation: the page in flight is already paid for, so it finishes
and its questions are kept, and everything after it is skipped. That is the point —
the reason to stop a walk is to stop it spending, not to undo it. A run stopped this
way is labelled as stopped rather than done, so it cannot be mistaken for a badge that
ran out of material, and the pages it did not reach are still there for the next run.

**Skill level.** A run can be pitched at one level, or left mixed. The scale is the one
every question already carries — `foundational`, `intermediate`, `advanced` — so asking
for a level and filtering for it later use the same vocabulary.

Each level is *described* in the prompt rather than merely named, because "advanced" on
its own is read as harder wording rather than harder judgement, and that produces obscure
trivia instead of questions a senior engineer finds worth answering: foundational is
someone a few weeks in, tested on what a feature does and when to reach for it;
intermediate ships MongoDB in production, choosing between reasonable approaches and
reading diagnostic output; advanced owns the deployment, and is tested on failure modes,
interactions between features, and the reasoning behind a recommendation rather than the
recommendation itself. A version number nobody remembers is not an advanced question.

**Mixed** is an instruction, not the absence of one: left silent the model pitches a whole
page at one level of its own choosing, so a mixed run explicitly asks for a spread. The
level requested is recorded with the run, since comparing two runs on a badge is
meaningless without knowing one asked for foundational and the other for advanced.

**When a badge has no pages.** Two different situations, with two different answers.
A badge that has *never* been walked and resolves to nothing has no material in the
corpus — the run falls back to the older single-prompt path, which researches with
server-side web search, and says so on screen. A badge whose pages have *all* been
written from is exhausted: another run will not help, and the honest answer is to
say so rather than research around it. The fix there is a wider corpus or a lower
relevance floor, not another press of the button.

**Run history.** Run state is a single in-process dict: enough to drive a screen while
a run is going, and gone the moment the server restarts. The token counts and the wall
clock are unrecoverable after the fact, so every finished run is written to
`generation_runs` — which badge, the choices the author made (page cap,
questions-per-page, instructions), the model, the effort, the relevance floor, how long
it took, what it produced, what it cost, which pages it read, and anything that failed.
Failed runs are recorded too: "we tried this badge and it broke" is exactly the thing
that gets forgotten and retried.

Kept in its own collection because a run is an event and a question is an artefact, with
different lifetimes: deleting a bad batch of questions must not erase the record that
the batch was generated, since that record is the evidence for why the prompt was
changed afterwards. Questions carry the id of the run that wrote them, so a question
leads back to its run and a run to its questions.

The **Run history** panel lists runs newest first with the cumulative totals across all
of them. The cumulative figure is the one that matters: per-run cost is small enough to
ignore individually and large enough to matter in aggregate, which is exactly the shape
of spending that goes unnoticed.

The run summary on the screen is **dismissible**. Rendered from run state it otherwise
sits there permanently until somebody starts another run, and dismissing loses nothing
now that the run is recorded.

**Coverage.** Because a badge's questions come from its documentation, a badge with
little documentation gets few questions. The **Coverage** panel makes that a
workflow rather than a defect: every badge, thinnest first, with its question count
and how many pages it has left to walk. Few questions and many pages left
means run it again; few questions and no pages left means the material is spent.
Resolving every badge's page set is dozens of vector searches, so the panel is
fetched on demand rather than rendered with the screen.

**What kinds of question get asked.** Left to itself the model writes everything as a
scenario — every question opens by painting a situation, which is exhausting to read
and tests one narrow skill. So the prompt names the forms and says what each is for:
**situational** (judgement, failure modes, debugging), **factual** (behaviour, limits,
defaults — asked straight, with no scene-setting), **procedural** (the correct order of
operations, where getting the order wrong breaks it), **best practice** (what you should
do and why that rather than the alternative), **diagnostic** (given this output or
error, what does it mean) and **comparative** (when to use one thing over a similar
thing, which is where the real confusions live).

The material chooses the form, not a quota: a page describing a sequence should yield
procedural questions, and one defining behaviour should yield factual and diagnostic
ones. Best practices in particular are asked plainly — a practitioner recognises "which
order should this compound index use" faster stated directly than buried in a story
about a slow query. A direct question is not a lesser question.

**How the questions read.** The audience is working software developers, and a
question phrased like a technical writer's abstract announces that nobody who does the
job wrote it. "Write naturally" does not fix that on its own, because the
machine-written register comes from a specific and recognisable vocabulary — so the
prompt names it and bans it: no *leverage*, *utilize*, *robust*, *seamless*,
*crucial*, *delve*, *harness*, *streamline*, *comprehensive*; no "It is important to
note that"; no "Which of the following best describes…" or "All of the above".

What replaces it is specificity. A question puts the candidate in a real situation in
the second person — "your replica set has one node lagging 40 seconds behind the
primary" — and names actual stage names, commands, flags, field names and error
strings. That is not only a style rule: a question that says "the appropriate
configuration" instead of the actual flag is a question that tests nothing. Short
sentences, active voice, contractions allowed, no stacked rhetorical triads, no
hedging, options kept grammatically parallel and roughly equal in length so length is
not a clue. The same voice applies to the option rationales, which is where textbook
prose otherwise creeps back in.

Grammar and spelling are held to normal standards throughout — the goal is a
developer's vocabulary and sentence shapes, not informality for its own sake.

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

**Following a citation.** Each question links the documentation page it was written
from. The visible text is the canonical `mongodb.com` URL, because that is what
identifies the page — but the link goes through `/admin/docs/render`, which fetches that
page from MongoDB now and renders it with the same Markdown viewer the stored copy uses.
MongoDB serves these pages as raw Markdown, so following the URL directly lands on
unformatted text; a citation nobody wants to read is a citation nobody checks.

Live rather than stored, deliberately: the stored copy is the snapshot the question was
written from and the live page is what MongoDB publishes today, and after a docs refresh
the two can differ. The view says which one you are looking at and offers the stored copy
alongside, so a divergence reads as a divergence rather than as a wrong question. A page
the corpus no longer holds still renders, and says no question came from it.

**That route is host-pinned.** It fetches a caller-supplied URL server-side, which is a
server-side request forgery hole unless the host is fixed — an arbitrary URL would reach
anything the server can reach, including a cloud metadata endpoint, and hand the response
back. Only `https` pages on `docs_domain` (`www.mongodb.com`) are fetched, checked on the
parsed hostname rather than by prefix, since `https://www.mongodb.com.evil.example/`
starts with the right string and is not the right host. The check is enforced in the
fetcher as well as the route, so a future caller cannot bypass it by forgetting.

**Questions show when they were written**, from the `created_at` they have always
carried. The list is newest first, but without a time an author cannot tell which
questions came out of the run they just watched, or — once a prompt has changed — which
side of that change a question is from. That comparison is the point of keeping run
history, and it needs to be visible on the question itself.

**New questions appear as they are stored.** A walk stores section by section over many
minutes, so a list left alone goes stale while its reader watches it being filled — the
questions exist in the database and not on the screen. While a run is going the screen
polls `/api/questions/count` and reloads when it grows. Reloaded rather than patched in
place, because the question card is a Jinja template and rebuilding it in JavaScript would
be a second copy of it to keep in step. The count is scoped to the filters on screen, so a
badge-filtered list does not reload every time some other badge gains a question, and the
reload is skipped while a dialog is open or the tab is hidden — pulling the page out from
under someone reading a delete confirmation is worse than a slightly stale list.

**Reading a question's tags.** Three kinds of tag sit together on each question, so they
are told apart by colour as well as by position: a solid chip for the difficulty, gold
for the skill badges the question is filed under — the badge artwork is gold, so the
association is already made — and green for the topic areas it exercises. Each also says its kind in its title text, because colour alone is not a
label — it fails for anyone who cannot separate the two hues, and it fails in a
screenshot.

Badge tags are links to that badge's row on `/admin/skill-badges`. A slug is not
self-explanatory — `secure-mongodb-self-managed-authn-authz` does not say what it covers
— and checking should be one click rather than a hunt through a 34-row table for a row
whose name differs from its slug. Topic areas are deliberately not links: they are
free-text labels with no definition anywhere, and linking one would promise a page that
does not exist.

**No review workflow.** A question that passes the format check is stored and
usable — there is no draft state, no approve step and no reject step. At thousands of
questions nobody works a queue of drafts, so the gate was a bottleneck rather than a
safeguard, and a question nobody had blessed was indistinguishable from one nobody
wanted. That leaves one list, one count, and no tabs.

The screen still shows everything needed to judge a question: the stem, all four
options with the correct one marked, each option's rationale, the explanation, the
difficulty, the badges and categories, and the source pages.

**Deletion is the only editorial act, and it is guarded.** Nothing can re-create a
question, and the button now sits next to no other control, so a misplaced click has
nowhere else to land. Deleting opens a dialog that shows the question again and
requires the word *delete* to be typed before the confirming button enables. Two
deliberate acts, because the first one is one click away from a list of thousands.

Questions written before the workflow was dropped still carry a `status` field, which
would ride along in the JSON export and tell whoever consumes it that a question is an
unfinished draft. `POST /api/questions/drop-status` strips it, and is safe to re-run.

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

**Finding never deletes.** Pairs at or above the threshold are *flagged*, with the
question this program would drop and the one it would keep both named — more badges
beats fewer, then older beats newer — and the pairs below it are listed too, so the
threshold stays visible as a judgement rather than a fact. Nothing is removed until
someone unticks the pairs they consider genuinely different questions and confirms the
rest by typing the word.

That demotes the threshold from deciding which questions die to shortlisting the ones
worth looking at, which is the right weight for a number measured on six questions. It
also removes the old dry-run button: two controls where one was the same thing but
irreversible made the operator pick a mode before seeing the collection, and since
reporting is strictly more informative, nobody should ever have pressed the other
first.

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
| `POST` | `/api/questions/generate` | Start a walk: `skill_badges`, `max_pages`, `questions_per_page`, `difficulty` |
| `GET` | `/api/questions/generate/status` | Poll a run — phase, pages, cost, page in flight |
| `POST` | `/api/questions/generate/stop` | Stop the walk after the page it is on |
| `GET` | `/api/questions/coverage` | Per-badge question counts and pages left to walk |
| `GET` | `/api/questions` | List / export questions, same filters |
| `GET` | `/api/questions/search?q=&limit=` | Questions ranked by similarity to `q` |
| `GET` | `/api/questions/runs` | Recorded runs, newest first, with cumulative totals |
| `GET` | `/api/questions/runs/{run_id}` | One recorded run in full |
| `POST` | `/api/questions/generate/dismiss` | Clear the last run's notice from the screen |
| `POST` | `/api/questions/duplicates/sweep` | Find duplicate candidates; deletes nothing |
| `POST` | `/api/questions/duplicates/delete` | Delete questions chosen from that report |
| `POST` | `/api/questions/backfill-embedding-text` | Compose `embedding_text` where missing or stale |
| `POST` | `/api/questions/drop-status` | Strip the legacy review field from stored questions |
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

**Refresh documentation** replaces the corpus with what MongoDB publishes now, and
chunks each batch as it lands — one action, not two. Measured on 2026-08-19:
**7,158 pages** (~75 MB) becoming **27,399 sections**, in **71 minutes**.

That is far longer than fetching 7,000 pages should take, and most of it is spent
waiting rather than transferring: the docs are behind CloudFront, which refuses a crawl
that asks for too much, and each refusal costs a growing back-off. It has not been
optimised because it does not need to be — the corpus is expected to be refreshed about
twice a year, so an hour is cheap and being politely slow is what keeps the crawl from
being blocked outright.

**Fill gaps** fetches only pages the corpus does not already have, and removes nothing.
This is the recovery path: the docs are served through CloudFront, which starts answering
**403** when a crawl asks for too much too fast, and re-crawling seven thousand pages to
recover the few hundred that were refused wastes an hour and invites another block.

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
| `GET` | `/admin/docs/page?url=` | One stored page, rendered as Markdown |
| `GET` | `/admin/docs/render?url=` | The canonical page, fetched live and rendered |
| `POST` | `/api/admin/docs/rechunk` | Re-split stored pages into sections; fetches nothing |
| `GET` | `/api/admin/docs/chunks` | How the corpus is currently chunked |
| `GET` | `/api/admin/docs/chunks/page?url=` | One page's sections, in order |
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
app/services/doc_chunking.py         splits a page into the sections questions come from
app/repositories/doc_chunks.py       chunk storage, search, totals
app/services/doc_retrieval.py        resolves a badge to the sections it is about
app/services/run_cost.py             prices a run from the tokens it reported
app/services/question_duplicates.py  the ad-hoc duplicate sweep ($vectorSearch + $rerank)
app/services/discover_cli.py         shell entry point
app/repositories/runs.py             run history: record, list, totals
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
