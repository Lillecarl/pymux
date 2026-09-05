"""
Where the log of a server goes.

A server logs an exception and keeps going, so logging is a normal path
here. In `integrated` and in `standalone` that server shares one
terminal with the client drawing on it, and a logger with no handler
writes to `sys.stderr`, which is that terminal. One exception in a
background task then paints its traceback over the frame.

Lillecarl/pymux#36.
"""
import io
import logging
import sys

import pytest

from pymux import log


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


def test_nothing_reaches_the_terminal_when_no_file_can_be_opened(
    tmp_path, monkeypatch
):
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
