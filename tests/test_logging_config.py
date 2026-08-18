"""Tests for app/logging_config.py — the rotating log file and reading it back.

Every test carries an Intent / Success / Feature block. Those blocks are the
recorded requirement and are never edited: if behavior must change, the
program changes, or a new test is added alongside with its own block.
"""

import logging
from logging.handlers import RotatingFileHandler

import pytest

from app import logging_config


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    """Point logging at a temporary directory, and leave the root logger clean."""
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    yield tmp_path
    for handler in list(root.handlers):
        if handler not in before_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in before_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(before_level)
    logging_config._configured = False


def test_the_rotation_budget_is_a_hundred_megabytes_across_ten_files(log_dir):
    """
    Intent: The log has to be bounded or it fills the disk of whatever runs this tool.
        The agreed budget is 100 MB per file and ten files; RotatingFileHandler counts
        backups separately from the active file, so an off-by-one here silently keeps
        eleven files or nine.
    Success: The handler rotates at 100 MB and keeps nine backups, ten files in all.
    Feature: Logging — bounded, rotating log files.
    """
    logging_config.configure_logging(force=True)
    handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)
    ]
    assert len(handlers) == 1
    assert handlers[0].maxBytes == 100 * 1024 * 1024
    assert handlers[0].backupCount == 9
    assert logging_config.BACKUP_COUNT + 1 == logging_config.TOTAL_FILES == 10


def test_log_lines_reach_the_file(log_dir):
    """
    Intent: A log nothing is written to is worse than none, because it looks like a
        working log that reports no problems. The handler must actually be attached to
        the root logger, so a module logger anywhere in the app lands in the file.
    Success: A message logged through a named logger appears in the file.
    Feature: Logging — application output is captured.
    """
    logging_config.configure_logging(force=True)
    logging.getLogger("app.some.module").warning("a thing happened")
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = logging_config.log_file_path().read_text()
    assert "a thing happened" in contents
    assert "WARNING" in contents and "app.some.module" in contents


def test_configuring_twice_does_not_double_every_line(log_dir):
    """
    Intent: uvicorn's reloader imports the application repeatedly in one process. If
        each import added a handler, every line would be written as many times as the
        app had been imported, making the log unreadable.
    Success: After configuring twice, one line is written once.
    Feature: Logging — configuration is idempotent.
    """
    logging_config.configure_logging(force=True)
    logging_config.configure_logging()
    logging.getLogger("app.dup").error("only once")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert logging_config.log_file_path().read_text().count("only once") == 1


def test_the_log_directory_is_created_if_it_is_missing(tmp_path, monkeypatch, log_dir):
    """
    Intent: On a first run the directory does not exist. Failing to create it would
        crash the application at import, before it could log why.
    Success: Configuring logging into a missing subdirectory creates it.
    Feature: Logging — works on a first run.
    """
    target = tmp_path / "nested" / "logs"
    monkeypatch.setenv("LOG_DIR", str(target))
    logging_config.configure_logging(force=True)
    assert target.is_dir()


def test_the_log_location_is_configurable(tmp_path, monkeypatch, log_dir):
    """
    Intent: A deployment may not be able to write beside the code. The location must be
        settable from the environment, like every other setting in this program.
    Success: LOG_DIR determines where the log file is written.
    Feature: Logging — configurable location.
    """
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "elsewhere"))
    assert logging_config.log_file_path().parent == tmp_path / "elsewhere"


def test_the_log_level_is_configurable(tmp_path, monkeypatch, log_dir):
    """
    Intent: Diagnosing a bad generation run needs more detail than normal operation
        should pay for. The level has to be changeable without editing code.
    Success: LOG_LEVEL sets the root logger's level.
    Feature: Logging — configurable verbosity.
    """
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logging_config.configure_logging(force=True)
    assert logging.getLogger().level == logging.DEBUG


# --- reading the log back ---


def test_only_the_requested_number_of_lines_is_returned(log_dir):
    """
    Intent: The viewer asks for a tail. Returning the whole file would push a 100 MB
        response through the browser and hang the screen.
    Success: Asking for three lines returns the last three, oldest of them first.
    Feature: Log viewer — bounded tail.
    """
    logging_config.log_file_path().write_text("one\ntwo\nthree\nfour\nfive\n")
    assert logging_config.read_recent(3) == ["three", "four", "five"]


