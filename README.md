# mongodb-skill-badge-questions-generator

Internal authoring tool for the MongoDB teams who build Credly skill badge quizzes. It
generates, validates, stores, filters and exports quiz questions. It is **not** a
quiz-taking platform — no learners, no scoring, no attempts.

## The problem, and why it is not "call an LLM in a loop"

Volume is easy. Quality is the entire job, and three constraints make it hard.

**A question has to discriminate.** Four options, exactly one defensibly correct, and
three distractors that a competent practitioner might actually pick. A model asked for
multiple-choice questions will happily produce one right answer and three obviously wrong
ones, which tests nothing.

**The bank has to be large enough that leaking a quiz does not compromise it.** The target
is thousands of questions across 34 badges (and growing). That only holds if the questions
are genuinely independent: knowing a leaked question's answer must not let someone answer
another one *without knowing the material*. Same concept in a different scenario is fine —
answering both needs the understanding the badge certifies. The same question reworded is
fatal. So deduplication is a core requirement, not housekeeping.

**Questions must be grounded and checkable.** A question written from the model's memory
of MongoDB cannot be verified without redoing the research, so every question cites the
documentation it came from, and the run that produced it is recorded.

Everything below follows from those three.

## Stack

Python 3.14 (stdlib `venv` + `pip`), FastAPI + Jinja2 server-rendered HTML, MongoDB Atlas,
Claude via the `anthropic` SDK, Bootstrap 5 / vanilla JS from CDN. No Node, no bundler, no
Docker, no task queue.

## Getting started

### 1. Build

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # requirements.txt for runtime only
```

### 2. Configure

```bash
export PTM_HACKATHON_CONNECTION_STRING='mongodb+srv://...'   # Atlas: PTM-Hackathon cluster

