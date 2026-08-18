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

from typing import Any

from app.config import Settings, get_settings
from app.models.question import GeneratedQuestion, GeneratedQuestions

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


def _scope_to_selected_badges(
    questions: list[GeneratedQuestion], slugs: list[str]
) -> None:
    """Keep each question's badge list inside the badges that were asked for.

    The model is told which badges are in scope, but the stored `skill_badges`
    array is what the whole collection is filtered by, so a hallucinated or
    mis-slugged badge there would make a question unfindable. Anything outside the
    request is dropped; a question left with none is attributed to the request.
    """
    allowed = set(slugs)
    for question in questions:
        inside = [s for s in question.skill_badges if s in allowed]
        question.skill_badges = inside or list(slugs)


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

    known = {b["slug"]: b for b in skill_badges.list_badges()}
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
    _scope_to_selected_badges(generated, slugs)
    kept, rejected = split_well_formed(generated)

    summary = questions_repo.insert_questions(kept)
    summary["source"] = "question-generation"
    summary["requested"] = count
    summary["generated"] = len(generated)
    summary["rejected"] = rejected
    summary["skill_badges"] = slugs
    summary["draft"] = draft
    summary["questions"] = [q.model_dump() for q in kept]
    return summary
