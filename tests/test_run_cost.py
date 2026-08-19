"""Tests for app/services/run_cost.py — what a run has spent.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from dataclasses import replace

from app.services.run_cost import RunCost


def test_cost_is_priced_from_the_tokens_actually_reported(settings):
    """
    Intent: Cost is reported, not estimated. Every response carries its token counts, so a
        run can price exactly what it consumed — an estimate from page sizes would drift
        the moment thinking tokens moved, and thinking is most of what a walk spends.
    Success: Input and output tokens are priced at the configured per-million rates.
    Feature: Run cost — priced from reported token usage.
    """
    cost = RunCost(_settings=settings)
    cost.add({"input_tokens": 1_000_000, "output_tokens": 1_000_000})
    expected = settings.cost_input_per_mtok + settings.cost_output_per_mtok
    assert round(cost.dollars, 6) == round(expected, 6)


def test_cached_input_is_priced_at_its_own_rate(settings):
    """
    Intent: Cached reads cost about a tenth of fresh input and cache writes cost more than
        it. Priced as ordinary input, a run using the cache would be reported as costing
        roughly ten times what it did.
    Success: Cache reads and writes are priced at their own configured rates.
    Feature: Run cost — cached tokens priced separately.
    """
    cost = RunCost(_settings=settings)
    cost.add({"cache_read_input_tokens": 1_000_000})
    assert round(cost.dollars, 6) == round(settings.cost_cache_read_per_mtok, 6)

    write = RunCost(_settings=settings)
    write.add({"cache_creation_input_tokens": 1_000_000})
    assert round(write.dollars, 6) == round(settings.cost_cache_write_per_mtok, 6)


def test_usage_accumulates_across_calls(settings):
    """
    Intent: A walk is many calls, one per page. Reporting only the last one would show a
        rounding error where the run's total should be.
    Success: Two responses' usage adds up, and the call count reflects both.
    Feature: Run cost — accumulated across a whole run.
    """
    cost = RunCost(_settings=settings)
    cost.add({"input_tokens": 100, "output_tokens": 200})
    cost.add({"input_tokens": 300, "output_tokens": 400})
    assert cost.input_tokens == 400
    assert cost.output_tokens == 600
    assert cost.calls == 2


def test_a_response_without_usage_does_not_break_the_count(settings):
    """
    Intent: Usage arrives as an SDK object in production and a dict in tests, and a
        gateway-proxied response may omit the cache fields or the block entirely. None of
        that is a reason to lose the count of what was definitely spent, or to fail a run
        over bookkeeping.
    Success: A missing usage block and missing fields both count as zero.
    Feature: Run cost — tolerant of an incomplete usage block.
    """
    cost = RunCost(_settings=settings)
    cost.add(None)
    cost.add({"input_tokens": 50})
    assert cost.calls == 1
    assert cost.input_tokens == 50
    assert cost.output_tokens == 0


def test_usage_is_read_from_an_object_as_well_as_a_dict(settings):
    """
    Intent: The SDK returns usage as an attribute-bearing object, not a mapping. A reader
        that only understood dicts would report every real run as costing nothing, and the
        tests would still pass.
    Success: Usage exposed as attributes is priced the same as a dict.
    Feature: Run cost — reads the SDK's usage object.
    """
    class Usage:
        input_tokens = 1000
        output_tokens = 2000

    cost = RunCost(_settings=settings)
    cost.add(Usage())
    assert cost.input_tokens == 1000 and cost.output_tokens == 2000


def test_the_total_is_projected_from_what_is_done_so_far(settings):
    """
    Intent: "Spent $0.31" does not tell an author whether to let a walk run. "About $2.60
        by the end" does, and that is the decision the stop button exists for.
    Success: A quarter of the way through, the projection is four times the spend.
    Feature: Run cost — the projected total of a run in progress.
    """
    cost = RunCost(_settings=settings)
    cost.add({"input_tokens": 1_000_000})
    projected = cost.projected_dollars(done=5, total=20)
    assert round(projected, 6) == round(cost.dollars * 4, 6)


def test_nothing_is_projected_before_any_work_is_done(settings):
    """
    Intent: A projection from zero finished pages is a confident $0.00, which reads as
        "this run is free" at exactly the moment the author is deciding whether to start
        it. No number is better than a wrong one.
    Success: With nothing finished, the projection is None.
    Feature: Run cost — no projection without evidence.
    """
    cost = RunCost(_settings=settings)
    assert cost.projected_dollars(done=0, total=20) is None
    assert cost.snapshot(0, 20)["projected_dollars"] is None


def test_the_price_list_is_configurable(settings):
    """
    Intent: Published prices change and the model is configurable, so a hard-coded rate
        would quietly report the wrong number after either moved. The price is the only
        part of this that can be wrong.
    Success: Changing the configured rate changes the reported cost.
    Feature: Run cost — prices come from configuration.
    """
    dearer = replace(settings, cost_input_per_mtok=50.0)
    cost = RunCost(_settings=dearer)
    cost.add({"input_tokens": 1_000_000})
    assert round(cost.dollars, 6) == 50.0
