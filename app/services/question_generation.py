"""Generate skill badge quiz questions with Claude.

Two passes, mirroring badge discovery:

1. Authoring — Claude reads the selected badges, any material the author pasted
   in, and the documentation pages retrieved for those badges out of the stored
   corpus, then drafts questions as prose. Streaming, because the turn runs for
   minutes. When the corpus returns nothing — not crawled yet, or its Atlas index
   missing — the turn falls back to server-side web search and fetch instead.
2. Extraction — the draft is turned into validated structured output.

The badges scope the questions; they are not the whole source. What a badge
document supplies is its title, what it covers, its topic areas and its curated
links — enough to keep a question inside the badge's syllabus. Deeper material
(internal training content, live cluster behaviour) is passed in by the author as
`source_material` for now: this process has no Glean or MongoDB MCP access of its
own, so that content is pasted rather than fetched.

Reading from the corpus rather than the web is what makes a run repeatable. Two
runs on the same badge now see the same source text, so a question that comes out
badly is a prompt problem rather than a different day's search results; and the
minutes a run used to spend waiting on fetches are gone.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from app.config import Settings, get_settings
from app.services.run_cost import RunCost
from app.models.question import (
    GeneratedQuestion,
    GeneratedQuestions,
    QuestionBadgeAttributions,
)

logger = logging.getLogger(__name__)

OPTIONS_PER_QUESTION = 4

AUTHOR_SYSTEM = """\
You write assessment questions for MongoDB's skill badge quizzes, on behalf of \
the team that authors them. The reader is a practitioner earning the badge, not \
a beginner being introduced to MongoDB.

Scope: the skill badges named in the request. A question must test something \
inside those badges' subject matter. Nothing else is in scope, however \
interesting.

Every question is multiple choice with exactly FOUR options and exactly ONE \
correct answer.

What makes these questions good — this is the whole point of the exercise:

1. Test understanding, not recall of a sentence. A question whose answer is \
   found by matching a phrase from the documentation tests reading, not skill. \
   Prefer questions that put the candidate in a situation and ask what to do, \
   or what will happen, or why an approach fails.
2. Every wrong option must be a mistake a real practitioner would actually make \
   — a plausible misreading, a confusion between two similar features, an \
   approach that works in a different situation. Never pad the option list with \
   an answer nobody would pick.
3. Exactly one option is defensibly correct. If two options could both be \
   argued, the question is broken: rewrite it.
4. The stem must be answerable on its own, before the options are read.
5. Do not signal the answer by making the correct option the longest, the most \
   qualified, or the only grammatical fit.
6. No trick questions, no trivia about version numbers or default port \
   numbers, no questions about the badge or the quiz itself.
7. Ground each question in the material provided or in MongoDB's official \
   documentation. If you had to guess at how a feature behaves, do not write \
   the question.

For each option give a short rationale: for the correct one, why it is right; \
for a wrong one, the specific misconception it catches. For each question, note \
which badges it serves, the topic areas it exercises, its difficulty, and the \
URLs of the material it came from.

Write the questions as a plain numbered list. No preamble, no methodology \
section, no closing summary."""

ATTRIBUTE_SYSTEM = """\
You are cataloguing finished quiz questions against MongoDB's skill badges.

A question was written for one set of badges, but skills overlap: a question \
about an aggregation stage used to reshape documents for a search index may \
belong to the aggregation badge and the search badge both. Your job is to find \
every badge whose subject matter the question genuinely tests, so an author \
looking at any of those badges finds it.

For each question you are given the stem, all four options and the explanation. \
Read the answers, not just the stem: what a question tests is often visible only \
in what distinguishes the correct option from the wrong ones.

Include a badge when someone earning that badge would be expected to answer the \
question correctly, and answering it requires knowledge that badge covers.

Do NOT include a badge because:
- the question mentions a term that appears in the badge's description;
- the badge is adjacent, related, or a prerequisite;
- the topic is broadly part of MongoDB and so is the badge.

Over-tagging is worse than under-tagging: a badge whose question list is full of \
questions that only nearly apply is a badge whose list cannot be trusted. When \
in doubt, leave the badge out.

Use only slugs from the badge list given to you — never invent one, and never \
alter one. Always include the badges the question was written for. Say briefly \
why any additional badge applies."""

# The difficulty scale every question already carries. Described here because
# "advanced" on its own is read as "harder wording" rather than "harder judgement",
# which produces obscure trivia instead of questions a senior engineer finds worth
# answering.
DIFFICULTY_GUIDANCE = {
    "foundational": (
        "Target FOUNDATIONAL difficulty: someone who has used MongoDB for a few "
        "weeks. Test what a feature does and when to reach for it. Do not test "
        "interactions between features, tuning, or failure modes."
    ),
    "intermediate": (
        "Target INTERMEDIATE difficulty: someone who ships MongoDB in production. "
        "Test choosing between two reasonable approaches, reading diagnostic output, "
        "and the consequences of a configuration. Assume the basics."
    ),
    "advanced": (
        "Target ADVANCED difficulty: someone who owns the deployment. Test failure "
        "modes, interactions between features, and the reasoning behind a "
        "recommendation rather than the recommendation itself. Harder means deeper "
        "judgement, not obscurer trivia — a version number nobody remembers is not "
        "an advanced question."
    ),
}


PAGE_AUTHOR_SYSTEM = """\
You write assessment questions for MongoDB's skill badge quizzes, on behalf of \
the team that authors them. The reader is a practitioner earning a badge, not a \
beginner being introduced to MongoDB.

