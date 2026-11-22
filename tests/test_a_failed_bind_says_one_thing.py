"""
A server that cannot bind its socket says why, and nothing else. A memory
stream that the server made before the bind was never closed, and its
`__del__` ran a lazy import at interpreter exit: two tracebacks under the
one that mattered. Lillecarl/pyterm#1065.
"""

from __future__ import annotations

import subprocess
import sys

RUN = "import sys; from pymux.entry_points.run_pymux import run; sys.argv[0] = 'pymux'; run()"


def test_a_socket_path_too_long_prints_one_error(tmp_path):
    # AF_UNIX holds 108 bytes of path.
    socket = tmp_path / ("x" * 120)
    done = subprocess.run(
        [sys.executable, "-c", RUN, "-S", str(socket), "new-session", "-d"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode != 0
    assert "path too long" in done.stderr
    assert "Exception ignored" not in done.stderr, done.stderr
