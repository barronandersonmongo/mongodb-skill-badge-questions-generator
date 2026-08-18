"""Generate skill badge quiz questions with Claude.

Two passes, mirroring badge discovery:

1. Authoring — Claude reads the selected badges, any material the author pasted
   in, and the badges' reference links (server-side web search and fetch), then
   drafts questions as prose. Streaming, because a research turn runs for minutes.
2. Extraction — the draft is turned into validated structured output.

The badges scope the questions; they are not the whole source. What a badge
document supplies is its title, what it covers, its topic areas and its curated
links — enough to keep a question inside the badge's syllabus. Deeper material
(internal training content, live cluster behaviour) is passed in by the author as
`source_material` for now: this process has no Glean or MongoDB MCP access of its
own, so that content is pasted rather than fetched.
"""

import logging
from typing import Any

from app.config import Settings, get_settings
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
) -> str:
    """Assemble the authoring request. Kept separate so it can be asserted on."""
    briefs = "\n".join(_badge_brief(b) for b in badges)
    prompt = (
        f"Write {count} multiple-choice question(s) for the following skill "
        f"badge(s).\n\n{briefs}\n\n"
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
    settings: Settings | None = None,
) -> str:
    """Run the authoring pass. Returns Claude's draft questions as prose."""
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
            ),
        }
    ]
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
                tools=[
                    {"type": settings.web_search_tool, "name": "web_search"},
                    {"type": settings.web_fetch_tool, "name": "web_fetch"},
                ],
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

    if not slugs:
        raise ValueError("Select at least one skill badge to generate questions for.")

    catalog = skill_badges.list_badges()
    known = {b["slug"]: b for b in catalog}
    missing = [s for s in slugs if s not in known]
    if missing:
        raise ValueError(f"No skill badge with slug(s): {', '.join(sorted(missing))}.")
    badges = [known[s] for s in slugs]

    draft = author_questions(
        badges,
        count,
        source_material=source_material,
        extra_instructions=extra_instructions,
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