# Claude access — either a direct key:
export ANTHROPIC_API_KEY='sk-ant-...'                        # or run `ant auth login`
# …or an internal gateway fronting the Anthropic Messages API:
export GROVE_PRIMARY_KEY='...'                               # secondary key used as fallback
export GROVE_ANTHROPIC_BASE_URL='https://.../anthropic'      # part before /v1/messages
```

`~/.profile` is read only by login shells, so a variable exported there is invisible to
cron, systemd, or anything started as a service — source it explicitly when starting the
server that way.

Storage is the `skill-badge-questions` database on the `PTM-Hackathon` cluster.

Optional overrides: `ANTHROPIC_MODEL`, `WEB_SEARCH_TOOL_TYPE`, `WEB_FETCH_TOOL_TYPE`,
`SKILL_BADGE_CATALOG_URL`, `CREDLY_COLLECTION_URL`, `VECTOR_INDEX_NAME`,
`QUESTIONS_VECTOR_INDEX_NAME`, `DOC_PAGES_VECTOR_INDEX_NAME`, `DOCS_INDEX_URL`, `LOG_DIR`,
`LOG_LEVEL`.

Everything else lives in `app/config.py` rather than the environment, because each value
is a measured judgement with the measurement written beside it — token prices, the
relevance floor, the chunk band, the effort level. Read the comments there before changing
one.

### 3. Create the Atlas Search indexes

**This program does not create them**, and Atlas Search index definitions cannot be
created through `create_index`. Three are needed, all using **autoEmbed** so the cluster
embeds both the stored text and the query — this program stores no vectors and needs no
embedding key:

| Index | Collection | Path |
|---|---|---|
| `doc_chunks_embed_text_vector` | `doc_chunks` | `embed_text` |
| `questions_embedding_text_vector` | `questions` | `embedding_text` |
| `skill-badge-description-vector` | `skill_badges` | `description` |

Until the first exists, documentation retrieval resolves nothing and every badge silently
falls back to researching the web. Note the ordering problem on a fresh cluster: the
collection must have documents before Atlas's index wizard can inspect the field shape, so
**refresh the corpus first, then build the index** — the crawl creates `doc_chunks` and
fills it as it goes, and Atlas will index the rest as it arrives.

### 4. Run

```bash
.venv/bin/uvicorn app.main:app --reload
```

Open **<http://127.0.0.1:8000/>** — the questions screen. The badge catalog is under
`/admin`; generated API docs are at `/docs`. If MongoDB is unreachable the screen still
loads and names the problem instead of returning a stack trace.

### 5. First run, in order

1. **`/admin/skill-badges`** → sync the badge catalog, if it is empty.
2. **`/admin/docs`** → **Refresh documentation**. ~7,200 pages become ~27,000 sections in
   about **71 minutes** (see [The corpus](#the-corpus) for why it is slow and why that is
   fine).
3. Create `doc_chunks_embed_text_vector` in Atlas and wait for it to build.
4. **`/`** → **Generate questions**: pick a badge, how many sections to walk, how many
   questions per section, and a skill level.

## Tests

```bash
.venv/bin/python -m pytest                                    # ~2,460 tests, ~9 seconds
.venv/bin/python -m coverage run -m pytest && .venv/bin/python -m coverage report
.venv/bin/python -m pytest tests/test_doc_chunking.py -q      # one file
.venv/bin/python -m pytest -k "chunk and not repository"      # by name
```

**Tests never touch Atlas or the Claude API.** An autouse fixture strips real credentials
from the environment, MongoDB is an in-memory fake (`tests/fakes.py`), and the Anthropic
client is a scripted double. That is why the suite runs in seconds and why it can be run
without any configuration at all.

**Every test carries an `Intent` / `Success` / `Feature` block** recording why it exists,
what passing proves, and which feature it protects. Those blocks are the recorded
requirement and are **never edited**. To change behaviour, change the program; if a
requirement genuinely changes, delete the old test and write a new one whose block says
what changed and why. `tests/test_test_documentation.py` enforces the convention.

This matters more than it sounds. Several times in this codebase a test looked wrong and
was actually recording a decision whose reason had been forgotten — the block is where the
reason lives. See **[tests/README.md](tests/README.md)** for the suite layout.

> `mongomock` is deliberately not used: as of mongomock 4.3.0 / pymongo 4.17.0 its
> `bulk_write` path breaks on pymongo's newer `add_update()` signature. `tests/fakes.py`
> implements only the operations this program actually issues, and raises
> `NotImplementedError` for anything else rather than quietly returning nothing.

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

Consequences that are deliberate: the export link is built from the same parameters as
the screen, so it cannot disagree with what is displayed; the count polled during a run is
scoped to the same parameters, so a filtered view does not reload for questions it is not
showing; and changing a filter navigates rather than mutating a hidden variable.

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

The look is not decided per template. Every screen is built from the macros in
`app/templates/_ui.html` — `page_header`, `filter_bar`, `field`, `panel`, `empty_state`
and the tag macros — and every colour, space, radius, shadow and type size comes from
`app/static/theme.css`. Bootstrap is still loaded, because the modals and dropdowns use
its JavaScript, but almost none of its appearance survives: the theme is loaded after it
and replaces its components, keeping the class names the scripts and tests hook onto.
Surfaces are separated by elevation and whitespace rather than by hairline borders, and
MongoDB forest green appears only where something is the primary action or the current
screen. Colour means one thing each — forest for a primary action and for a skill badge,
spring green for focus, red for destructive, a warm neutral for a topic area, and nothing else is a chip
at all. The same green, at its palest, is the ground under the correct option in a question,
so "this is the right answer" and "this succeeded" are one colour rather than two; grey is
the ground under a question's identifier, date and source, because none of that is the
question.

### 3. One shell, with authoring separated from curation

The screens are not a flat list of peers. Writing questions is the job; the badge catalog,
the corpus and the logs are curation you visit when something about the material is wrong.
So `base.html` renders one app shell — a persistent sidebar that does not scroll with the
work, and a sticky header per screen — with **Questions** and **Duplicates**
at the top — both act on the questions themselves — and the three curation screens below a
rule under an **Admin** heading. Nothing enforces that boundary,
because there are no authorizations and both areas are reachable by anyone who can reach
the service; the layout is the only thing that tells an author which side of it they are
on, which is why it is drawn in the layout rather than repeated as a label on each link.

`tests/test_theme.py` holds the screens to all of this, including tests that fail if a
template hand-rolls a page header instead of calling the macro, or if the questions link
drifts into the admin section.

### 4. Long work runs in the background and is timed on the server

Anything that calls a model or crawls a site runs as a background task with its own run
state, and the page polls for status. Each job — question generation, badge sync,
duplicate sweep, documentation refresh — holds separate state, so one never reports or
blocks another.

Run state carries `started_at`, `finished_at`, and the endpoint returns `server_time`
alongside it. The page computes elapsed time from those, correcting for clock skew.
This is why the timer survives a reload or a trip to another screen: the browser is not
the thing that remembers when the run began.

### 5. Failures are reported, never swallowed

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

### 6. Never lose work that has already been paid for

By the time a follow-up step runs, the expensive part is done. So:

- a malformed question is discarded and reported, but never fails the batch it arrived in;
- if cross-badge attribution fails, the questions are still stored under the badges they
  were written for;
- if duplicate screening cannot run, the questions are still stored, unscreened, and the
  screen says so;
- if a documentation crawl stores nothing, the previous corpus is left intact rather than
  swept away.

### 7. Cheap and deterministic first, expensive and probabilistic second

Where a model or a paid service makes a judgement, something cheap narrows the field
first, and the narrowing is never allowed to make the decision:

- duplicate detection shortlists with `$vectorSearch` and decides with `$rerank`;
- the score floor exists to trim cost, not to classify — a pair below it is simply never
  put to the reranker;
- badge attribution runs after the format check, so a question about to be discarded is
  never catalogued;
- nothing calls out at all when there is nothing to compare.

### 8. Machine runs never overwrite human decisions

Review is the product of this tool, so a re-run must not undo it. Status is set on
insert only; a corrected badge title and curated links are locked against later syncs;
and when a merge or a duplicate sweep must choose a survivor, it prefers the record
carrying review work — curated over machine-written.

### 9. Model output is validated deterministically

Schemas are permissive on arrival and the rules are enforced afterwards, in code:
exactly four options, exactly one correct, no repeated or empty options, a non-empty
stem, badge slugs that name a real badge, positional answers bounds-checked against
what was sent. A schema strict enough to reject one bad question would fail the whole
batch and lose the good ones.

Refusals, truncated structured output and missing parsed results are errors — never
read as "the model found nothing", which is indistinguishable from a correct empty
answer.

### 10. Fetched content is data, never instructions or markup

Documentation pages, search results and log lines are content this program did not
write. They go into a prompt as clearly-labelled reference material, and into a page as
data: the Markdown viewer receives page text as JSON and sanitises it before it reaches
the document; the log viewer and every traceback are written with `textContent`. No
endpoint accepts a file path — the log viewer serves one known file and takes no path
parameter at all, because there are no authorizations here to fall back on.

### 11. Configuration from the environment; external contracts as named constants

Every setting resolves from the environment through one frozen `Settings` object, and
nothing that identifies infrastructure is defaulted in code that lives in a public
repository. Logging is deliberately configured *without* `Settings`, because `Settings`
requires a database connection string and "cannot reach Atlas" is exactly what someone
comes to the log to find out.

Anything named outside this repository is a constant, not a literal: the vector index
name and the `embedding_text` field path are referenced by an Atlas index definition
created by hand, so renaming either would silently stop the index matching anything
with no error raised here.

### 12. Storage shapes follow the way things are read

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

### 13. Atlas does the embedding and the reranking

The vector index uses `autoEmbed`, and reranking uses the native `$rerank` stage in the
same aggregation. This program stores no vectors, needs no model API key for retrieval,
and makes no second round trip. The consequence to preserve: retrieval quality is a
property of the index definition and the pipeline, not of client code.

### 14. Tests are the recorded requirements

Every test carries an `Intent` / `Success` / `Feature` block, and those blocks are never
edited. When a requirement genuinely changes — as several have — the test is **replaced**
with a new block that records the new requirement and says what it supersedes, rather
than quietly relaxed to match the code.

The suite is hermetic: no network, no Atlas, no model API. Credentials are stripped by
an autouse fixture, MongoDB is an in-memory fake that implements real operator
semantics, the Anthropic client is a scripted double, and HTTP is a local stub server.
When a fake diverges from real behaviour it gets fixed — a fake that returns the wrong
shape lets a test pass while the code asks for fields it will never receive.

### 15. Two areas, separated by audience rather than permission

Authoring lives at the root; curation lives under `/admin`. There are no authorizations
anywhere in this program, and both are reachable by anyone who can reach the service.
The split exists so each screen has one audience, and it is enforced only by tests: the
questions screen is not served under `/admin`, and no questions endpoint is served under
`/api/admin`.

### 16. Destructive actions are reversible, confirmed, or both

Retiring is reversible and deleting is not, so a badge cannot be deleted until it is
retired. The duplicate sweep deletes nothing at all — it reports, and deleting is a
separate act on a list somebody has read. Deleting a question, singly or as a batch,
requires typing the word. Every irreversible button confirms first, and says what
cannot be undone.

A control that is "the same thing but safe" is a smell: it makes the operator choose a
mode before they have the information to choose with, and the safe one is strictly more
informative. Where that pattern appeared — a dry run beside a real sweep — the
destructive mode was removed rather than kept as an option.

## How it works

```
llms.txt indexes ──crawl──▶ doc_pages ──split──▶ doc_chunks ──┐
                                                              │ vector search
