"""
What every run of this suite shares: a loop for the panes, and the
examples a property test draws.

A property test with no seed is a different test every run, and a gate
that is red by luck teaches everybody to run it again instead of to
look. `pyte` learned that the hard way (Lillecarl/pyterm#182), so the
gate here takes a pinned profile from the first property test this
suite has.

`pyte/tests/conftest.py` is the same section, with the groups of that
suite around it.
"""

from __future__ import annotations

import collections
import cProfile
import faulthandler
import gc
import os
import pstats
import resource
import statistics
import sys
import time
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings
from hypothesis.internal.entropy import deterministic_PRNG
from prompt_toolkit.application.current import _current_app_session
from ptyhost.process import Process

sys.path.insert(0, str(Path(__file__).parent.parent))

import contextlib

from pymux.core.main import Pymux

#: How long one test may take before it is stuck. The check sets it.
HANG_SECONDS = float(os.environ.get("PYMUX_HANG_SECONDS") or 0)

#: Where the stacks go: a copy of stderr made before any test's capture.
#: Written into the capture instead, they were thrown away with it by the
#: exit -- measured, an empty log. pytest's own plugin keeps the same copy.
_stacks_go_to = None


def pytest_configure(config):
    global _stacks_go_to
    try:
        fileno = sys.stderr.fileno()
    except AttributeError, ValueError, OSError:
        fileno = sys.__stderr__.fileno()
    _stacks_go_to = os.dup(fileno)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item):
    """
    Dump every thread's stack and **end the run** when a test hangs.

    Not pytest's `faulthandler_timeout`: it dumps and then waits on, so a
    deadlocked run never wrote its log or its status. Measured: one run
    still going after twenty minutes, another after hours.
    Lillecarl/pyterm#484.
    """
    if HANG_SECONDS:
        faulthandler.dump_traceback_later(HANG_SECONDS, exit=True, file=_stacks_go_to)
    try:
        yield
    finally:
        if HANG_SECONDS:
            faulthandler.cancel_dump_traceback_later()


#: Profile the collection of the first worker and print it, when set. Each
#: xdist worker collects the whole suite again, so a second of collection
#: costs a second per core.
PROFILE_COLLECTION = bool(os.environ.get("PYMUX_PROFILE_COLLECTION"))


@pytest.hookimpl(hookwrapper=True)
def pytest_collection(session):
    worker = getattr(session.config, "workerinput", {}).get("workerid", "gw0")
    if not PROFILE_COLLECTION or worker != "gw0":
        yield
        return
    profiler = cProfile.Profile()
    profiler.enable()
    try:
        yield
    finally:
        profiler.disable()
        with os.fdopen(os.dup(_stacks_go_to), "w") as stream:
            stats = pstats.Stats(profiler, stream=stream)
            stats.sort_stats("cumulative").print_stats(60)
            stats.sort_stats("tottime").print_stats(30)


def pytest_collection_finish(session):
    """
    Freeze what importing the suite made, so a full collection never
    scans it again. It is most of a worker's heap: 360 000 objects after
    the first test, against 410 000 to 440 000 after the last, and one
    collection of all of it held a timing test for 0.343 s.
    Lillecarl/pyterm#1001.

    Hypothesis makes its own `Random` first: it checks with
    `gc.get_referrers`, which sees no frozen object, and made after the
    freeze it warns that nothing holds the one its module holds.
    """
    with deterministic_PRNG():
        pass
    gc.collect()
    gc.freeze()


#: How many of the costliest tests by CPU the run names. The check sets it.
#: `--durations` is wall time, and under load wall time measures the load.
CPU_NAMES = int(os.environ.get("PYMUX_CPU") or 0)

_cpu_spent: list[tuple[float, str]] = []


def _cpu_seconds():
    mine = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return mine.ru_utime + mine.ru_stime + children.ru_utime + children.ru_stime


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    item.cpu_before = _cpu_seconds()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """
    Put the CPU of a whole test on its teardown report, children counted
    once reaped. A property rides the report to the xdist controller.
    """
    outcome = yield
    if CPU_NAMES and call.when == "teardown" and hasattr(item, "cpu_before"):
        spent = _cpu_seconds() - item.cpu_before
        outcome.get_result().user_properties.append(("cpu", spent))
    if HEAP_NAMES and call.when == "teardown":
        outcome.get_result().user_properties.append(("heap", _heap_after_a_test()))


