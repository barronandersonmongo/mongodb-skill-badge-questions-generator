"""Read a badge's title from its badge image.

The text titles on Credly and on learn.mongodb.com do not agree with each other,
and neither reliably matches the title printed on the badge artwork itself —
which is what a badge holder sees and what the quiz should be named after. The
artwork is therefore the authority for the title, and reading it needs vision.
"""

from pydantic import BaseModel, Field

from app.config import Settings, get_settings

TITLE_SYSTEM = """\
You read the title printed on a MongoDB skill badge image.

Report exactly the badge's own name as printed on the artwork. Transcribe it \
verbatim: keep the original capitalisation, punctuation and word order, and do \
not expand abbreviations, append a subtitle, or tidy the wording.

Ignore surrounding chrome that is not the badge's name — a "MongoDB Skill" or \
"Skill Badge" banner, an issuer name, a level or duration label, and any logo or \
icon. If the artwork shows no readable title, say so instead of guessing."""


class BadgeImageTitle(BaseModel):
    title: str = Field(
        description="The badge name exactly as printed on the artwork, or an empty "
        "string if no title is readable"
    )
    readable: bool = Field(
        description="False when the artwork carries no readable title, in which case "
        "the title must be empty"
    )


def read_title_from_image(
    image_url: str, *, settings: Settings | None = None
) -> str | None:
    """Return the title printed on the badge artwork, or None if unreadable."""
    settings = settings or get_settings()
    from app.services.badge_discovery import _client, _translate_auth_error

    try:
        response = _client(settings).messages.parse(
            model=settings.model,
            max_tokens=2000,
            system=TITLE_SYSTEM,
            output_format=BadgeImageTitle,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "url", "url": image_url}},
                        {"type": "text", "text": "What is this badge's title?"},
                    ],
                }
            ],
        )
    except Exception as exc:
        _translate_auth_error(exc)
        raise

    parsed = response.parsed_output
    if parsed is None:
        # A failed call is not the same as artwork without a title: reporting it as
        # "no title" would silently keep the catalog title while claiming the
        # artwork had been read.
        raise RuntimeError(
            f"Reading the badge title produced no structured output (stop_reason="
            f"{response.stop_reason}) for {image_url}."
        )
    if not parsed.readable:
        return None
    title = parsed.title.strip()
    return title or None