skill_badges ──topic queries──────────────────────────────────┤ per badge
                                                              ▼
                                                     section set (ranked)
                                                              │ one section, one call
                                                              ▼
                                          questions ──▶ duplicate sweep (on request)
                                                              │
                                          generation_runs ◀───┘ cost, timings, choices
```

### The corpus

MongoDB publishes an agent-oriented index of its documentation (`llms.txt`) and serves
every page as Markdown. That is the only enumerable route to the whole corpus: the MCP
server's `search-knowledge` returns the best few chunks for a query and cannot be asked for
everything — the right tool at authoring time, the wrong one for building a cache.

**Refresh documentation** crawls it and chunks each batch as it lands: one action, not two.
Measured 2026-08-19 — 7,158 pages (~75 MB) becoming 27,399 sections in **71 minutes**. Most
of that hour is waiting, not transferring: the docs sit behind CloudFront, which starts
answering **403** when a crawl asks for too much, and each refusal costs a growing back-off.
It has not been optimised because it does not need to be — the corpus is refreshed about
twice a year, so being politely slow is what keeps the crawl from being blocked outright.

Being refused is handled rather than merely reported. `Retry-After` is honoured when sent;
after enough consecutive refusals the crawl stops rather than prolonging the block, keeps
everything it fetched, and says so. **Fill gaps** then fetches only what is missing and
removes nothing — re-crawling seven thousand pages to recover a few hundred wastes an hour
and invites another block. Pages are written in batches so a crawl that dies half way leaves
the corpus it did fetch intact, and the sweep that removes withdrawn pages is skipped
entirely if the crawl was refused or stored nothing, since sweeping on partial evidence
would delete everything it never reached.

Navigation stubs are skipped by a byte floor: an index page listing links is not something
a question can be written from.

### Two units, and the words for them

A **page** is a documentation page. A **chunk** is a heading and the passage under it —
one page holds 3.8 of them on average across the stored corpus, and up to 253. A run reads
one chunk per Claude call, so the chunk is the unit of everything a run is measured in, and
every screen now says chunk for it.

It did not, for a while: the generate form said "how many pages to walk", the progress panel
and the run history said sections, and the material screen said articles — four words for
one thing, three of them for the piece rather than the page. The form's number is the one
that decides what a run costs, and calling it pages overstated the material by about four
times. The stored run records still use `pages_done` / `pages_total` / `pages_available` as
their keys: renaming them would orphan every run already recorded, and an old run is a
record of what was known then.

### Splitting pages into sections

A page is the wrong unit, and it took two failures to establish that. Sent whole, one
**1.7 MB** page — a driver tutorial repeating every example in a dozen languages — cost
**$2.58 for three questions** (505,435 input tokens). Capping what a page contributes fixed
the cost and created a worse problem: everything past the cap became unreachable, so a
page's later material could never produce a question however often a badge was walked.

So each page is split into **sections**, and a section is what gets embedded, retrieved and
written from. Retrieval sharpens as a side effect: a section about `$search` stops being
buried inside a page about aggregation.

**The band was measured, not guessed.** Over the 3,844 non-reference pages:

| split at | sections | median chars | under 500 |
|---|---:|---:|---:|
| H1–H2 | 26,125 | 642 | 44% |
| H1–H3 | 40,561 | 545 | 47% |
| H1–H4 | 43,589 | 528 | 48% |

Sections are mostly *small*, so **merging matters more than splitting** — a naive
split-on-headings corpus would be mostly heading stubs, which embed badly and support no
question at all. Three passes, in order: cut at headings to `chunk_heading_depth` (3); cut
anything still over the ceiling on blank lines, and bluntly if a single paragraph is still
too big (needed where the heading structure gives out inside a giant code block); then pack
neighbours until each chunk clears the floor. At 1,500/8,000 that produced **27,399
sections**, median 2,026 chars, p90 6,683, nothing over the ceiling, 5% under 500. Of those,
33% sit under reference paths and are excluded from walks, leaving **18,434 to write from**.

Every chunk carries its page and page title, the heading it sits under, the full heading
path above it, its source index, position, size and content hash. The heading path **leads
the embedded text**, because "Limitations" embedded bare matches every limitations section
in the corpus, and means something only as "Atlas Vector Search > Filtering > Limitations".
That is why a chunk has both `text` (what the model reads, what excerpts come from) and
`embed_text` (the same with its context prepended): Atlas autoEmbed indexes one field path
and cannot concatenate at index time.

**Chunks are derived, not crawled**, and live in their own collection. **Re-chunk** on the
corpus screen re-splits everything from stored pages in seconds, because the band is a
judgement that will want re-tuning against real question quality and that should not cost
another 71-minute crawl. Chunk ids key on page URL and position, so a rebuild of an
unchanged page produces the same ids and questions written from it stay attributable.
Chunks are stamped with the refresh that wrote them and swept the same way pages are — a
chunk outliving its page is invisible and harmful, since retrieval keeps offering it and a
question written from it cites a URL that now 404s.

### Resolving a badge to its material

A badge document supplies a name, a description and topic areas. Those become one semantic
search for the badge overall plus one per topic area, each with the badge name attached
because "indexes" matches most of the corpus while "Atlas Search indexes" matches what the
badge means by it. Candidates are then filtered three ways:

- **A relevance floor** (`doc_page_set_score_floor`, 0.70). Topic areas come from Credly's
  skill tags, which are marketing metadata rather than a syllabus: on the live Cluster
  Reliability badge the tag "Cluster IP" — a Kubernetes term — pulled VPC peering and IP
  access lists in at 0.64–0.69, while pages plainly about the badge scored 0.70–0.86. The
  floor sits in that gap.
- **Reference material is excluded.** A third of the corpus is parameter lists, CLI
  synopses and command references. A question written from a parameter list tests whether
  someone can look up a flag, which is not a skill the badges certify.
- **Sections already written from are dropped**, derived from the `source_chunk_ids` of the
  badge's existing questions. Section-level rather than page-level: excluding a whole page
  would mean one question written from its opening made the rest of that page unreachable
  forever, the reverse of the point.

**Then no page may crowd out the others.** Sections are taken in rounds — the best from
every page, then the second best — because relevance order alone is not enough when one
page scores well throughout. Measured: a Vector Search Fundamentals run walked 25 sections
drawn from **six pages**, since 85 of that badge's 252 sections were hard-split slices of
one 1.7 MB page under an identical heading. Twenty of those 25 produced nothing, while
`mongodb-overview` spread over 24 pages produced 72 from the same budget. With the rounds,
that badge's first 25 sections come from 25 distinct pages. Sections held back by the
per-page limit are appended rather than dropped: the limit reorders the set, it does not
shrink it.

### Writing questions

A run is scoped to a badge and **walks** its section set, one Claude call per section,
storing as it goes. That is the opposite of the obvious design, and the reason is capacity:
cramming a badge's best pages into one prompt caps it at whatever fits in a request, and
asking the same badge again re-reads the same material, so the second batch is variations
on the first. Walking makes each section read exactly once, worth several questions, with
coverage a counter against an enumerable list. It also spreads the cost — questions arrive
per badge when somebody asks for that badge, so the first badge tells you whether the
output is any good before the other 33 are paid for.

**The page budget is per badge, and the form says so.** Selecting several badges walks
each one with the full budget in turn, so 25 pages at 3 questions each over 34 badges is up
to 2,550 questions and 850 pages, not 75. The form projects the multiplication as the
selection changes, and calls it a ceiling: pages already written from are skipped, so a
badge with little unused material contributes fewer, and **Stop after this page** ends the
whole run rather than the badge being walked.

**One section, one structured call.** Not the draft-then-extract pair the older
research path uses: that earns its keep when a turn is doing research and benefits from
thinking in prose first, but reading one section needs no tools, so a second pass would
only pay output tokens again to restate questions already written. Badge attribution is
folded into the same call for the same reason — the catalog is small and the model is
already holding the question.

**Effort is tuned separately** (`page_author_effort`, `medium`) rather than inherited from
the research path's `high`. Output tokens dominate a walk's cost and thinking dominates
output, so this is the largest single cost lever in the program — and the least tested
assumption in it.

**Nothing is lost to one bad section.** Questions are stored section by section, so a
failure on section eighteen keeps the first seventeen. A section that refuses, truncates or
has vanished is recorded with its reason and stepped over. A walk can be stopped, and keeps
what it wrote.

**When a badge resolves to nothing**, two different situations get two different answers. A
badge never walked has no material in the corpus, so the run falls back to the older
single-prompt path with server-side web search and says so. A badge whose sections are all
used up is *exhausted*: another run will not help, and the fix is a wider corpus or a lower
floor, not another press of the button.

### What makes a question good

The prompt is the largest single artefact in the program and every rule in it is there
because output was wrong without it.

**The correct answer's position is randomised in code.** Measured on the first 125 questions
produced: the correct answer was option A in **every single one**. A candidate who always
answers A scores 100%, so the bank was worthless as a quiz. The cause is structural — a
model filling four options into a schema writes the right one first — and asking it not to
does not reliably work, so the options are shuffled after extraction and before storage
where it cannot be forgotten. `GET /api/questions/answer-positions` reports the spread,
which is the check that catches this recurring.

**A mix of question forms.** Left alone the model writes everything as a scenario, which is
exhausting to read and tests one narrow skill. The prompt names the forms and what each is
for: *situational* (judgement, failure modes, debugging), *factual* (behaviour, limits,
defaults — asked straight), *procedural* (order of operations), *best practice* (asked
plainly; a practitioner recognises "which order should this compound index use" faster
stated directly than buried in a story), *diagnostic* (given this output, what does it
mean), *comparative* (when to use one thing over a similar thing, where the real confusions
live). The material chooses the form, not a quota.

**A developer's voice.** The audience is working engineers, and a question phrased like a
technical writer's abstract announces that nobody who does the job wrote it. "Write
naturally" does not fix that, because the machine-written register comes from a specific
vocabulary — so the prompt bans it by name (*leverage*, *utilize*, *robust*, *seamless*,
*crucial*, *delve*, *streamline*…), bans filler openings and stock stems ("Which of the
following best describes…", "All of the above"), and requires specificity: real stage names,
flags, field names and error strings rather than "the appropriate configuration". That last
one is not only style — a question that will not name the flag is a question that tests
nothing.

**A skill level**, using the scale every question already carries: `foundational`,
`intermediate`, `advanced`, or mixed. Each level is *described* rather than named, because
"advanced" alone is read as harder wording rather than harder judgement and yields obscure
trivia — so advanced explicitly means failure modes, interactions between features, and the
reasoning behind a recommendation, and explicitly not a version number nobody remembers.
Mixed is an instruction to spread the levels, not the absence of one.

**The format is enforced, not requested.** Four options, exactly one correct, no repeated or
empty options, a non-empty stem. Anything failing is discarded with a reason rather than
stored for a reviewer to find, and the reasons are reported: a run that quietly stored three
of ten would look like a model with little to say.

**Badge slugs are checked against the catalog.** `skill_badges` is what the whole collection
is filtered by, so a hallucinated slug would make a question unfindable under any real badge
while looking correctly tagged. Unknown slugs are dropped; a question left with none falls
back to the badge the run was scoped to.

### Identifiers

A question has two, for different reasons. `question_id` is what this program keys on and
what every endpoint takes; MongoDB's `_id` is projected out of every listing, so it is the
one an author only ever sees in Atlas or Compass — which is exactly when they want the
question it belongs to.

So both are accepted everywhere a question is looked up: `question_id` is shown on the card
(shortened, click to copy) at the top of it, labelled, above the question's generated date
and its source link — the three facts about a question that an author refers to, dates and
checks it by, read before the question rather than after its options. The search box takes
either. A query is treated as an
identifier by **shape** — 32 hex characters for a `question_id`, 24 for an ObjectId,
neither of which anyone types as a search — and looked up exactly rather than embedded,
because a hex string has no meaning to embed and a semantic search for one returns whatever
happens to be nearest, which reads as "that question does not exist". An identifier that
matches nothing says so, and says it was understood as an identifier.

The API route is `/api/questions/by-id/{identifier}` rather than `/{identifier}`: the
latter would capture every named route added under the prefix later, and the collision
would surface as a puzzling 422 on whichever route lost.

### Paging

The bank is meant to hold thousands of questions, so the list is paged: 5, 10, 25, 50,
100, 200 or 500 at a time, defaulting to 50. Rendering everything builds a document the
browser is slow to lay out and scroll, from a cursor that read every match to produce it —
the screen would get worse exactly as the collection got more valuable.

The size is validated against that list rather than trusted, because it reaches the
database as a limit and a hand-edited URL asking for a hundred thousand would render the
page this exists to prevent. A page number past the end is clamped to the last page rather
than refused: deleting the last question on the last page leaves a URL pointing past it,
and an error there is a dead end where showing the last page is what was meant. Changing a
filter returns to the first page, since page 7 of the old result is not page 7 of the new
one.

The pager carries the filters, the search and the size, so the view stays an address that
can be shared. **Export JSON is not paged** — it returns every question matching the
filters, which is what anyone asking for an export means.

### Saturation, and why sections are not the measure

`/admin/material` answers the question the coverage screen raises: whether a badge is
about to run out of material worth writing from. The section count does not answer it. A
walk takes one section per article before it takes a second from any of them, so the
**distinct article count** is the ceiling on new material — measured on the live corpus,
Vector Search Fundamentals resolves to 252 sections across 25 articles, 85 of them slices
of one page, so it has about 25 sections' worth of fresh material and 227 helpings of what
it already read. The screen therefore reports articles left, sections left, sections per
article, the biggest single article's share, and how many articles have been written from —
and orders badges by how few articles they have left rather than how few sections.

Two kinds of filter, kept apart because they act on different things. **Sections about**
narrows the documentation, matching a topic against each section's heading, its article's
title and the URL — not its body, since a body mentions everything it relates to. That is
what answers "this badge looks healthy at 25 articles, but how much of it is about Voyage
AI" — the answer on the live corpus being 3. **Category** and **skill level** narrow the
questions already written, and change no documentation figure: the corpus is not tagged the
way questions are, and a screen that implied otherwise would report two numbers that cannot
both be true.

Measuring is opt-in. Resolving one badge's sections is dozens of vector searches and all of
them is tens of seconds, so it happens when asked for and never on a page load or a filter
change.

### No review workflow

A question that passes the format check is stored and usable. There is no draft state, no
approve step, no reject step. At thousands of questions nobody works a queue of drafts, so
the gate was a bottleneck rather than a safeguard, and a question nobody had blessed was
indistinguishable from one nobody wanted.

Deleting is the only editorial act, and it is guarded twice: the dialog shows the question
again and the word *delete* has to be typed. Nothing can re-create a question, and the
button sits next to no other control, so a misplaced click has nowhere else to land.

Each question shows when it was written, and while a run is going the screen polls a count
endpoint and reloads as new questions arrive — a walk stores over many minutes, so a list
left alone goes stale while its reader watches it being filled. The count is scoped to the
filters on screen, and the reload is skipped while a dialog is open or the tab is hidden.

### Cost, throughput and history

**Cost is reported, not estimated.** Every response carries its token counts, so a run adds
up exactly what it consumed and prices it from rates that live next to the model in
`Settings` — meaning the published price is the only thing here that can be wrong. The
status panel shows spend so far and the projected total at the current rate, which is what
makes **Stop after this page** an informed decision rather than a guess. Nothing is
projected until a section has finished, because a projection from zero reads as "this run is
free" at exactly the wrong moment.

**A failed lookup is not an empty one.** `chunk_set_for_badge` used to return an empty
list when its search failed, which is the same value it returns for "this badge has nothing
left" — so a transient Atlas 503 was reported as a badge having exhausted its documentation.
Observed on 2026-08-19: Search Fundamentals, which has 291 chunks, reported as used up, and
the run walked nothing for it while still paying for the attempt. It now raises
`ChunkSetUnavailable`, and the walk reports that badge as failed with the cause rather than
claiming exhaustion or researching around it — the two call for opposite actions, try again
or widen the corpus.

**A multi-badge run reports the job as well as the badge.** Every figure on the panel used
to belong to the badge being walked, so a run over several of them showed a cost, a count
and a percentage that reset at each badge while the clock ran on — leaving the one thing the
author actually asked for unreported. Finished badges are summed and the badge in flight
added to them. Overall progress is counted in badges, not chunks: a badge's chunk set is
only resolved when its walk begins, so a percentage derived from the requested maximum would
jump backwards whenever a badge turned out to have less material than asked for. The job's projected question count and
projected spend are measured per chunk — questions actually written per chunk, and dollars
per question — against the chunks the job is heading for, so both answer after the first
chunk rather than after the first badge. Projecting from the requested questions-per-chunk
would state the ceiling as the expectation; the model decides what a chunk's material
supports, and it is often fewer. The chunk total is itself a ceiling for badges not yet
started, since a badge can hold less material than the budget asked for — one held 7
against a budget of 25 — which is why it feeds the projection and not the progress bar.

**Questions per minute and cost per question** are the two derived figures worth watching.
Total spend cannot be compared between runs because it depends on how much was walked; cost
per question says whether a prompt or effort change paid for itself. Both are absent rather
than zero until there is something to divide by.

Coverage and export have screens too, for the same reason. **Coverage** answers "what
should I run next", which is asked before looking at questions rather than while looking at
them, and its rows link into the filtered list. It is the one list here not rendered
server-side: resolving every badge's page set is dozens of vector searches, and a screen
that shows nothing until they finish reads as broken rather than as slow, so it paints and
then fills. **Export** was a toolbar link scoped to whatever the list happened to be
filtered to — meaning what you got depended on a filter you may have set minutes earlier
and scrolled past. It now carries its own filters, states how many questions are in it, and
shows the JSON on the page as well as offering the download, because pasting it somewhere
is the usual thing to do with it.

The history has its own screen at `/runs`, server-rendered like every other list. It was a
dialog on the questions screen, filled in by JavaScript when it opened — which made the one
lasting record of what has been spent the least reachable thing here: it could not be linked
to, could not be read beside the questions a run produced, and did not exist for a browser
that had not run the script. The cumulative totals sit above the runs, because per-run cost
is small enough to ignore individually and large enough to matter in aggregate. Runs
recorded before a figure was measured do not carry it and show it as absent; the collection
is never migrated, since an old run is a record of what was known then.

**Every finished run is recorded** in `generation_runs` — the badge, the choices made, the
model, the effort, the relevance floor, the timings, the counts, the cost, the sections read
and anything that failed. Failed runs too: "we tried this badge and it broke" is exactly
what gets forgotten and retried. Run state itself is one in-process dict and dies with the
process, and the token counts and wall clock are unrecoverable after the fact, so this is
the only lasting record. It is a separate collection because a run is an event and a
question is an artefact: deleting a bad batch must not erase the record that it was
generated, which is the evidence for changing the prompt.

**Coverage** lists every badge thinnest-first with its question count and how many sections
it has left to walk. Because a badge's questions come from its documentation, a badge with
little documentation gets few questions — this is what makes that a workflow rather than a
defect. Few questions and many sections left means run it again; few questions and none left
means the material is spent. Resolving every badge's section set is dozens of vector
searches, so the panel is fetched on demand rather than rendered with the screen.

### Duplicate detection

One aggregation per question does both stages on the cluster: `$vectorSearch` over
`embedding_text` shortlists the nearest questions, then `$rerank` re-scores each candidate
with a cross-encoder that reads both texts together — which is what separates "the same
question reworded" from "the same topic", something two independently embedded vectors
cannot do. An earlier design asked Claude to judge each pair; it was accurate and cost a
round trip per pair.

Calibrated against `rerank-2.5` on the live collection:

| pair | score |
|---|---:|
| genuinely distinct questions | 0.379 – 0.512 |
| deliberately reworded copy | 0.945 |
| identical text | 0.941 |

`question_rerank_delete_threshold` is **0.85**, inside that gap. Note the reranker does not
return 1.0 for identical text, so a threshold near 1.0 would never fire.

The sweep and its report have their own screen at `/duplicates`. They used to sit on the
questions list, where every entry in the report linked to a question — which meant linking
back to the page the report was on — and where a long list about pairs of questions
outweighed the list of questions it sat above. A sweep is also not that screen's work: it
is done occasionally to the whole collection, not in the course of writing or reviewing.
The two jobs still share one run slot, so each screen names the other's run rather than
reporting it as its own.

The sweep runs in the background on the same run state a generation run uses, reporting
after each question — the work is one round trip per stored question, and how many there
are is known before the first one, so the bar measures the whole job rather than standing
in for it. It also says which of the two jobs it is: they are not interchangeable — one writes questions and spends
money, the other only compares what is already stored — so the screen reports a sweep in
its own words, hides the stats that belong to a walk, and offers no stop button, there
being no seam to stop at.

**The threshold is a control, not a constant.** 0.85 is where the slider starts and is
labelled as the configured default, but every pair the reranker scored is on the page with
its score, so moving the slider re-partitions what is already there and runs nothing — the
scores are in hand and which side of a line one falls on is arithmetic. A pair below the
threshold renders the same tickable row as one above it, because the operator's judgement
outranks the number: "I have read both of these and they are the same question" is a better
reason to delete than any score. Ticks follow the slider, and a tick set by hand stands
until the slider moves again.

**A sweep can be scoped** to a skill badge, a category, a skill level, or any combination.
The cost is one round trip per question scanned, so without this the only sweep available on
a bank of thousands is the expensive one — run once and then never again. A pair is only
reported when both of its questions are inside the scope: comparing a subset against
everything would flag pairs whose other half the operator cannot judge from where they are.
The report echoes its scope, because "no duplicates" means something very different about
one badge than about the whole bank.

A pair is judged by reading both questions, so each one offers a **Compare** control that
puts them side by side in full — options, badges, source and all. Links to the questions
came first and were wrong: they asked the reader to hold one question in their head while
looking at the other in a different tab, which is the work they were trying to do. The two
sides are labelled "would be deleted" and "would be kept", because they are identical in
form and getting them the wrong way round deletes the one that was meant to survive. The
comparison is built through the DOM and never by assigning innerHTML — a stem, an option
and a rationale are all model-written text drawn from fetched documentation.

**Finding never deletes.** Pairs at or above the threshold are *flagged*, with the question
this program would drop and the one it would keep both named — more badges beats fewer, then
older beats newer — and pairs below it are listed too, so the threshold stays visible as a
judgement rather than a fact. Deleting is a separate act on that list: each flagged pair has
its own tickbox, so a pair judged to be two genuinely different questions is left alone
rather than decided by the threshold again, and the ticked set goes through the same typed
confirmation a single delete uses.

That demotes 0.85 from deciding which questions die to shortlisting the ones worth reading,
which is the right weight for a number measured on six questions. It also removed the old
dry-run button — see strategy 16.

### Citations

Each question links the section's page. The visible text is the canonical `mongodb.com`
URL, but the link goes through `/admin/docs/render`, which fetches that page now and renders
it with the same Markdown viewer the stored copy uses: MongoDB serves these as raw Markdown,
so following the URL directly lands on unformatted text, and a citation nobody wants to read
is a citation nobody checks.

Live rather than stored, deliberately — the stored copy is the snapshot the question was
written from and the live page is what MongoDB publishes today. The view says which one is
on screen and offers the stored copy beside it, so a divergence reads as a divergence rather
than a wrong question.

**That route is host-pinned.** It fetches a caller-supplied URL server-side, which is a
server-side request forgery hole unless the host is fixed: an arbitrary URL would reach
anything the server can reach — an internal service, a cloud metadata endpoint — and hand
the response back. Only `https` pages on `docs_domain` are fetched, checked on the parsed
hostname rather than by prefix, since `https://www.mongodb.com.evil.example/` starts with
the right string and is not the right host. The check lives in the fetcher as well as the
route, so a later caller cannot bypass it by forgetting.

