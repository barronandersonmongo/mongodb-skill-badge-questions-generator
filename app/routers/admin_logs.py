"""JSON endpoint behind the log viewer.

Only the active log file is served. That is what "the most recent log" means, and
it keeps the viewer from becoming a way to read arbitrary paths off the server —
there are no authorizations here, so the endpoint must not accept a path at all.
"""

import logging

from fastapi import APIRouter, Query

from app.logging_config import (
    DEFAULT_LINES,
    MAX_LINES,
    log_file_path,
    read_recent,
    rotated_files,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/logs", tags=["admin"])


@router.get("")
def recent_log(
    lines: int = Query(default=DEFAULT_LINES, ge=1, le=MAX_LINES)
) -> dict:
    """The tail of the active log file, plus what rotated history exists."""
    return {
        "file": str(log_file_path()),
        "lines": read_recent(lines),
        "rotated": rotated_files(),
        "max_lines": MAX_LINES,
    }
