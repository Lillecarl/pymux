"""
An async SQLite connection whose opening is cancelled must not keep the
process alive. apsw's `as_async` starts a worker thread that is not a
daemon, then awaits the open; a cancel there left the thread waiting for
ever, and the server hung at `kill-server`. The actions log opens its
database on the first action, so a `kill-server` at that moment did it.
Lillecarl/pyterm#968.
"""

from __future__ import annotations

import subprocess
import sys

#: Cancelled at its first checkpoint, which `mkdir` would otherwise be.
PROGRAM = """
import sys
from pathlib import Path

import anyio

from pymux.keys import actions


async def mkdir(self, **_):
    pass


anyio.Path.mkdir = mkdir


async def main():
    with anyio.CancelScope() as scope:
        scope.cancel()
        await actions._open(Path(sys.argv[1]))


anyio.run(main)
print("ended")
"""


def test_a_cancelled_open_lets_the_process_exit(tmp_path):
    done = subprocess.run(
        [sys.executable, "-c", PROGRAM, str(tmp_path / "actions.sqlite")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert done.stdout.strip() == "ended", done.stderr
