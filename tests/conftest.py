"""
What every run of this suite shares: a loop for the panes, and the
examples a property test draws.

A property test with no seed is a different test every run, and a gate
that is red by luck teaches everybody to run it again instead of to
look. `pyte` learned that the hard way (Lillecarl/pymux#180), so the
gate here takes a pinned profile from the first property test this
suite has.

`pyte/tests/conftest.py` is the same section, with the groups of that
suite around it.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

sys.path.insert(0, str(Path(__file__).parent.parent))

from pymux.main import Pymux  # noqa: E402


@pytest.fixture(autouse=True)
def a_server_in_test_mode(monkeypatch):
    """
    Every `Pymux` this suite builds has `test-mode` on.

    **The clock is the reason.** `Pymux.displayed_now` is where every
    clock a person reads goes through, and `test-mode` pins it to 13:37
    on the 14th of March. Without it a test that reads the time is a
    different test either side of a second, and the suite has to
    choose between not testing the clock and being flaky about it --
    `measure_keystroke.py` cleared `status-right` rather than face it.

    A test that wants the real clock, or the option off, sets it back:
    this is a default and not a rule.

    The patch is on `__init__` because the tests build their own
    servers, a hundred and eighty of them, and a default is worth
    saying once.
    """
    original = Pymux.__init__

    def __init__(self, *args, **kw):
        original(self, *args, **kw)
        self.test_mode = True
        self.paint_screen = True

    monkeypatch.setattr(Pymux, "__init__", __init__)


@pytest.fixture(autouse=True)
def a_loop_for_this_test():
    """
    A current event loop for a test that runs none of its own.

    A pane that is starting holds an `asyncio.Future`, and a bare
    `Future()` asks this thread for its current loop. A coroutine test
    has one because anyio runs it; a plain `def test` has none, and a
    hundred and eighty of them make panes.

    `Pymux.__init__` used to make a loop and set it as the current one.
    That loop never ran, so anything armed on it happened never -- the
    server's own clock among them. Lillecarl/pymux#87.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield
    finally:
        asyncio.set_event_loop(None)
        loop.close()

# The gate. `derandomize` seeds each property test from its own source,
# so a run draws the examples the run before it drew, and a green gate
# means the same thing twice.
#
# This is hypothesis's own `ci` profile, which it loads by itself when
# it recognises the runner. A nix sandbox is not one it recognises, so
# the profile is named and loaded here.
settings.register_profile(
    "pinned",
    derandomize=True,
    print_blob=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# The hunt. Fresh examples, which is what finds the case nobody has
# written down. `--hypothesis-profile=roaming` picks it. No check runs
# it yet: `checks.pyte-roaming` is the shape one takes when this suite
# has enough property tests to be worth a hunt of its own.
#
# `database=None` on purpose. A sandbox throws its `.hypothesis`
# directory away, so a database there only pretends to remember. What
# a suite remembers is the `@example` decorators on its property
# tests, and those run under both profiles.
settings.register_profile(
    "roaming",
    derandomize=False,
    database=None,
    print_blob=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# **Here, and not in a fixture.** A `@settings(...)` decorator reads
# the profile that is loaded when the decorator runs, which is when
# pytest imports the test module. pytest imports a conftest before
# that, and calls a fixture long after it.
settings.load_profile("pinned")
