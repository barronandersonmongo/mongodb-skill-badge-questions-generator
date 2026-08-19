"""What a run has spent, from the token counts Claude reports.

Reported rather than estimated. Every Messages response carries a `usage` block, so
a run can add up exactly what it consumed and price it — the only thing that can be
wrong is the published price, which lives in `Settings` next to the model it applies
to. A guess derived from page sizes would drift the moment thinking tokens moved,
and thinking is most of what a page walk spends.

The projection is the other half: a walk of 25 pages is a commitment, and "spent
$0.31, about $2.60 by the end" is what tells an author whether to let it run or stop
it. Projected from cost per finished page, because pages vary in length and an
average over the pages already done is the best available predictor of the rest.
"""

from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings


@dataclass
class RunCost:
    """Accumulated token usage for one run, and the dollars it comes to."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    _settings: Settings | None = field(default=None, repr=False)

    def add(self, usage: Any) -> None:
        """Record one response's usage. Missing fields count as zero.

        Usage arrives as an SDK object here and as a plain dict in tests, and older
        or gateway-proxied responses may omit the cache fields entirely — none of
        which is a reason to lose the count of what was definitely spent.
        """
        if usage is None:
            return
        self.calls += 1
        self.input_tokens += _field(usage, "input_tokens")
        self.output_tokens += _field(usage, "output_tokens")
        self.cache_read_tokens += _field(usage, "cache_read_input_tokens")
        self.cache_write_tokens += _field(usage, "cache_creation_input_tokens")

    @property
    def dollars(self) -> float:
        settings = self._settings or get_settings()
        return (
            self.input_tokens * settings.cost_input_per_mtok
            + self.output_tokens * settings.cost_output_per_mtok
            + self.cache_read_tokens * settings.cost_cache_read_per_mtok
            + self.cache_write_tokens * settings.cost_cache_write_per_mtok
        ) / 1_000_000

    def projected_dollars(self, done: int, total: int) -> float | None:
        """What the whole run will cost at the rate it is going, or None if unknowable.

        Needs at least one finished unit of work: a projection from nothing is a
        confident zero, which is worse than showing nothing at all.
        """
        if not done or not total:
            return None
        return self.dollars / done * total

    def snapshot(self, done: int = 0, total: int = 0) -> dict[str, Any]:
        """The cost figures a status panel shows."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "calls": self.calls,
            "dollars": round(self.dollars, 4),
            "projected_dollars": _round_or_none(self.projected_dollars(done, total)),
        }


def _field(usage: Any, name: str) -> int:
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return int(value or 0)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 4)
