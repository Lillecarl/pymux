"""
`pymux renice` and `list-processes`: pymux's own processes take a
niceness, and a program a person runs does not. Lillecarl/pyterm#675.

The sandbox may not lower a niceness, so the end to end run raises it,
which any user may. The lowered case is judged with `os.getpriority`
answering a raised server.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import anyio
import psutil
from prompt_toolkit.application.current import set_app
from session import create_session

from pymux.answer import Answer
from pymux.commands import handle_command
from pymux.niceness import MOST_PASSES, Target, as_a_person_runs, reach_every_thread, targets_of


async def _run_answering(pymux, state, command) -> list[str]:
    pymux.answer = Answer()
    try:
        with set_app(state.app):
            answer = handle_command(pymux, command)
            if answer is not None:
                await answer
            return [line.strip() for line in pymux.answer.output]
    finally:
        pymux.answer = None


async def test_list_processes_names_the_server_and_no_pane():
    async with create_session() as (pymux, state):
        said = targets_of(await _run_answering(pymux, state, "list-processes"))

        assert Target("server", os.getpid()) in said
        panes = {pane.process.backend.pid for pane in pymux.panes_by_id.values()}
        assert panes and not panes & {target.pid for target in said}


def test_a_command_is_unchanged_at_normal_priority():
    assert as_a_person_runs("echo hello") == "echo hello"
    assert as_a_person_runs(("echo", "hello")) == ["echo", "hello"]


async def test_a_raised_server_starts_a_command_through_the_wrapper(monkeypatch):
    with monkeypatch.context() as patched:
        patched.setattr(os, "getpriority", lambda which, who: -10)
        shell = as_a_person_runs("echo $((1 + 1))")
        argv = as_a_person_runs(["echo", "two words"])

    assert shell[:4] == [sys.executable, "-I", "-m", "ptyhost.normal_priority"]
    assert (await anyio.run_process(shell)).stdout == b"2\n"
    assert (await anyio.run_process(argv)).stdout == b"two words\n"


def _niceness_of(pid: int) -> set[int]:
    "Every thread's, where a niceness is a thread's."
    ids = [thread.id for thread in psutil.Process(pid).threads()] if sys.platform == "linux" else [pid]
    return {os.getpriority(os.PRIO_PROCESS, thread) for thread in ids}


def test_renice_reaches_every_thread_of_the_server(tmp_path):
    sock = str(tmp_path / "pymux.sock.renice-test.0")
    env = dict(os.environ)
    env.pop("PYMUX", None)
    pymux = [sys.executable, "-m", "pymux", "-S", sock]
    server = subprocess.Popen([*pymux, "start-server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 10
        while not os.path.exists(sock) and time.time() < deadline:
            time.sleep(0.05)
        assert os.path.exists(sock), "the test server never came up"

        done = subprocess.run([*pymux, "renice", "7"], env=env, capture_output=True, text=True, timeout=30)

        assert done.returncode == 0, done.stderr
        assert "server %d" % server.pid in done.stdout
        assert _niceness_of(server.pid) == {7}
    finally:
        subprocess.run(
            [*pymux, "kill-server"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30
        )
        server.wait(timeout=30)


def test_a_thread_started_during_the_renice_is_reached():
    "It took the niceness of a starter not yet set, and kept it: the gate saw `{0, 7}`."
    threads = [1, 2]
    niceness = {}

    def set_one(thread):
        niceness[thread] = 7
        if thread == 1 and 3 not in threads:
            threads.append(3)  # Thread 2 starts it, and 2 is not set yet.
            niceness[3] = 0

    reach_every_thread(list(threads), lambda: list(threads), set_one)

    assert niceness == {1: 7, 2: 7, 3: 7}


def test_renice_stops_listing_when_threads_never_stop_starting():
    started = iter(range(100, 10_000))
    asked = []

    reach_every_thread([1], lambda: [next(started)], asked.append)

    assert len(asked) == MOST_PASSES


def test_renice_refuses_a_niceness_out_of_range():
    done = subprocess.run([sys.executable, "-m", "pymux", "renice", "-21"], capture_output=True, text=True, timeout=30)
    assert done.returncode == 2
    assert "from -20 to 19" in done.stderr
