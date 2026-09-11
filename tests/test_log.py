"""
Where the log of a server goes.

A server logs an exception and keeps going, so logging is a normal path
here. In `integrated` and in `standalone` that server shares one
terminal with the client drawing on it, and a logger with no handler
writes to `sys.stderr`, which is that terminal. One exception in a
background task then paints its traceback over the frame.

Lillecarl/pymux#36.

It also runs for weeks, so a log that grows is a log that fills a disk.
One left running for four days reached 86 MB, because the level was
DEBUG and a server writes a line for every frame it draws -- eleven a
second on a session where the panes animate. Lillecarl/pymux#248.
"""

import io
import logging
import sys

import pytest

from pymux import log
from pymux.entry_points.run_pymux import _how_much_to_log


@pytest.fixture(autouse=True)
def a_clean_logger():
    "Give each test the logger as a fresh process would have it."
    handlers = list(log.logger.handlers)
    level = log.logger.level
    propagate = log.logger.propagate
    log.logger.handlers = []
    yield
    for handler in log.logger.handlers:
        handler.close()
    log.logger.handlers = handlers
    log.logger.setLevel(level)
    log.logger.propagate = propagate


# ----------------------------------------------------------------------
# The file.


def test_the_named_file_is_the_one_that_is_used(tmp_path):
    wanted = tmp_path / "named.log"
    assert log.configure(str(wanted)) == wanted


def test_without_a_name_it_goes_under_the_state_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert log.configure() == tmp_path / "pymux" / "server.log"


def test_the_state_directory_has_a_default(monkeypatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/somebody")
    assert log.default_logfile() == (
        log.Path("/home/somebody/.local/state/pymux/server.log")
    )


def test_a_directory_that_is_missing_is_made(tmp_path):
    wanted = tmp_path / "one" / "two" / "server.log"
    assert log.configure(str(wanted)) == wanted
    assert wanted.parent.is_dir()


# ----------------------------------------------------------------------
# What it writes, and when.


def test_a_run_that_logs_nothing_leaves_no_file(tmp_path):
    """
    The file is opened by the first message. So the common case, a
    session that goes well, writes nothing at all.
    """
    wanted = tmp_path / "server.log"
    log.configure(str(wanted))
    assert not wanted.exists()


def test_a_message_reaches_the_file(tmp_path):
    wanted = tmp_path / "server.log"
    log.configure(str(wanted))
    log.logger.error("a packet went wrong")
    for handler in log.logger.handlers:
        handler.flush()
    assert "a packet went wrong" in wanted.read_text()


def test_a_traceback_reaches_the_file(tmp_path):
    "This is the message that used to land on the terminal."
    wanted = tmp_path / "server.log"
    log.configure(str(wanted))
    try:
        raise ValueError("boom")
    except ValueError:
        log.logger.exception("the read loop failed")
    for handler in log.logger.handlers:
        handler.flush()
    written = wanted.read_text()
    assert "the read loop failed" in written
    assert "ValueError: boom" in written


# ----------------------------------------------------------------------
# What it must never write to.


def test_nothing_reaches_the_terminal(tmp_path, monkeypatch):
    """
    The whole point. Without a handler python writes a record to
    `sys.stderr`, and in two of the modes that is the terminal the
    client draws on.
    """
    caught = io.StringIO()
    monkeypatch.setattr(sys, "stderr", caught)
    log.configure(str(tmp_path / "server.log"))
    log.logger.error("a packet went wrong")
    assert caught.getvalue() == ""


def test_nothing_reaches_the_terminal_when_no_file_can_be_opened(tmp_path, monkeypatch):
    "A log that cannot be written is dropped, and not painted."
    caught = io.StringIO()
    monkeypatch.setattr(sys, "stderr", caught)

    # A file cannot be made under a file.
    blocked = tmp_path / "a-file"
    blocked.write_text("")
    assert log.configure(str(blocked / "server.log")) is None

    log.logger.error("a packet went wrong")
    assert caught.getvalue() == ""


def test_a_root_handler_does_not_take_the_messages(tmp_path, monkeypatch):
    """
    Something else may call `basicConfig` and point the root logger at
    the terminal. The messages of pymux must not follow it there.
    """
    caught = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(caught)
    root.addHandler(handler)
    try:
        log.configure(str(tmp_path / "server.log"))
        log.logger.error("a packet went wrong")
        assert caught.getvalue() == ""
    finally:
        root.removeHandler(handler)
        handler.close()


# ----------------------------------------------------------------------
# How loud it is, and how big it gets.


def test_a_server_nobody_asked_to_debug_logs_at_info(tmp_path):
    "DEBUG writes a line for every frame, and a server draws eleven a second."
    log.configure(str(tmp_path / "server.log"))

    assert log.logger.level == logging.INFO


def test_the_file_has_an_end(tmp_path):
    log.configure(str(tmp_path / "server.log"))

    handler = log.logger.handlers[-1]
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == log.HOW_BIG
    assert handler.backupCount == log.HOW_MANY


def test_it_starts_again_rather_than_growing(tmp_path, monkeypatch):
    "The rotation is real, and not only configured."
    monkeypatch.setattr(log, "HOW_BIG", 2000)
    monkeypatch.setattr(log, "HOW_MANY", 2)
    log.configure(str(tmp_path / "server.log"), logging.DEBUG)

    for number in range(400):
        log.logger.debug("a line that is long enough to fill this up: %d", number)

    written = sorted(path.name for path in tmp_path.iterdir())
    assert written == ["server.log", "server.log.1", "server.log.2"]
    for name in written:
        assert (tmp_path / name).stat().st_size < 4000


def test_the_log_says_where_it_went(tmp_path):
    "`pymux/introspect.py` writes its dumps beside it, so it has to be found."
    path = log.configure(str(tmp_path / "server.log"))

    assert log.logfile() == path


def test_a_level_nobody_named_is_info():
    assert _how_much_to_log(None) == logging.INFO
    assert _how_much_to_log("") == logging.INFO


def test_a_person_debugging_asks_for_it():
    assert _how_much_to_log("debug") == logging.DEBUG
    assert _how_much_to_log("warning") == logging.WARNING


def test_a_frame_is_not_logged_at_info():
    """
    The lines are good ones -- `Woke` exists so that a frame drawn for
    no reason can be traced to what asked for it (Lillecarl/pymux#180).
    They are just not what a person wants by default, and `pymux
    counters` gives the same finding with nothing written down.
    """
    import inspect

    from pymux.main import Pymux

    said = inspect.getsource(Pymux.invalidate)
    assert "logger.debug(" in said
    assert "logger.info(" not in said


def test_a_pane_that_writes_is_still_logged():
    """
    The line that says a pane is animating.

    A pane's write does not come through `Pymux.invalidate` any more --
    it wakes only the clients that can see it (Lillecarl/pymux#224) --
    so the line that found eleven frames a second has to be on the path
    that a write does take.
    """
    import inspect

    from pymux.main import Pymux

    said = inspect.getsource(Pymux.client_asked_for_a_frame)
    assert "logger.debug(" in said
    assert "logger.info(" not in said