You are given ONE page of MongoDB's official documentation and the skill badge \
the page was selected for. Write questions that this page supports. The page is \
your source: if the page does not establish something, do not write a question \
that depends on it, and never fall back on your own memory of how a feature \
behaves.

Mine the page. It is being read once, and a page that covers several ideas should \
yield a question about each of them rather than several questions about the first. \
If the page genuinely supports fewer questions than asked for, write fewer — a \
padded question is worse than a missing one.

Every question is multiple choice with exactly FOUR options and exactly ONE \
correct answer.

What makes these questions good — this is the whole point of the exercise:

1. Test understanding, not recall of a sentence. A question whose answer is \
   found by matching a phrase from the page tests reading, not skill.

MIX THE KINDS OF QUESTION. Do not write every question as a scenario. A page of \
questions that all open by setting a scene is exhausting to read and tests one \
narrow skill. Vary the form to suit what is being tested:

- **Situational** — the candidate is somewhere and has to act or predict. Right \
  for judgement, failure modes, trade-offs, debugging. "A write with \
  `w: "majority"` times out on a three-node set with one node down. Why?"
- **Factual** — a direct question with a definite answer. Right for behaviour, \
  limits, defaults and guarantees. Ask it straight: "What does `$unwind` do to a \
  document whose array field is empty?" No scene-setting, no preamble.
- **Procedural** — the correct order of operations, or which step comes next. \
  Right for anything with a sequence where getting the order wrong breaks it. \
  "You are adding a shard to a running cluster. Which of these happens first?" \
  Options can be orderings.
- **Best practice** — what you should do, and why that rather than the \
  alternative. Ask these directly. "You have a field queried on equality and \
  another on range. Which order should the compound index use?" Do not dress a \
  best practice up as a story; practitioners recognise it faster stated plainly.
- **Diagnostic** — given an output, an error, or a metric, what does it mean. \
  "`rs.status()` shows a member in `RECOVERING`. What is it doing?"
- **Comparative** — when to use one thing rather than a similar thing. This is \
  where real confusions live, so these are often the most valuable questions on \
  a page.

Let the material choose the form. A page about a sequence should mostly yield \
procedural questions; a page defining behaviour should mostly yield factual and \
diagnostic ones. Do not force a scenario onto a page that is simply stating how \
something works — a direct question is not a lesser question.
2. Every wrong option must be a mistake a real practitioner would actually make \
   — a plausible misreading, a confusion between two similar features, an \
   approach that works in a different situation. Never pad the option list with \
   an answer nobody would pick.
3. Exactly one option is defensibly correct. If two options could both be \
   argued, the question is broken: rewrite it.
4. The stem must be answerable on its own, before the options are read.
5. Do not signal the answer by making the correct option the longest, the most \
   qualified, or the only grammatical fit.
6. No trick questions, no trivia about version numbers or default port numbers, \
   no questions about the badge or the quiz itself.
7. Questions must be independent of each other. Knowing the answer to one must \
   not give away another: these go into a bank large enough that a leaked quiz \
   does not compromise the rest, and that only holds if the questions do not \
   paraphrase one another.

HOW TO WRITE IT. The reader is a working software developer or engineer. Write \
the way a good developer writes — in a code review, a design doc, an incident \
postmortem. Correct grammar and correct spelling throughout, but a developer's \
vocabulary and a developer's sentences, not a technical writer's and not a \
marketer's.

Concretely:

- When a question sets a situation, put the candidate in it in the second person, \
  using the words they would use. "Your replica set has one node lagging \
  40 seconds behind the primary." Not "Consider a scenario in which a replica set \
  member exhibits replication lag." When a question does not need a situation, do \
  not invent one — ask the thing directly.
- Name real things. Actual stage names, commands, flags, field names, error \
  strings, numbers. `$lookup`, `rs.status()`, `w: "majority"`, "not primary". \
  Vague nouns like "the appropriate configuration" or "the relevant setting" are \
  the tell that a question knows nothing specific.
- Short sentences. Active voice. Contractions are fine. Say "you'll see", not \
  "one would observe".
- Never use these words: leverage, utilize, robust, seamless, seamlessly, \
  crucial, essential, vital, delve, harness, unlock, empower, streamline, \
  facilitate, comprehensive, cutting-edge, best-in-class, holistic, myriad, \
  plethora, paradigm, ecosystem (unless it literally means a software \
  ecosystem). Use the plain word a developer would use: "use", not "utilize"; \
  "reliable", not "robust".
- No filler openings: "It is important to note that", "In today's data-driven \
  world", "As a best practice", "When it comes to". Start with the substance.
- No stock stems. Never "Which of the following best describes…", "Which \
  statement is true regarding…", "All of the above", "None of the above". Ask \
  the actual question: "Why does the write still fail?" "What does the balancer \
  do next?"
- Do not stack rhetorical triads — "faster, safer, and more scalable" — and do \
  not lean on em dashes or semicolons. A developer writes shorter sentences \
  instead.
- No hedging. If a thing is true, say it. Cut "generally", "typically", "may \
  potentially" unless the behaviour genuinely varies, in which case say what it \
  depends on.