### The badge catalog

Badges are discovered rather than hand-listed, because the set grows. Credly's collection
endpoint returns the badge set as JSON, so the list is a deterministic fetch rather than a
research result; Claude then fills in descriptions, topic areas and reference links. The
badge artwork title is canonical for naming — every other source disagrees with it — and
slugs derive from it.

Hand edits are protected: a corrected title or a curated link is never overwritten by a
later sync. Badge review keeps the workflow questions no longer have (candidate / approved /
retired), because a badge is a claim about what MongoDB certifies and a wrong one
mis-scopes every question written against it.

## Screens

| Address | What it is |
|---|---|
| `/` | **Authoring** — write, browse, filter, export questions |
| `/admin/skill-badges` | Badge catalog and review |
| `/admin/docs` | Documentation corpus: refresh, fill gaps, re-chunk |
| `/admin/docs/search?q=` | Semantic search over every stored section |
| `/admin/docs/source?source=` | Sections in one documentation source |
| `/admin/docs/page?url=` | One stored page, rendered |
| `/admin/docs/render?url=` | The canonical page, fetched live and rendered |
| `/admin/logs` | Log viewer |

## API

Questions, under `/api/questions`:

| | | |
|---|---|---|
| `POST` | `/generate` | Start a walk: `skill_badges`, `max_pages`, `questions_per_page`, `difficulty` |
| `GET` | `/generate/status` | Poll — phase, sections, cost, section in flight |
| `POST` | `/generate/stop` | Stop after the section in flight |
| `POST` | `/generate/dismiss` | Clear the last run's notice |
| `GET` | `` | List / export, filtered by `skill_badge` and `category` |
| `GET` | `/count?skill_badge=&category=` | How many match — polled during a run |
| `GET` | `/by-id/{identifier}` | One question, by `question_id` or ObjectId |
| `GET` | `/search?q=&limit=` | Questions ranked by similarity |
| `GET` | `/coverage` | Per-badge counts and sections left |
| `GET` | `/runs`, `/runs/{id}` | Run history, and one run in full |
| `GET` | `/answer-positions` | Where the correct answer sits |
| `POST` | `/shuffle-options` | Re-order stored questions' options |
| `POST` | `/duplicates/sweep` | Find duplicate candidates; deletes nothing |
| `POST` | `/duplicates/delete` | Delete questions chosen from that report |
| `POST` | `/backfill-embedding-text` | Compose `embedding_text` where missing |
| `POST` | `/drop-status` | Strip the legacy review field |
| `DELETE` | `/{id}` | Delete a question |

