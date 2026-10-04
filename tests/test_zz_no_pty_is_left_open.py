"""
Nothing this suite ran is still holding a pty.

A pane opens two descriptors and gives neither back by itself. `kill`
sends a signal; the reap closes the slave from a loop callback and the
reader closes the master when it reads the end of the file
(Lillecarl/pymux#121), and a test ends before either turn. So a suite
that makes a hundred and eighty panes held a descriptor for each side
of every one of them.

It ended with 1067 open. `select()` takes no descriptor above 1023, so
`test_windows_are_renumbered` failed with "filedescriptor out of range"
as soon as anything added a pane, and which test paid for it depended
on the order they ran in. `every_pty_this_test_opened` in `conftest.py`
closes them now, and this says so.

**The name begins with `zz` to put it last.** pytest runs the files of
a directory in order, and this one covers what ran before it. It is
correct anywhere -- every test it follows has been torn down by then --
but last is the whole suite.
"""
from __future__ import annotations

import os


def what_is_open() -> list:
    "Every pty this process holds, as the device each one names."
    held = []
    for name in os.listdir("/proc/self/fd"):
        try:
            where = os.readlink("/proc/self/fd/" + name)
        except OSError:
            # The descriptor of the listing itself, already gone.
            continue
        if where == "/dev/ptmx" or where.startswith("/dev/pts/"):
            held.append(where)
    return held


def test_no_pane_of_this_suite_still_holds_a_pty():
    assert what_is_open() == []