- Options should read like things a developer would actually say in a standup. \
  Keep them grammatically parallel and roughly the same length, so length is not \
  a clue.
- Code and identifiers go in backticks.

For each option give a short rationale, in the same voice: for the correct one, \
why it is right; for a wrong one, the specific misconception it catches. Write \
the rationale as you would explain it to a colleague at their desk, not as a \
textbook footnote.

File each question under every badge from the supplied catalog whose subject \
matter it genuinely tests — always including the badge the page was selected \
for. Use only slugs from that catalog, never invent or alter one. Include a \
badge only when someone earning it would be expected to answer the question and \
answering it needs knowledge that badge covers; not because the question mentions \
a term in the badge's description, and not because the badge is adjacent or a \
prerequisite. Over-tagging is worse than under-tagging.

Set source_urls to the page's Source URL."""

EXTRACT_SYSTEM = """\
Convert the drafted questions into structured records. Carry over every \
question exactly as drafted — do not reword a stem, add an option, drop an \
option, change which option is marked correct, or invent categories, badge \
slugs or URLs the draft does not contain. Use the badge slugs given in the \
draft."""


def _badge_brief(badge: dict[str, Any]) -> str:
    """The part of a badge document that scopes a question."""
    lines = [f"- Badge: {badge.get('name')} (slug: {badge.get('slug')})"]
    if badge.get("description"):
        lines.append(f"  Covers: {badge['description']}")
    if badge.get("categories"):
        lines.append(f"  Topic areas: {', '.join(badge['categories'])}")
    for url in badge.get("source_urls") or []:
        lines.append(f"  Reference: {url}")
    for field, label in (("mongodb_url", "Badge page"), ("credly_url", "Credly page")):
        if badge.get(field):
            lines.append(f"  {label}: {badge[field]}")
    return "\n".join(lines)


def build_prompt(
    badges: list[dict[str, Any]],
    count: int,
    *,
    source_material: str | None = None,
    extra_instructions: str | None = None,
    corpus_pages: list[dict[str, Any]] | None = None,
) -> str:
    """Assemble the authoring request. Kept separate so it can be asserted on."""
    from app.services import doc_retrieval

    briefs = "\n".join(_badge_brief(b) for b in badges)
    prompt = (
        f"Write {count} multiple-choice question(s) for the following skill "
        f"badge(s).\n\n{briefs}\n\n"
    )
    if corpus_pages:
        prompt += (
            "Below is MongoDB's own documentation for these badges, retrieved from "
            "this tool's stored copy of the docs. Write from it. Cite the Source URL "
            "of the page each question came from. If the material does not support a "
            "question you want to ask, ask a different question rather than writing "
            "from memory.\n\n"
            + doc_retrieval.format_pages(corpus_pages)
        )
    else:
        prompt += (
            "Use the reference links above, and MongoDB's official documentation, as "
            "your sources. Search and fetch as needed."
        )
    if source_material:
        prompt += (
            "\n\nThe author supplied this material to write from. Prefer it over "
            "anything you find yourself, and stay consistent with it:\n"
            f"{source_material}"
        )
    if extra_instructions:
        prompt += f"\n\nAdditional instructions from the author:\n{extra_instructions}"
    return prompt