def test_the_newest_entries_are_the_ones_kept(log_dir):
    """
    Intent: An operator opens the log to see what just happened. A tail that returned
        the oldest lines would answer a question nobody asked.
    Success: The last line of the file is the last line returned.
    Feature: Log viewer — shows the most recent entries.
    """
    logging_config.log_file_path().write_text("old\nnewest\n")
    assert logging_config.read_recent(1) == ["newest"]


def test_an_absent_log_file_reads_as_empty_rather_than_failing(log_dir):
    """
    Intent: Before anything is logged there is no file. That is an empty log, not an
        error — the viewer must be able to say so instead of showing a stack trace.
    Success: read_recent returns an empty list when the file does not exist.
    Feature: Log viewer — first-run state.
    """
    assert not logging_config.log_file_path().exists()
    assert logging_config.read_recent() == []


def test_a_huge_log_is_not_read_into_memory_whole(log_dir, monkeypatch):
    """
    Intent: The file is allowed to reach 100 MB. Reading it all to show the last few
        hundred lines would spike memory on every viewer refresh.
    Success: Only the tail window is read from disk, and the requested lines still come
        back.
    Feature: Log viewer — reads only the end of the file.
    """
    monkeypatch.setattr(logging_config, "TAIL_BYTES", 64)
    logging_config.log_file_path().write_text("".join(f"line {i}\n" for i in range(500)))
    recent = logging_config.read_recent(2)
    assert recent == ["line 498", "line 499"]


def test_a_partial_first_line_is_not_shown_as_an_entry(log_dir, monkeypatch):
    """
    Intent: Reading a fixed window from the end usually starts mid-line. Presenting that
        fragment as a log entry would show a truncated, misleading message at the top of
        the viewer.
    Success: The fragment is dropped, so no returned line is a partial one.
    Feature: Log viewer — whole entries only.
    """
    monkeypatch.setattr(logging_config, "TAIL_BYTES", 20)
    logging_config.log_file_path().write_text("aaaaaaaaaaaaaaa\nbbbb\ncccc\n")
    assert all(line in ("bbbb", "cccc") for line in logging_config.read_recent(50))


def test_a_request_for_more_lines_than_allowed_is_capped(log_dir):
    """
    Intent: The line count comes from the browser. An unbounded value would let one
        request pull the entire file, defeating the tail.
    Success: A request far above the cap returns at most MAX_LINES lines.
    Feature: Log viewer — the tail cannot be widened without limit.
    """
    logging_config.log_file_path().write_text("".join(f"{i}\n" for i in range(20)))
    assert len(logging_config.read_recent(10**9)) <= logging_config.MAX_LINES


def test_undecodable_bytes_do_not_break_the_viewer(log_dir):
    """
    Intent: A log can contain a partially written multi-byte character, or output from
        something that did not write UTF-8. Raising on it would make the whole log
        unreadable because of one bad byte.
    Success: read_recent returns the readable lines rather than raising.
    Feature: Log viewer — resilient to malformed bytes.
    """
    logging_config.log_file_path().write_bytes(b"good line\n\xff\xfe broken\n")
    assert "good line" in logging_config.read_recent(10)


def test_rotated_files_are_listed_newest_first(log_dir):
    """
    Intent: The viewer shows the active file only, so it must at least say what history
        exists on disk — otherwise an operator cannot tell whether the entry they want
        has already rotated out of view.
    Success: Existing rotated files are reported with their sizes, app.log.1 first.
    Feature: Log viewer — reports the rotated history on disk.
    """
    (log_dir / "app.log.1").write_text("newer backup")
    (log_dir / "app.log.3").write_text("older backup")
    listed = logging_config.rotated_files()
    assert [f["name"] for f in listed] == ["app.log.1", "app.log.3"]
    assert listed[0]["bytes"] == len("newer backup")


def test_per_request_library_chatter_does_not_bury_the_applications_own_log(log_dir):
    """
    Intent: httpx and friends log a line per HTTP request at INFO. At that level they
        outnumber this application's own messages by orders of magnitude, so the log an
        operator opens to diagnose a generation run is mostly noise — and real history
        rotates out sooner.
    Success: Those loggers are raised to WARNING, so their errors still appear but their
        per-request lines do not.
    Feature: Logging — the log carries this application's messages.
    """
    logging_config.configure_logging(force=True)
    for name in logging_config.NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    logging.getLogger("httpx").info("GET http://testserver/ 200 OK")
    logging.getLogger("httpx").warning("connection failed")
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = logging_config.log_file_path().read_text()
    assert "200 OK" not in contents
    assert "connection failed" in contents
