"""Persistence for the generation_runs collection — a record of every run.

Run state lives in a single in-process dict, which is enough to drive a screen while
a run is going and loses everything the moment the server restarts. That is fine for
"is it still working"; it is useless for "what did we do, and was it worth it".

So each finished run is written here: which badge, the choices the author made, how
long it took, what it produced, what it cost, which pages it read, and anything that
failed. Those are the things you want weeks later when deciding whether a prompt
change helped — and they are unrecoverable after the fact, because the token counts
and the wall clock are gone once the process is.

Kept separate from `questions`: a run is an event and a question is an artefact. They
have different lifetimes — deleting a bad batch of questions should not erase the
record that the batch was generated, since that record is the evidence for why the
prompt was changed afterwards.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.collection import Collection

from app.config import get_settings
from app.db import get_database

logger = logging.getLogger(__name__)

# Page text and per-question payloads are excluded from listings: a run that walked
# 200 pages carries megabytes that no summary row needs.
LIST_PROJECTION = {"_id": False, "source_pages": False, "questions": False}


def collection() -> Collection:
    return get_database()[get_settings().runs_collection]


def ensure_indexes() -> None:
    coll = collection()
    coll.create_index([("run_id", ASCENDING)], unique=True, name="run_id_unique")
    # The history screen reads newest first, and a badge's own history is the other
    # question worth asking ("has this badge got better since the prompt changed?").
    coll.create_index([("finished_at", DESCENDING)], name="finished_at")
    coll.create_index([("skill_badges", ASCENDING)], name="skill_badges")


def record_run(summary: dict[str, Any]) -> str | None:
    """Store one finished run. Returns its run_id, or None if it could not be stored.

    Never raises. A run that produced questions has already succeeded, and losing the
    bookkeeping is not a reason to report that success as a failure — so a storage
    problem here is logged and swallowed.
    """
    run_id = summary.get("run_id")
    if not run_id:
        logger.warning("Not recording a run with no run_id (source=%s)", summary.get("source"))
        return None
    try:
        ensure_indexes()
        document = {**summary, "recorded_at": datetime.now(timezone.utc)}
        collection().replace_one({"run_id": run_id}, document, upsert=True)
        return run_id
    except Exception as exc:
        logger.warning("Could not record run %s: %s", run_id, exc)
        return None


def list_runs(*, skill_badge: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Finished runs, newest first, without their bulky per-page detail."""
    query: dict[str, Any] = {}
    if skill_badge:
        query["skill_badges"] = skill_badge
    return list(
        collection()
        .find(query, LIST_PROJECTION)
        .sort("finished_at", DESCENDING)
        .limit(limit)
    )


def get_run(run_id: str) -> dict[str, Any] | None:
    """One run in full, including the pages it read. None if there is no such run."""
    return collection().find_one({"run_id": run_id}, {"_id": False})


def totals() -> dict[str, Any]:
    """What every recorded run adds up to.

    The number that matters at the top of a history screen is the cumulative spend:
    per-run cost is small enough to ignore individually and large enough to matter in
    aggregate, which is exactly the shape of cost that goes unnoticed.
    """
    pipeline = [
        {
            "$group": {
                "_id": None,
                "runs": {"$sum": 1},
                "questions": {"$sum": "$inserted"},
                "pages": {"$sum": "$pages_done"},
                "dollars": {"$sum": "$cost.dollars"},
                "seconds": {"$sum": "$elapsed_seconds"},
            }
        }
    ]
    rows = list(collection().aggregate(pipeline))
    if not rows:
        return {"runs": 0, "questions": 0, "pages": 0, "dollars": 0.0, "seconds": 0.0}
    row = rows[0]
    return {
        "runs": row.get("runs") or 0,
        "questions": row.get("questions") or 0,
        "pages": row.get("pages") or 0,
        "dollars": round(row.get("dollars") or 0.0, 4),
        "seconds": round(row.get("seconds") or 0.0, 1),
    }