Documentation and badges, under `/api/admin`:

| | | |
|---|---|---|
| `POST` | `/docs/refresh?mode=replace\|fill` | Crawl and chunk |
| `GET` | `/docs/refresh/status` | Poll a crawl |
| `POST` | `/docs/rechunk` | Re-split stored pages; fetches nothing |
| `GET` | `/docs/chunks` | How the corpus is currently chunked |
| `GET` | `/docs/chunks/page?url=` | One page's sections, in order |
| `GET` | `/docs/sources`, `/docs/pages`, `/docs/page` | Stored inventory |
| `GET` | `/docs/search?q=` | Section search |
| `POST` | `/docs/prune-stubs` | Remove navigation stubs stored before the floor existed |
| `POST` | `/skill-badges/sync` | Discover and refresh the catalog |
| `POST` | `/skill-badges/{slug}/status` | Set review status |
| `GET` | `/logs/tail` | Log tail |

## Layout

```
app/main.py                            FastAPI app; mounts the routers, configures logging
app/config.py                          settings; measured constants with their measurements
app/db.py                              Mongo client (one per process)
app/logging_config.py                  rotating file log, and reading it back

app/models/question.py                 question schemas (Claude output + stored doc)
app/models/skill_badge.py              badge schemas

app/services/doc_corpus.py             crawls the published docs index; rebuilds chunks
app/services/doc_chunking.py           splits a page into sections  ← the band lives here
app/services/doc_retrieval.py          resolves a badge to the sections it is about
app/services/question_generation.py    the prompts and the walk    ← the biggest file
app/services/question_duplicates.py    $vectorSearch + $rerank sweep
app/services/run_cost.py               prices a run from the tokens it reported
app/services/badge_discovery.py        the two Claude passes for badges
app/services/badge_art.py              reads the title out of the badge artwork
app/services/badge_titles.py           the four name sources, and which wins
app/services/badge_matching.py         matching a discovered badge to a stored one
app/services/credly_catalog.py         the badge set, as JSON from Credly
app/services/credly_page.py            one badge's Credly page
app/services/mongodb_page.py           one badge's learn.mongodb.com page
app/services/duplicates.py             badge duplicate detection
app/services/discover_cli.py           shell entry point

app/repositories/doc_pages.py          crawled pages
app/repositories/doc_chunks.py         sections: storage, search, totals
app/repositories/questions.py          insert / filter / count / delete, indexes
app/repositories/runs.py               run history: record, list, totals
app/repositories/skill_badges.py       upsert / list / status, indexes

app/routers/pages.py                   the questions screen, served at /
app/routers/questions.py               /api/questions
app/routers/admin_pages.py             server-rendered /admin screens
app/routers/admin_docs.py              /api/admin/docs
app/routers/admin_skill_badges.py      /api/admin/skill-badges
app/routers/admin_logs.py              /api/admin/logs

app/templates/                         base shell + one template per screen
tests/                                 pytest suite + fakes (see tests/README.md)
```

