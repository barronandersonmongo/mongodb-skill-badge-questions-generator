"""File logging: one rotating log, and reading it back for the log viewer.

Configuration is read straight from the environment rather than from
`app.config.Settings`, because that requires a MongoDB connection string. Logging
has to work before — and especially when — the database does not, since "cannot
reach Atlas" is exactly the kind of thing an operator will come to the log to
find out.

The rotation budget is fixed: 100 MB per file, ten files, so the log occupies at
most ~1 GB and old entries are dropped rather than filling the disk. Python's
RotatingFileHandler counts the backups separately from the file it is writing, so
nine backups plus the active file is the ten files asked for.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 100 * 1024 * 1024  # 100 MB per file
TOTAL_FILES = 10               # the active file plus BACKUP_COUNT rotations
BACKUP_COUNT = TOTAL_FILES - 1
LOG_FILE_NAME = "app.log"
DEFAULT_LEVEL = "INFO"

# Rotated files are named app.log.1 … app.log.9 by the handler.
FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# Libraries that log one line per HTTP request at INFO. uvicorn already keeps an
# access log, so at INFO these only bury this application's own messages — which
# is what the log exists to carry. Raised to WARNING rather than silenced, so a
# failing request still appears.
NOISY_LOGGERS = ("httpx", "httpcore", "urllib3")

_configured = False


def log_directory() -> Path:
    """Where the log lives. Overridable so a deployment can place it elsewhere."""
    return Path(os.environ.get("LOG_DIR") or "logs")


def log_file_path() -> Path:
    return log_directory() / LOG_FILE_NAME


def configure_logging(*, force: bool = False) -> Path:
    """Attach the rotating file handler to the root logger, once.

    Idempotent: uvicorn's reloader imports the application repeatedly in one
    process, and adding the handler each time would write every line as many
    times as it was imported.
    """
    global _configured
    if _configured and not force:
        return log_file_path()

    path = log_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(os.environ.get("LOG_LEVEL") or DEFAULT_LEVEL)

    # Replace any handler this function added before, so `force` re-reads the
    # environment instead of accumulating handlers on the same file.
    for existing in list(root.handlers):
        if getattr(existing, "_skill_badge_file_handler", False):
            root.removeHandler(existing)
            existing.close()

    handler = RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(FORMAT))
    handler._skill_badge_file_handler = True
    root.addHandler(handler)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    return path


# --- reading the log back ---

# Read at most this much from the end of the file. A tail must not pull 100 MB
# into memory to show the last few hundred lines.
TAIL_BYTES = 4 * 1024 * 1024
DEFAULT_LINES = 500
MAX_LINES = 10000


def read_recent(lines: int = DEFAULT_LINES) -> list[str]:
    """The last `lines` lines of the active log file, oldest of them first.

    A missing file is not an error: nothing has been logged yet, which the viewer
    reports as an empty log rather than a failure.
    """
    path = log_file_path()
    if not path.exists():
        return []

    lines = max(1, min(lines, MAX_LINES))
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - TAIL_BYTES))
        chunk = handle.read()

    text = chunk.decode("utf-8", errors="replace")
    # A partial first line is likely when the read started mid-file; drop it
    # rather than showing a fragment as though it were a whole entry.
    if len(chunk) == TAIL_BYTES and "\n" in text:
        text = text.split("\n", 1)[1]
    return text.splitlines()[-lines:]


def rotated_files() -> list[dict]:
    """The rotated log files that exist, newest first, with their sizes.

    Listed so the viewer can say what history is on disk. Only the active file is
    served — that is what "the most recent log" means, and it keeps the viewer
    from becoming a way to read arbitrary paths.
    """
    found = []
    for index in range(1, BACKUP_COUNT + 1):
        path = log_directory() / f"{LOG_FILE_NAME}.{index}"
        if path.exists():
            found.append({"name": path.name, "bytes": path.stat().st_size})
    return found