#: How many tests the run names for what they left alive and for the
#: collector's longest pause. The check sets it. Each test then ends in
#: a full collection and a count of every object, so a run takes longer.
#: Lillecarl/pyterm#1001.
HEAP_NAMES = int(os.environ.get("PYMUX_HEAP") or 0)

#: The live objects by type after the last test of this worker.
_heap_before: collections.Counter[str] | None = None
_longest_pause = 0.0
_pause_started = 0.0
_heap_kept: list[tuple[int, str, str, int]] = []
_pauses: list[tuple[float, str]] = []
_live_by_worker: dict[str, list[int]] = {}
#: What each full collection the measurement forced took, by worker.
_collections_by_worker: dict[str, list[float]] = {}


def _time_the_collector(phase, info):
    global _pause_started, _longest_pause
    if phase == "start":
        _pause_started = time.perf_counter()
    else:
        _longest_pause = max(_longest_pause, time.perf_counter() - _pause_started)


if HEAP_NAMES:
    gc.callbacks.append(_time_the_collector)


def _heap_after_a_test() -> tuple[int, list[tuple[str, int]], float, int, float]:
    """
    Growth since the last test, the types that grew most, the longest
    pause, the live count, and what the full collection here took.
    """
    global _heap_before, _longest_pause
    longest = _longest_pause
    started = time.perf_counter()
    gc.collect()
    took = time.perf_counter() - started
    # Not this collection's own pause: it is the measurement, not the test.
    _longest_pause = 0.0
    now = collections.Counter(type(one).__qualname__ for one in gc.get_objects())
    before, _heap_before = _heap_before, now
    if before is None:
        return 0, [], longest, now.total(), took
    grown = now.copy()
    grown.subtract(before)
    return now.total() - before.total(), grown.most_common(5), longest, now.total(), took


def pytest_runtest_logreport(report):
    for name, value in report.user_properties:
        if name == "cpu":
            _cpu_spent.append((value, report.nodeid))
        elif name == "heap":
            grew, types, pause, live, took = value
            worker = getattr(report, "node", None)
            worker_id = getattr(worker, "gateway", None) and worker.gateway.id or "gw0"
            _heap_kept.append((grew, report.nodeid, ", ".join("%s +%d" % one for one in types), live))
            _pauses.append((pause, "%s %s (%d live)" % (worker_id, report.nodeid, live)))
            _live_by_worker.setdefault(worker_id, []).append(live)
            _collections_by_worker.setdefault(worker_id, []).append(took)


