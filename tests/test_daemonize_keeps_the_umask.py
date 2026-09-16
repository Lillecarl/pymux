"""
`daemonize` keeps the umask of the shell that started it.

The daemon is the server, and every pane forks from it, so the umask
it keeps is the umask every file a pane writes gets. It used to set
0 at its "decouple" step and never restore anything, which made every
pane write mode 0666 files on a machine whose login shell sets 077.
Lillecarl/pymux#398.
"""

import os
import stat
import subprocess
import sys
import textwrap
import time

import pytest


@pytest.mark.skipif(os.name != "posix", reason="daemonize forks")
def test_daemonize_keeps_the_umask_of_its_caller(tmp_path):
    "The daemon reports the caller's umask, and writes files like they would."
    mark = tmp_path / "mark"
    made = tmp_path / "made"

    code = textwrap.dedent(
        f"""
        import os
        from pymux.utils import daemonize

        os.umask(0o077)
        if daemonize(stdout={str(mark)!r}):
            # The daemon: stdout is the mark now. Report the umask it
            # was left with, and leave a file behind as a pane would.
            print(oct(os.umask(0o077)), flush=True)
            with open({str(made)!r}, "w"):
                pass
        """
    )

    # The original leaves first; the daemon it orphaned reports after.
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30)

    deadline = time.monotonic() + 10
    while not (mark.exists() and mark.read_text()) and time.monotonic() < deadline:
        time.sleep(0.05)

    assert mark.read_text().strip() == "0o77"
    assert stat.S_IMODE(made.stat().st_mode) == 0o600
