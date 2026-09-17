"""
What importing the entry point costs.

A detached command -- `send-keys`, `set-option`, `new-window` from a
script or an agent -- pays the imports of `pymux.entry_points.run_pymux`
before it reaches the socket. The toolkit and the ssh machinery are
the attach path's to carry: they arrive when a mode draws or when an
address names a machine, and never before. Lillecarl/pymux#392.

The assertions read the modules of a fresh interpreter, because this
one has already imported whatever the rest of the suite pulled in.
"""

import json
import subprocess
import sys

#: What the detached path must never carry in.
FOREIGN = ("prompt_toolkit", "asyncssh", "anyio")


def modules_after_import(imports: str) -> list[str]:
    "The modules a fresh interpreter holds after `imports`."
    statement = (
        "import json, sys; %s print(json.dumps(sorted(sys.modules)))" % imports
    )
    out = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_the_entry_imports_no_toolkit():
    "The entry comes in without the stack that draws."
    assert not [
        m for m in modules_after_import("import pymux.entry_points.run_pymux;")
        if m.startswith(FOREIGN)
    ], "the entry imported the TUI stack: a detached command pays it"


def test_diagnose_imports_no_toolkit():
    "`diagnose` rides the entry's mode word, and reports stdlib-only."
    assert not [
        m for m in modules_after_import("import pymux.diagnose;")
        if m.startswith(FOREIGN)
    ], "diagnose imported the TUI stack: a report pays it"


def test_the_entry_still_imports():
    "The entry is importable at all, so the tests above can lie."
    assert "pymux.entry_points.run_pymux" in modules_after_import(
        "import pymux.entry_points.run_pymux;"
    )
