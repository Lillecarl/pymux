"""
A server that is killed takes its private PipeWire with it.

On 2026-10-22 two sets of PipeWire, WirePlumber and pw-cat ran on with
parent 1, one from a server killed that morning and one from a week
before. Lillecarl/pymux#729.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from pymux.audio import dies_with_this_process

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux's")

#: A server in small: starts `sleep` the way sound starts PipeWire, says
#: its pid, and dies of SIGKILL, which no handler sees.
SERVER = (
    "import os, signal, subprocess, sys, time\n"
    "from pymux.audio import dies_with_this_process\n"
    # Not on the captured streams: a sleep left running would hold them open.
    "child = subprocess.Popen(dies_with_this_process(['sleep', '60']),"
    " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "time.sleep(0.5)\n"
    "print(child.pid, flush=True)\n"
    "os.kill(os.getpid(), signal.SIGKILL)\n"
)


def _gone(pid: int) -> bool:
    "Ended: no process, or a zombie its new parent has yet to reap."
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError, ProcessLookupError:
        # ESRCH: the read met the process as it was reaped.
        return True


def test_a_program_ends_when_its_server_is_killed():
    done = subprocess.run([sys.executable, "-c", SERVER], capture_output=True, text=True, timeout=30)
    pid = int(done.stdout.strip())
    deadline = time.monotonic() + 10
    while not _gone(pid):
        if time.monotonic() > deadline:
            os.kill(pid, 9)
            pytest.fail("sleep outlived the server that started it")
        time.sleep(0.05)


def test_the_program_runs_as_itself():
    "The launcher execs it: same pid, same arguments, same output."
    done = subprocess.run(dies_with_this_process(["sh", "-c", "echo $0 $1", "a", "b"]), capture_output=True, text=True)
    assert done.stdout == "a b\n"
