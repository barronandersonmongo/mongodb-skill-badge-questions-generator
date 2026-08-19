"""Tests for app/repositories/runs.py — the record of what each run did.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

from app.repositories import runs


def summary(run_id: str = "r1", **overrides) -> dict:
    return {
        "run_id": run_id,
        "source": "badge-page-walk",
        "skill_badges": ["atlas-search"],
        "badge_name": "Atlas Search",
        "pages_done": 3,
        "inserted": 9,
        "elapsed_seconds": 120.0,
        "cost": {"dollars": 0.25},
        "requested": {"max_pages": 25, "questions_per_page": 3},
        **overrides,
    }


def test_a_finished_run_is_recorded(fake_runs):
    """
    Intent: Run state is one in-process dict and loses everything on restart. The token
        counts and the wall clock are unrecoverable after the fact, so a run that is not
        written down cannot be reflected on later — which is the whole point of asking
        whether a prompt change helped.
    Success: A recorded run is retrievable by its id.
    Feature: Run history — finished runs are persisted.
    """
    runs.record_run(summary())
    stored = runs.get_run("r1")
    assert stored["inserted"] == 9
    assert stored["skill_badges"] == ["atlas-search"]


def test_the_choices_made_are_kept_with_the_result(fake_runs):
    """
    Intent: "Why did this badge only get twelve questions" is answerable from the request,
        not the outcome — the page cap, the questions-per-page and the instructions are
        what a later reader needs to interpret the counts.
    Success: The request's choices survive on the recorded run.
    Feature: Run history — the run's inputs are recorded, not just its output.
    """
    runs.record_run(summary(requested={"max_pages": 5, "questions_per_page": 2}))
    stored = runs.get_run("r1")
    assert stored["requested"] == {"max_pages": 5, "questions_per_page": 2}


def test_a_run_is_stamped_with_when_it_was_recorded(fake_runs):
    """
    Intent: A run's own timings come from the request clock; the recording time is what
        orders the history when a run's timings are missing, as they are for a run that
        failed before it started properly.
    Success: A recorded run carries a recorded_at timestamp.
    Feature: Run history — recording is timestamped.
    """
    runs.record_run(summary())
    assert runs.get_run("r1")["recorded_at"] is not None


def test_recording_the_same_run_twice_replaces_it(fake_runs):
    """
    Intent: A walk reports progress and then finishes, and a caller may record either. Two
        rows for one run would double every total the history screen shows.
    Success: Recording the same run_id twice leaves one record, the later one.
    Feature: Run history — one record per run.
    """
    runs.record_run(summary(inserted=1))
    runs.record_run(summary(inserted=9))
    assert len(fake_runs.docs) == 1
    assert runs.get_run("r1")["inserted"] == 9


def test_a_run_with_no_id_is_not_recorded(fake_runs):
    """
    Intent: Without an id a run cannot be replaced or looked up, so recording it would grow
        the collection with rows nothing can reach or correct.
    Success: A summary with no run_id is refused and reported as not stored.
    Feature: Run history — a run needs an identity.
    """
    assert runs.record_run({"source": "badge-page-walk"}) is None
    assert fake_runs.docs == []


def test_a_storage_failure_does_not_break_the_run(fake_runs, monkeypatch):
    """
    Intent: By the time a run is recorded its questions are already stored and the expensive
        work is done. Failing the run over bookkeeping would report a success as a failure,
        which is the worse of the two errors.
    Success: A raising collection is swallowed and reported as not recorded.
    Feature: Run history — recording never fails a run.
    """
    def boom():
        raise RuntimeError("no primary")

    monkeypatch.setattr(runs, "collection", boom)
    assert runs.record_run(summary()) is None


def test_runs_are_listed_newest_first(fake_runs):
    """
    Intent: The history is read to see what just happened and how it compares with last
        time, so the newest run has to be at the top — ordered any other way, the useful
        rows are at the bottom of a growing list.
    Success: Runs come back in descending order of when they finished.
    Feature: Run history — newest first.
    """
    runs.record_run(summary("old", finished_at=1000.0))
    runs.record_run(summary("new", finished_at=2000.0))
    assert [r["run_id"] for r in runs.list_runs()] == ["new", "old"]


def test_history_can_be_narrowed_to_one_badge(fake_runs):
    """
    Intent: The question worth asking of a single badge is whether it is getting better —
        which means comparing its runs with each other, not with every other badge's.
    Success: Filtering by badge returns only that badge's runs.
    Feature: Run history — filtered by badge.
    """
    runs.record_run(summary("a", skill_badges=["atlas-search"]))
    runs.record_run(summary("b", skill_badges=["indexing"]))
    listed = runs.list_runs(skill_badge="indexing")
    assert [r["run_id"] for r in listed] == ["b"]


def test_the_listing_leaves_out_the_bulky_detail(fake_runs):
    """
    Intent: A run that walked 200 pages carries the pages it read and every question it
        wrote. Carried into a listing of fifty runs that is megabytes nothing on the screen
        uses, on a request made from a modal.
    Success: Listed runs omit the per-page and per-question payloads, which the full record
        still has.
    Feature: Run history — listings are summaries.
    """
    runs.record_run(summary(source_pages=[{"url": "https://x/a.md"}], questions=[{"stem": "?"}]))
    listed = runs.list_runs()[0]
    assert "source_pages" not in listed and "questions" not in listed
    assert runs.get_run("r1")["source_pages"] == [{"url": "https://x/a.md"}]


def test_an_unknown_run_is_absent_rather_than_an_error(fake_runs):
    """
    Intent: A link to a run can outlive the run — a bookmarked id, or a record removed. The
        caller needs to tell "no such run" from "the store is broken", so absence has to be
        an answer rather than an exception.
    Success: Fetching an unknown id returns None.
    Feature: Run history — an unknown run is reported as absent.
    """
    assert runs.get_run("nope") is None


def test_the_history_totals_what_every_run_spent(fake_runs):
    """
    Intent: Per-run cost is small enough to ignore individually and large enough to matter
        in aggregate, which is exactly the shape of spending that goes unnoticed. The
        cumulative figure is the one worth putting at the top of the screen.
    Success: Totals sum the runs, questions, pages, dollars and seconds.
    Feature: Run history — cumulative totals.
    """
    runs.record_run(summary("a", inserted=9, pages_done=3, elapsed_seconds=60.0,
                            cost={"dollars": 0.25}))
    runs.record_run(summary("b", inserted=6, pages_done=2, elapsed_seconds=30.0,
                            cost={"dollars": 0.15}))
    totals = runs.totals()
    assert totals == {"runs": 2, "questions": 15, "pages": 5, "dollars": 0.4, "seconds": 90.0}


def test_totals_of_an_empty_history_are_zero(fake_runs):
    """
    Intent: The screen is opened before any run has been recorded, and an empty aggregation
        returns no rows at all. Read naively that is a KeyError on the first render.
    Success: With nothing recorded the totals are zeroes.
    Feature: Run history — an empty history totals cleanly.
    """
    assert runs.totals() == {"runs": 0, "questions": 0, "pages": 0, "dollars": 0.0, "seconds": 0.0}


def test_run_history_is_indexed_for_the_way_it_is_read(fake_runs):
    """
    Intent: The history is read newest-first and per badge, and a run is replaced by id on
        every recording. Unindexed those become collection scans, and a duplicate run_id
        would let one run appear twice in every total.
    Success: Indexes exist for run_id (unique), finished_at and skill_badges.
    Feature: Run history — queryable by recency and badge.
    """
    runs.ensure_indexes()
    by_name = {index["name"]: index for index in fake_runs.indexes}
    assert {"run_id_unique", "finished_at", "skill_badges"} <= by_name.keys()
    assert by_name["run_id_unique"]["unique"] is True