def author_questions(
    badges: list[dict[str, Any]],
    count: int,
    *,
    source_material: str | None = None,
    extra_instructions: str | None = None,
    corpus_pages: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> str:
    """Run the authoring pass. Returns Claude's draft questions as prose.

    The web tools are offered only when the corpus supplied nothing. Left available
    alongside retrieved pages they get used anyway, which puts back the minutes of
    waiting and the run-to-run variation that reading from the corpus removes.
    """
    from app.services.badge_discovery import _client, _translate_auth_error

    settings = settings or get_settings()
    client = _client(settings)

    messages: list[dict] = [
        {
            "role": "user",
            "content": build_prompt(
                badges,
                count,
                source_material=source_material,
                extra_instructions=extra_instructions,
                corpus_pages=corpus_pages,
            ),
        }
    ]
    tools = (
        []
        if corpus_pages
        else [
            {"type": settings.web_search_tool, "name": "web_search"},
            {"type": settings.web_fetch_tool, "name": "web_fetch"},
        ]
    )
    draft: list[str] = []

    # Server-side web search can end a turn with stop_reason "pause_turn";
    # re-send to resume. Cap the resumes so a loop can't run away.
    for _ in range(6):
        try:
            with client.messages.stream(
                model=settings.model,
                max_tokens=32000,
                system=AUTHOR_SYSTEM,
                output_config={"effort": settings.effort},
                tools=tools,
                messages=messages,
            ) as stream:
                message = stream.get_final_message()
        except Exception as exc:
            _translate_auth_error(exc)
            raise

        draft.extend(b.text for b in message.content if b.type == "text")

        if message.stop_reason == "refusal":
            raise RuntimeError(
                f"Claude declined the authoring request: {message.stop_details}"
            )
        if message.stop_reason != "pause_turn":
            break

        messages.append({"role": "assistant", "content": message.content})
    else:
        raise RuntimeError("Authoring turn still paused after 6 resumes; giving up.")

    return "\n".join(draft).strip()


def extract_questions(
    draft: str, *, settings: Settings | None = None
) -> list[GeneratedQuestion]:
    """Turn drafted questions into validated question records."""
    from app.services.badge_discovery import _client, _translate_auth_error

    settings = settings or get_settings()

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=16000,
            system=EXTRACT_SYSTEM,
            output_format=GeneratedQuestions,
            messages=[{"role": "user", "content": f"Drafted questions:\n\n{draft}"}],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise
    if response.parsed_output is None:
        raise RuntimeError(
            f"Extraction produced no structured output (stop_reason="
            f"{response.stop_reason}, details={response.stop_details})."
        )
    return response.parsed_output.questions


def format_problem(question: GeneratedQuestion) -> str | None:
    """Why this question is not usable as written, or None if it is fine.

    The quiz format is fixed: four options, exactly one correct, no repeats, and a
    stem that asks something. A question failing any of those cannot be published
    however good its subject matter, so it is rejected here rather than stored and
    discovered by a reviewer later.
    """
    if not question.stem.strip():
        return "the stem is empty"
    if len(question.options) != OPTIONS_PER_QUESTION:
        return f"{len(question.options)} options, expected {OPTIONS_PER_QUESTION}"
    correct = sum(1 for o in question.options if o.is_correct)
    if correct != 1:
        return f"{correct} options marked correct, expected exactly 1"
    texts = [o.text.strip().casefold() for o in question.options]
    if any(not t for t in texts):
        return "an option has no text"
    if len(set(texts)) != len(texts):
        return "two options are the same"
    return None


def split_well_formed(
    questions: list[GeneratedQuestion],
) -> tuple[list[GeneratedQuestion], list[dict[str, Any]]]:
    """Split questions into those fit to store and those to report as rejected.

    This is the deterministic backstop for the format rules the prompt states.
    Rejections are reported with a reason, not dropped silently: a run that
    quietly stored three of ten questions would look like a model that had little
    to say.
    """
    kept, rejected = [], []
    for question in questions:
        problem = format_problem(question)
        if problem is None:
            kept.append(question)
        else:
            rejected.append({"stem": question.stem, "problem": problem})
    return kept, rejected


def _drop_unknown_badges(
    questions: list[GeneratedQuestion], known_slugs: set[str], requested: list[str]
) -> None:
    """Remove badge slugs that no stored badge answers to.

    `skill_badges` is what the whole collection is filtered by, so a hallucinated
    or mis-spelled slug would make a question unfindable under any real badge while
    looking correctly tagged. A question left with no badge at all is attributed to
    the badges the run was scoped to, which is where it came from.

    Note this does not restrict a question to the badges that were requested: a
    question may legitimately belong to others, which `attribute_badges` decides.
    """
    for question in questions:
        known = [s for s in question.skill_badges if s in known_slugs]
        # dict.fromkeys rather than set(), to keep the order stable for review.
        question.skill_badges = list(dict.fromkeys(known)) or list(requested)


def _question_brief(index: int, question: GeneratedQuestion) -> str:
    """One question as the attribution pass sees it: stem, options and explanation."""
    lines = [f"Question {index}:", f"  Stem: {question.stem}"]
    for option in question.options:
        mark = "correct" if option.is_correct else "wrong"
        lines.append(f"  Option ({mark}): {option.text}")
    if question.explanation:
        lines.append(f"  Explanation: {question.explanation}")
    if question.categories:
        lines.append(f"  Topic areas: {', '.join(question.categories)}")
    lines.append(f"  Written for: {', '.join(question.skill_badges)}")
    return "\n".join(lines)


def build_attribution_prompt(
    questions: list[GeneratedQuestion], catalog: list[dict[str, Any]]
) -> str:
    """Assemble the attribution request. Kept separate so it can be asserted on."""
    badge_lines = []
    for badge in catalog:
        description = badge.get("description") or ""
        badge_lines.append(f"- {badge['slug']}: {badge.get('name')} — {description}")
    question_lines = [
        _question_brief(index, question) for index, question in enumerate(questions, 1)
    ]
    return (
        "Every MongoDB skill badge, by slug:\n"
        + "\n".join(badge_lines)
        + "\n\nThe questions to catalogue:\n"
        + "\n\n".join(question_lines)
    )


def attribute_badges(
    questions: list[GeneratedQuestion],
    catalog: list[dict[str, Any]],
    requested: list[str],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Add every other badge each question genuinely tests.

    Skills overlap, so a question written for one badge often serves others. Left
    tagged only with the badge it was requested for, that question is invisible to
    an author working through any of the badges it also covers — so the same
    question gets written again.

    The pass reads the whole question, answers included: what a question actually
    tests is often only visible in what separates the correct option from the wrong
    ones. Slugs are checked against the catalog, and the requested badges are always
    kept, so this can only add findability, never remove it.
    """
    from app.services.badge_discovery import _client, _translate_auth_error

    if not questions or not catalog:
        return {"cross_tagged": 0, "attribution_error": None}

    settings = settings or get_settings()
    known = {badge["slug"] for badge in catalog}
    before = [list(q.skill_badges) for q in questions]

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=16000,
            system=ATTRIBUTE_SYSTEM,
            output_format=QuestionBadgeAttributions,
            messages=[
                {
                    "role": "user",
                    "content": build_attribution_prompt(questions, catalog),
                }
            ],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise
    if response.parsed_output is None:
        raise RuntimeError(
            f"Badge attribution produced no structured output (stop_reason="
            f"{response.stop_reason}, details={response.stop_details})."
        )

    reasons = []
    for attribution in response.parsed_output.attributions:
        # 1-based indexing in the prompt, so an out-of-range answer is ignored
        # rather than silently retagging the wrong question.
        position = attribution.question_index - 1
        if not 0 <= position < len(questions):
            logger.warning(
                "Badge attribution named question %d, which was not sent",
                attribution.question_index,
            )
            continue
        question = questions[position]
        # The badges the question was written for are never dropped here.
        merged = list(question.skill_badges) + [
            slug for slug in attribution.skill_badges if slug in known
        ]
        question.skill_badges = list(dict.fromkeys(merged))
        added = [s for s in question.skill_badges if s not in before[position]]
        if added and attribution.reason:
            reasons.append({"stem": question.stem, "added": added, "reason": attribution.reason})

    cross_tagged = sum(
        1
        for index, question in enumerate(questions)
        if set(question.skill_badges) != set(before[index])
    )
    logger.info(
        "Badge attribution: %d of %d question(s) gained a badge beyond %s",
        cross_tagged,
        len(questions),
        ", ".join(requested),
    )
    return {
        "cross_tagged": cross_tagged,
        "attribution_reasons": reasons,
        "attribution_error": None,
    }


def generate_questions(
    slugs: list[str],
    count: int,
    *,
    source_material: str | None = None,
    extra_instructions: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Author questions for the named badges and store the well-formed ones."""
    from app.repositories import questions as questions_repo
    from app.repositories import skill_badges
    from app.services import doc_retrieval

    if not slugs:
        raise ValueError("Select at least one skill badge to generate questions for.")

    catalog = skill_badges.list_badges()
    known = {b["slug"]: b for b in catalog}
    missing = [s for s in slugs if s not in known]
    if missing:
        raise ValueError(f"No skill badge with slug(s): {', '.join(sorted(missing))}.")
    badges = [known[s] for s in slugs]

    corpus_pages = doc_retrieval.pages_for_badges(badges, settings=settings)
    if corpus_pages:
        logger.info(
            "Authoring from %d stored documentation page(s) for %s",
            len(corpus_pages),
            ", ".join(slugs),
        )
    else:
        logger.warning(
            "No stored documentation matched %s; authoring will research the web. "
            "Refresh the documentation corpus to make runs faster and repeatable.",
            ", ".join(slugs),
        )

    draft = author_questions(
        badges,
        count,
        source_material=source_material,
        extra_instructions=extra_instructions,
        corpus_pages=corpus_pages,
        settings=settings,
    )
    generated = extract_questions(draft, settings=settings)
    _drop_unknown_badges(generated, set(known), slugs)
    kept, rejected = split_well_formed(generated)

    # Only the questions being stored are worth cataloguing, so attribution runs
    # after the format check rather than before it.
    attribution = _attribute_or_keep_going(kept, catalog, slugs, settings)

    summary = questions_repo.insert_questions(kept)
    summary["source"] = "question-generation"
    summary["requested"] = count
    summary["generated"] = len(generated)
    summary["rejected"] = rejected
    summary["skill_badges"] = slugs
    summary["draft"] = draft
    # What the run read, so a question's source is answerable after the fact and an
    # empty corpus is visible as the reason a run was slow.
    summary["source_pages"] = [
        {"url": p["url"], "title": p["title"]} for p in corpus_pages
    ]
    summary["researched_the_web"] = not corpus_pages
    summary["questions"] = [q.model_dump() for q in kept]
    summary.update(attribution)
    return summary


def _attribute_or_keep_going(
    questions: list[GeneratedQuestion],
    catalog: list[dict[str, Any]],
    requested: list[str],
    settings: Settings | None,
) -> dict[str, Any]:
    """Run the attribution pass, but never let it lose a finished batch.

    By this point the expensive work is done and the questions are good. If
    cataloguing them across badges fails — a rate limit, a truncated response — the
    right outcome is to store them under the badges they were written for and say
    so, not to discard an authoring run over a follow-up step.
    """
    try:
        return attribute_badges(questions, catalog, requested, settings=settings)
    except Exception as exc:
        logger.warning(
            "Badge attribution failed; questions keep their requested badges: %s", exc
        )
        return {"cross_tagged": 0, "attribution_error": str(exc)}


# --- the badge-scoped page walk ---


def build_page_prompt(
    page: dict[str, Any],
    badge: dict[str, Any],
    catalog: list[dict[str, Any]],
    count: int,
    *,
    difficulty: str | None = None,
    extra_instructions: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Assemble the request for one page. Kept separate so it can be asserted on."""
    settings = settings or get_settings()
    badge_lines = [
        f"- {b['slug']}: {b.get('name')} — {b.get('description') or ''}" for b in catalog
    ]
    prompt = (
        f"Write up to {count} question(s) from the documentation page below.\n\n"
        f"The page was selected for this badge:\n{_badge_brief(badge)}\n\n"
        "Every MongoDB skill badge, by slug — file each question under every one it "
        "genuinely tests:\n" + "\n".join(badge_lines) + "\n\n"
    )
    if difficulty:
        prompt += DIFFICULTY_GUIDANCE.get(difficulty, "") + "\n\n"
    else:
        prompt += (
            "Difficulty is yours to judge per question — let the material decide, and "
            "spread the questions across foundational, intermediate and advanced "
            "rather than pitching them all the same.\n\n"
        )
    if extra_instructions:
        prompt += f"Additional instructions from the author:\n{extra_instructions}\n\n"

    # Capped, and the cap matters more than it looks: the corpus holds pages up to 1.7 MB
    # — driver tutorials that repeat every example in a dozen languages — and one of them
    # sent whole is half a million input tokens, about $2.50 for a single page. Measured
    # on a real run, 2026-08-19. A page cut short is still usable material to write from;
    # a page that costs more than the rest of the badge put together is not.
    text = page.get("text") or ""
    limit = settings.doc_context_page_chars
    truncated = len(text) > limit
    body = text[:limit]
    header = f"### {page.get('title') or page['url']}\nSource: {page['url']}"
    if truncated:
        header += (
            "\n(This page is longer than shown; it has been cut short. Write only from "
            "what is here — do not assume what the rest says.)"
        )
    return prompt + f"{header}\n\n{body}"


def build_chunk_prompt(
    chunk: dict[str, Any],
    badge: dict[str, Any],
    catalog: list[dict[str, Any]],
    count: int,
    *,
    difficulty: str | None = None,
    extra_instructions: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Assemble the request for one documentation section.

    The heading path is given as well as the text, because a section read out of its
    page needs it: "Limitations" says nothing, and "Atlas Vector Search > Filtering >
    Limitations" says what is being limited. The model is also told this is a section
    rather than a whole page, so it does not write a question whose answer depends on
    material that was never in front of it.
    """
    settings = settings or get_settings()
    badge_lines = [
        f"- {b['slug']}: {b.get('name')} — {b.get('description') or ''}" for b in catalog
    ]
    prompt = (
        f"Write up to {count} question(s) from the documentation section below.\n\n"
        f"The section was selected for this badge:\n{_badge_brief(badge)}\n\n"
        "Every MongoDB skill badge, by slug — file each question under every one it "
        "genuinely tests:\n" + "\n".join(badge_lines) + "\n\n"
    )
    if difficulty:
        prompt += DIFFICULTY_GUIDANCE.get(difficulty, "") + "\n\n"
    else:
        prompt += (
            "Difficulty is yours to judge per question — let the material decide, and "
            "spread the questions across foundational, intermediate and advanced "
            "rather than pitching them all the same.\n\n"
        )
    if extra_instructions:
        prompt += f"Additional instructions from the author:\n{extra_instructions}\n\n"

    trail = " > ".join(
        part
        for part in [chunk.get("page_title") or "", *(chunk.get("heading_path") or [])]
        if part
    )
    heading = chunk.get("heading")
    if heading and heading not in trail:
        trail = f"{trail} > {heading}" if trail else heading
    return prompt + (
        "This is ONE SECTION of a documentation page, not the whole page. Write only "
        "from what is here — if a question would need material from elsewhere on the "
        "page, ask a different question.\n\n"
        f"Page: {chunk.get('page_title') or chunk['url']}\n"
        f"Section: {trail or 'the page opening'}\n"
        f"Source: {chunk['url']}\n\n"
        f"{chunk.get('text') or ''}"
    )


def questions_from_chunk(
    chunk: dict[str, Any],
    badge: dict[str, Any],
    catalog: list[dict[str, Any]],
    *,
    count: int | None = None,
    difficulty: str | None = None,
    extra_instructions: str | None = None,
    settings: Settings | None = None,
) -> PageResult:
    """Questions this one section supports, structured, in a single Claude call."""
    from app.services.badge_discovery import _client, _translate_auth_error

    settings = settings or get_settings()
    count = count or settings.questions_per_page

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=16000,
            system=PAGE_AUTHOR_SYSTEM,
            output_format=GeneratedQuestions,
            output_config={"effort": settings.page_author_effort},
            messages=[
                {
                    "role": "user",
                    "content": build_chunk_prompt(
                        chunk,
                        badge,
                        catalog,
                        count,
                        difficulty=difficulty,
                        extra_instructions=extra_instructions,
                        settings=settings,
                    ),
                }
            ],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise
    if response.parsed_output is None:
        raise RuntimeError(
            f"Section authoring produced no structured output (stop_reason="
            f"{response.stop_reason}, details={response.stop_details})."
        )
    return PageResult(response.parsed_output.questions, getattr(response, "usage", None))


@dataclass
class PageResult:
    """What one page produced, and what it cost to produce.

    Usage travels with the questions rather than being read back from the client,
    because a walk needs to attribute spend to the page that incurred it — that is
    what makes "stop, this is getting expensive" an informed decision.
    """

    questions: list[GeneratedQuestion]
    usage: Any = None


def questions_from_page(
    page: dict[str, Any],
    badge: dict[str, Any],
    catalog: list[dict[str, Any]],
    *,
    count: int | None = None,
    difficulty: str | None = None,
    extra_instructions: str | None = None,
    settings: Settings | None = None,
) -> PageResult:
    """Questions this one page supports, structured, in a single Claude call.

    One pass, not the draft-then-extract pair the badge-wide path uses. That pair
    exists because a research turn benefits from thinking in prose before being made
    structured, and because tool use and structured output did not sit well together.
    Reading one page needs no tools and no research, so the second pass would only pay
    output tokens a second time to restate questions already written.

    Badge attribution is folded in for the same reason: the catalog is small, the model
    is already holding the question, and a separate pass would re-send every question
    to decide something it could have decided when it wrote it.
    """
    from app.services.badge_discovery import _client, _translate_auth_error

    settings = settings or get_settings()
    count = count or settings.questions_per_page

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=16000,
            system=PAGE_AUTHOR_SYSTEM,
            output_format=GeneratedQuestions,
            output_config={"effort": settings.page_author_effort},
            messages=[
                {
                    "role": "user",
                    "content": build_page_prompt(
                        page,
                        badge,
                        catalog,
                        count,
                        difficulty=difficulty,
                        extra_instructions=extra_instructions,
                        settings=settings,
                    ),
                }
            ],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise
    if response.parsed_output is None:
        raise RuntimeError(
            f"Page authoring produced no structured output (stop_reason="
            f"{response.stop_reason}, details={response.stop_details})."
        )
    return PageResult(response.parsed_output.questions, getattr(response, "usage", None))


def generate_for_badge(
    slug: str,
    *,
    max_pages: int | None = None,
    questions_per_page: int | None = None,
    difficulty: str | None = None,
    extra_instructions: str | None = None,
    settings: Settings | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Write questions for one badge by walking the pages that badge is about.

    The badge is resolved to its page set, the pages already written from are dropped,
    and what is left is read one page at a time. Each page's questions are stored as
    they are written rather than at the end: a walk runs for many minutes, and a run
    that failed on page 18 should keep the questions from the first 17.

    Progress is reported per page, and a failing page is recorded and stepped over.
    One unreadable page, or one refusal, is not a reason to abandon a walk.
    """
    from app.repositories import questions as questions_repo
    from app.repositories import skill_badges
    from app.services import doc_retrieval

    settings = settings or get_settings()
    max_pages = max_pages or settings.max_pages_per_run
    questions_per_page = questions_per_page or settings.questions_per_page

    catalog = skill_badges.list_badges()
    known = {b["slug"]: b for b in catalog}
    if slug not in known:
        raise ValueError(f"No skill badge with slug {slug!r}.")
    badge = known[slug]

    started = time.monotonic()
    cost = RunCost(_settings=settings)

    summary: dict[str, Any] = {
        "source": "badge-page-walk",
        "run_id": uuid4().hex,
        # The choices the author made, kept with the result. Weeks later "why did this
        # badge only get 12 questions" is answerable from the request, not the outcome.
        "requested": {
            "max_pages": max_pages,
            "questions_per_page": questions_per_page,
            "difficulty": difficulty,
            "extra_instructions": extra_instructions,
        },
        "model": settings.model,
        "effort": settings.page_author_effort,
        "score_floor": settings.doc_page_set_score_floor,
        "skill_badge": slug,
        "badge_name": badge.get("name"),
        "skill_badges": [slug],
        # "resolving" until the page set is known: the walk genuinely cannot say how
        # much work there is yet, and a confident 0% would be a lie.
        "phase": "resolving",
        "pages_available": None,
        "pages_already_used": 0,
        "pages_total": 0,
        "pages_done": 0,
        "current_page": None,
        "questions_per_page": questions_per_page,
        "inserted": 0,
        "generated": 0,
        "rejected": [],
        "failures": [],
        "source_pages": [],
        "question_ids": [],
        "stopped_early": False,
    }

    def report() -> None:
        if not progress:
            return
        elapsed = time.monotonic() - started
        done = summary["pages_done"]
        total = summary["pages_total"]
        rate = done / elapsed if elapsed > 0 and done else 0.0
        state = dict(summary)
        state["elapsed_seconds"] = round(elapsed, 1)
        state["pages_per_minute"] = round(rate * 60, 1)
        state["questions_per_page_actual"] = (
            round(summary["inserted"] / done, 1) if done else None
        )
        # The throughput figure an author actually plans against: pages per minute is
        # an implementation detail, questions per minute is the output.
        state["questions_per_minute"] = (
            round(summary["inserted"] / elapsed * 60, 1)
            if elapsed > 0 and summary["inserted"]
            else None
        )
        # No confident estimate before any page has finished: "0%, 0 seconds left" is
        # worse than saying nothing.
        state["percent"] = round(done / total * 100, 1) if total else None
        state["eta_seconds"] = round((total - done) / rate) if rate and total else None
        state["cost"] = cost.snapshot(done, total, summary["inserted"])
        progress(state)

    report()

    already = questions_repo.source_chunk_ids_for_badge(slug)
    page_set = doc_retrieval.chunk_set_for_badge(
        badge, exclude_chunk_ids=already, settings=settings
    )
    summary["pages_available"] = len(page_set)
    summary["pages_already_used"] = len(already)
    summary["pages_total"] = min(len(page_set), max_pages)
    summary["phase"] = "writing"
    report()
    if not page_set:
        # Nothing left to walk. If the badge has never been walked, its material was
        # never in the corpus in the first place — so fall back to the single-prompt
        # path, which researches the web. If it has been walked, the material really is
        # used up, and the honest answer is to say so rather than research around it.
        if already:
            logger.info(
                "Every documentation page for %s has been written from already; "
                "nothing new to walk.",
                slug,
            )
            summary["exhausted"] = True
            summary["phase"] = "done"
            summary["failure_count"] = 0
            summary["cost"] = cost.snapshot()
            report()
            return summary
        logger.warning(
            "No documentation pages resolve to %s; falling back to a single "
            "research run. Refresh the documentation corpus to walk pages instead.",
            slug,
        )
        fallback = generate_questions(
            [slug],
            questions_per_page,
            extra_instructions=extra_instructions,
            settings=settings,
        )
        fallback["skill_badge"] = slug
        fallback["badge_name"] = badge.get("name")
        fallback["pages_available"] = 0
        fallback["pages_done"] = 0
        fallback["pages_total"] = 0
        fallback["fell_back_to_research"] = True
        fallback["phase"] = "done"
        fallback["failure_count"] = 0
        fallback["cost"] = cost.snapshot()
        return fallback

    for page_ref in page_set[:max_pages]:
        if stop and stop():
            summary["stopped_early"] = True
            break

        # Named before the work starts, so the panel says which page is being read
        # rather than only how many have finished.
        summary["current_page"] = {
            "url": page_ref["url"],
            # The heading, not the page title: on a walk through sections, "Failover"
            # says where the run is and the page title repeats for a dozen of them.
            "title": page_ref.get("heading") or page_ref.get("page_title"),
        }
        report()
        page = doc_chunk(page_ref["chunk_id"])
        if page is None:
            summary["failures"].append(
                {"url": page_ref["url"], "error": "section is no longer in the corpus"}
            )
            summary["pages_done"] += 1
            report()
            continue

        try:
            result = questions_from_chunk(
                page,
                badge,
                catalog,
                count=questions_per_page,
                difficulty=difficulty,
                extra_instructions=extra_instructions,
                settings=settings,
            )
            written = result.questions
            cost.add(result.usage)
        except Exception as exc:
            # Recorded per page, not raised: a badge's walk is worth more than any one
            # page in it, and a refusal on one page says nothing about the next.
            logger.warning(
                "Section %s produced no questions: %s", page_ref["chunk_id"], exc
            )
            summary["failures"].append({"url": page_ref["url"], "error": str(exc)})
            summary["pages_done"] += 1
            report()
            continue

        _drop_unknown_badges(written, set(known), [slug])
        _ensure_source_url(written, page["url"])
        _stamp_source_chunk(written, page)
        kept, rejected = split_well_formed(written)
        # Stamped with the walk's id, not a fresh one per page: the question a reviewer
        # is looking at has to lead back to the run that wrote it, and a walk is one run
        # however many pages it took.
        stored = questions_repo.insert_questions(kept, run_id=summary["run_id"])

        summary["generated"] += len(written)
        summary["inserted"] += stored["inserted"]
        summary["question_ids"].extend(stored["question_ids"])
        summary["rejected"].extend(rejected)
        summary["source_pages"].append(
            {
                "url": page["url"],
                "chunk_id": page.get("chunk_id"),
                "title": page.get("heading") or page.get("page_title"),
                "heading_path": page.get("heading_path") or [],
                "questions": stored["inserted"],
                "bytes": page.get("bytes"),
                "chars": page.get("chars"),
            }
        )
        summary["pages_done"] += 1
        report()

    summary["phase"] = "stopped" if summary["stopped_early"] else "done"
    summary["current_page"] = None
    summary["failure_count"] = len(summary["failures"])
    summary["elapsed_seconds"] = round(time.monotonic() - started, 1)
    summary["cost"] = cost.snapshot(
        summary["pages_done"], summary["pages_total"], summary["inserted"]
    )
    # From the raw clock, not the rounded figure on the summary: a fast run rounds to
    # 0.0 seconds, and dividing by that reports no rate for a run that had one.
    elapsed = time.monotonic() - started
    summary["questions_per_minute"] = (
        round(summary["inserted"] / elapsed * 60, 1)
        if elapsed > 0 and summary["inserted"]
        else None
    )
    summary["percent"] = (
        round(summary["pages_done"] / summary["pages_total"] * 100, 1)
        if summary["pages_total"]
        else None
    )
    report()
    logger.info(
        "Badge walk finished for %s: %d question(s) from %d page(s), %d page(s) failed",
        slug,
        summary["inserted"],
        summary["pages_done"],
        len(summary["failures"]),
    )
    return summary


def doc_pages_page(url: str) -> dict[str, Any] | None:
    """One page by URL. Indirected so a test can stand in for the corpus."""
    from app.repositories import doc_pages

    return doc_pages.page_by_url(url)


def doc_chunk(chunk_id: str) -> dict[str, Any] | None:
    """One documentation section by id. Indirected so a test can stand in for it."""
    from app.repositories import doc_chunks

    return doc_chunks.get_chunk(chunk_id)


def _stamp_source_chunk(questions: list[GeneratedQuestion], chunk: dict[str, Any]) -> None:
    """Record which section a question came from, beyond the page it cites.

    The citation stays a page URL, because that is what a reader opens and what the
    renderer can fetch. Resumability needs finer grain than that: a page has several
    sections and walking it again must skip only the ones already used, so the section
    is recorded separately rather than folded into the URL as a fragment.
    """
    for question in questions:
        question.source_chunk_ids = [chunk["chunk_id"]]


def _ensure_source_url(questions: list[GeneratedQuestion], url: str) -> None:
    """Guarantee every question cites the page it was written from.

    The prompt asks for it, but a citation is the only way a reviewer can check a
    question without re-reading the corpus — and it is what makes the walk resumable,
    since "pages already written from" is derived from these URLs. Too load-bearing to
    leave to the model remembering.
    """
    for question in questions:
        if url not in question.source_urls:
            question.source_urls = [url, *question.source_urls]
