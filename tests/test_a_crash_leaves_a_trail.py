"""
A server that dies leaves what is needed to understand it.

On 2026-10-22 a server died with an empty stack file and nothing in
its log. Now a fatal signal writes every thread's stack, an exception
that escapes a thread is logged, and a crash writes a report beside the
log. Lillecarl/pymux#736.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

from pymux import introspect


def test_a_crash_report_holds_the_chain_and_every_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(introspect, "where_dump_goes", lambda: tmp_path)
    try:
        try:
            raise KeyError("the cause")
        except KeyError as cause:
            raise RuntimeError("the crash") from cause
    except RuntimeError as error:
        path = introspect.write_crash_report(error, started=0.0)

    assert path is not None and path.parent == tmp_path
    report = path.read_text()
    assert "RuntimeError: the crash" in report
    assert "KeyError: 'the cause'" in report
    assert "Every thread:" in report
    assert "test_a_crash_report_holds_the_chain_and_every_thread" in report
    assert "executable %s" % (sys.executable,) in report


def test_a_crash_report_says_what_the_server_was_in_the_middle_of(tmp_path, monkeypatch):
    "A resume names the snapshot it loads, and a crash after it names nothing."
    monkeypatch.setattr(introspect, "where_dump_goes", lambda: tmp_path)
    introspect.note_for_a_crash("resuming from the snapshot /x.sqlite")
    try:
        first = introspect.write_crash_report(RuntimeError("during"))
    finally:
        introspect.note_for_a_crash(None)
    assert first is not None and "while resuming from the snapshot /x.sqlite" in first.read_text()
    # The same second names the same file, so this one replaces it.
    second = introspect.write_crash_report(RuntimeError("after"))
    assert second is not None and "while " not in second.read_text()


def test_an_exception_that_kills_a_thread_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(sys, "unraisablehook", sys.unraisablehook)
    introspect.log_what_escapes()

    def dies() -> None:
        raise ValueError("in a worker")

    with caplog.at_level(logging.CRITICAL, logger="pymux"):
        worker = threading.Thread(target=dies, name="the-worker")
        worker.start()
        worker.join()

    (died,) = [record for record in caplog.records if "Thread the-worker died." in record.message]
    assert died.exc_info is not None
    assert str(died.exc_info[1]) == "in a worker"


#: Sets the dump up as a server does, then dies of SIGABRT.
ABORTS = (
    "import os, sys\n"
    "from pymux import log, introspect\n"
    "log.configure(sys.argv[1])\n"
    "print(introspect.answer_signal(), flush=True)\n"
    "os.abort()\n"
)


def test_a_fatal_signal_writes_every_stack(tmp_path):
    "The stack file stayed empty: only SIGUSR1 wrote to it."
    done = subprocess.run(
        [sys.executable, "-c", ABORTS, str(tmp_path / "server.log")], capture_output=True, text=True, timeout=30
    )

    stacks = done.stdout.strip()
    assert stacks.endswith(".log"), done.stderr
    written = open(stacks).read()
    assert "Fatal Python error: Aborted" in written
    assert "Current thread" in written or "Thread 0x" in written