Reading order for someone new: `app/config.py` (the comments are the design rationale),
then `doc_chunking.py`, `doc_retrieval.py`, `question_generation.py` — that is the pipeline
in order.

## Known limitations

- **A walk is serial and single-run.** One section at a time, one run at a time, run state
  in a single in-process dict. Walking 34 badges means supervising it, and a restart loses
  the live state (finished runs survive in `generation_runs`). The Message Batches API is
  the natural fit — independent requests, nobody waiting, half price — but whether the
  Grove gateway exposes it is unprobed.

- **`page_author_effort` is set on reasoning, not measurement.** Output tokens dominate a
  walk's cost and thinking dominates output, so this is the program's largest untested cost
  assumption. Twenty sections at each effort level would settle it.

- **Relevance screening is a similarity floor and a URL pattern.** A cheap per-candidate
  classification pass would judge "is this section really about this badge" better than a
  score can, and the section set should be reviewable on screen before a run spends against
  it.

- **Nothing checks for duplicates at generation time.** The sweep is on request, over what
  is stored. At the current scale that is the right trade; at tens of thousands it may not
  be.

- **The duplicate threshold is calibrated on six questions.** It is a shortlist filter
  rather than a delete decision, which is why that is tolerable — but it wants recalibrating
  once the bank spans many badges, and the "would a leaked answer give this one away"
  criterion is sharper than similarity and not directly measured.

- **The log viewer shows only the active file.** Reading a rotated file means going to disk.

- **`learn.mongodb.com` course pages are client-rendered**, so their titles come from the
  search index rather than the page. Three badges have no title available by any headless
  method; a rendering browser (e.g. Playwright) would be needed. One badge has no readable
  artwork title, so its name and slug fall back to the reviewed title.

- **A wipe-and-rebuild reproduces everything machine-derived** (badges, slugs, name
  sources, artwork, links) but **not** reviewed titles, approvals or curated links. Back up
  before testing that.

## Open questions

- Whether the MongoDB MCP server exposes documentation search. It is configured read-only
  in `.mcp.json` and is not yet authorized.
- Whether the Grove gateway exposes the Batches API, which would halve bulk generation cost
  and remove the supervision problem.
- Whether chunk-level retrieval wants a reranking pass of its own, as duplicate detection
  has: the section set is currently ordered by embedding similarity alone.