def _heap_summary(terminalreporter) -> None:
    write = terminalreporter.write_line
    terminalreporter.section("the %d tests that left the most alive" % HEAP_NAMES)
    for grew, nodeid, types, live in sorted(_heap_kept, reverse=True)[:HEAP_NAMES]:
        write("%+8d %8d live  %s" % (grew, live, nodeid))
        write("                    %s" % types)
    terminalreporter.section("the %d longest collector pauses" % HEAP_NAMES)
    for pause, where in sorted(_pauses, reverse=True)[:HEAP_NAMES]:
        write("%7.3fs %s" % (pause, where))
    terminalreporter.section("live objects of each worker: first, lowest, highest, last, and the tenths")
    for worker_id, lives in sorted(_live_by_worker.items()):
        tenths = [lives[len(lives) * part // 10] for part in range(10)]
        write(
            "%s %5d tests  %d %d %d %d  %s"
            % (worker_id, len(lives), lives[0], min(lives), max(lives), lives[-1], " ".join(map(str, tenths)))
        )
    terminalreporter.section("a full collection after each test, by worker: median and longest")
    for worker_id, took in sorted(_collections_by_worker.items()):
        write("%s %.4fs %.4fs" % (worker_id, statistics.median(took), max(took)))


def pytest_terminal_summary(terminalreporter):
    if HEAP_NAMES:
        _heap_summary(terminalreporter)
    if not CPU_NAMES:
        return
    write = terminalreporter.write_line
    total = sum(spent for spent, _ in _cpu_spent)
    terminalreporter.section(f"costliest {CPU_NAMES} by CPU, of {total:.1f} s")
    for spent, nodeid in sorted(_cpu_spent, reverse=True)[:CPU_NAMES]:
        write(f"{spent:7.2f}s {nodeid}")
    by_file: dict[str, float] = {}
    for spent, nodeid in _cpu_spent:
        path = nodeid.split("::")[0]
        by_file[path] = by_file.get(path, 0.0) + spent
    terminalreporter.section("CPU by file")
    for path, spent in sorted(by_file.items(), key=lambda pair: -pair[1])[:CPU_NAMES]:
        write(f"{spent:7.2f}s {path}")


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
        self.options.test_mode = True
        self.session_defaults.paint_screen = True

    monkeypatch.setattr(Pymux, "__init__", __init__)


@pytest.fixture(autouse=True)
def no_application_left_current():
    """
    A test leaves no application current behind it.

    `set_app` saves and restores one attribute of the default
    `AppSession`, which every task shares. Two tasks that each hold
    `set_app` across an `await` restore in the wrong order, and an
    application of a finished test stays current. The next test on that
    worker then reads another server's application through `get_app()`:
    `test_full_screen.py` found no client watching its window, at random.
    Lillecarl/pyterm#754. A task that holds `set_app` across an await
    needs `create_app_session()` around it, as the server's connections
    have (Lillecarl/pyterm#232).

    The fixture blames the test that leaked and clears it, so the next
    test starts clean whatever happened.
    """
    yield
    session = _current_app_session.get()
    left, session.app = session.app, None
    assert left is None, "this test left %r current; see no_application_left_current" % (left,)


@pytest.fixture
async def pymux():
    "A server with one window, whose program ends at once. A file that needs another server defines its own."
    mux = Pymux()
    async with mux.running():
        await mux.panes.create_window("%s -c pass" % (sys.executable,))
        try:
            yield mux
        finally:
            for window in list(mux.arrangement.windows):
                for pane in list(window.panes):
                    process = getattr(pane, "process", None)
                    if process is not None and not process.is_terminated:
                        process.kill()


@pytest.fixture
def anyio_backend():
    """
    asyncio, and nothing else.

    anyio runs each coroutine test on every backend the `anyio_backend`
    fixture names, which is a second dimension on every test id. This
    suite never ran on trio -- ptyhost speaks asyncio underneath -- so
    the dimension carried no information, only brackets.
    """
    return "asyncio"


@pytest.fixture(autouse=True)
def every_pty_this_test_opened(monkeypatch):
    """
    Close the pty of every pane a test made.

    **`kill` does not close one, on purpose.** A program writes its
    last words and exits, and the kernel still holds them; the reader
    picks them up on the turns of the loop after the kill and
    `Backend.close` runs when that read reaches the end of the file
    (Lillecarl/pyterm#123). A test ends before any of those turns, so
    the descriptors stayed open -- two per pane, in a suite that makes
    a hundred and eighty of them.

    It ended holding 1067 of them. `select()` takes no descriptor above
    1023, so `test_windows_are_renumbered` failed with "filedescriptor
    out of range" as soon as anything added a pane, and which test paid
    for it depended on the order they ran in.

    **Here rather than in the thirty-three fixtures that kill a pane.**
    They each tear down what they know about; this closes what the test
    really opened, including a pane a fixture forgot.
    """
    opened = []
    original = Process.__init__

    def __init__(self, *args, **kw):
        original(self, *args, **kw)
        opened.append(self.backend)

    monkeypatch.setattr(Process, "__init__", __init__)
    try:
        yield
    finally:
        for backend in opened:
            # Both sides, and the slave first, which is the order the
            # reap uses: closing it is what makes the end of the file
            # reachable on the master. Neither is reached here -- the
            # reap closes the slave from a loop callback and the reader
            # closes the master when it reads the end -- so a test that
            # never turns the loop again leaves both.
            slave = getattr(backend, "slave", None)
            if slave is not None:
                backend.slave = None
                with contextlib.suppress(OSError):
                    os.close(slave)
            with contextlib.suppress(OSError):
                backend.close()


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
