"""
The room a server binds its unnamed sockets in.

A socket named in a flat /tmp is a socket any account can squat in
front of the bind: the next "pymux attach" lands on the squatter. So
the sockets live in `<base>/pymux-<uid>`, a directory the first server
creates mode 0700 and every user of it verifies before it uses, the
way tmux keeps its sockets in `tmux-<uid>`.
Lillecarl/pymux#405.
"""

import os
import stat

import pytest

from pymux.pipes.posix import socket_directory

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the room needs a unix uid and lstat"
)


@pytest.fixture
def bases(tmp_path, monkeypatch):
    """
    A base that obeys the environment, and the environment itself.

    Both override names are taken out, the platform's runtime directory
    is patched to the same place, so the room lands under the patched
    temp directory unless a test puts a name back in.
    """
    monkeypatch.delenv("PYMUX_TMPDIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        "platformdirs.PlatformDirs", lambda: FakePlatformDirs(str(tmp_path))
    )
    return tmp_path


class FakePlatformDirs:
    """
    The runtime directory platformdirs answers, minus the platform.

    It reads `$XDG_RUNTIME_DIR` first, and names the platform default
    when that is unset -- the contract of its mixin, with the platform
    itself patched out: the machine the suite runs on may have none of
    the directories the real one names, and a test that depended on
    them would only pass where it was written.
    """

    def __init__(self, base):
        self.base = base

    @property
    def user_runtime_dir(self):
        value = os.environ.get("XDG_RUNTIME_DIR", "")
        if value and os.path.isabs(value):
            return value
        return self.base


def test_the_room_is_created_private(bases):
    room = socket_directory()

    assert os.path.basename(room) == "pymux-%d" % os.getuid()
    assert stat.S_IMODE(os.lstat(room).st_mode) == 0o700


def test_an_existing_private_room_is_used(bases):
    room = os.path.join(str(bases), "pymux-%d" % os.getuid())
    os.mkdir(room, 0o700)

    assert socket_directory() == room


def test_pymux_tmpdir_comes_first(bases, monkeypatch):
    monkeypatch.setenv("PYMUX_TMPDIR", str(bases / "first"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(bases / "second"))
    (bases / "first").mkdir()

    assert socket_directory() == str(bases / "first" / ("pymux-%d" % os.getuid()))


def test_xdg_runtime_dir_comes_before_the_temp_dir(bases, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(bases / "runtime"))
    (bases / "runtime").mkdir()

    assert socket_directory() == str(
        bases / "runtime" / ("pymux-%d" % os.getuid())
    )


def test_the_platform_runtime_dir_comes_before_the_temp_dir(bases, monkeypatch):
    """
    With `$XDG_RUNTIME_DIR` unset -- a login that skipped it, cron --
    the platform's own runtime directory is the second base: on Linux
    `/run/user/<uid>`, on macOS what Apple prefers, on the BSDs their
    own. A socket room there is private by the platform's rule, and
    dies with the session. Lillecarl/pymux#421.
    """
    platform = bases / "platform"
    platform.mkdir()
    monkeypatch.setattr("platformdirs.PlatformDirs", lambda: FakePlatformDirs(platform))

    assert socket_directory() == str(platform / ("pymux-%d" % os.getuid()))


def test_a_squatted_platform_room_falls_through_to_the_temp_dir(
    bases, monkeypatch
):
    platform = bases / "platform"
    platform.mkdir()
    (platform / ("pymux-%d" % os.getuid())).mkdir(mode=0o755)
    monkeypatch.setattr("platformdirs.PlatformDirs", lambda: FakePlatformDirs(platform))

    assert socket_directory() == str(bases / ("pymux-%d" % os.getuid()))


def test_a_relative_base_is_skipped(bases, monkeypatch):
    monkeypatch.setenv("PYMUX_TMPDIR", "relative/path")

    assert socket_directory() == str(bases / ("pymux-%d" % os.getuid()))


def test_a_base_that_cannot_hold_a_room_falls_through(
    bases, monkeypatch
):
    unwritable = bases / "unwritable"
    unwritable.mkdir()
    unwritable.chmod(0o555)
    monkeypatch.setenv("PYMUX_TMPDIR", str(unwritable))

    try:
        assert socket_directory() == str(bases / ("pymux-%d" % os.getuid()))
    finally:
        unwritable.chmod(0o755)


def test_a_squatted_room_falls_through_to_the_next_base(bases, monkeypatch):
    """
    A squat in the first base must not stop the server: the next base
    holds a good room, and the squat wins nothing by existing.
    """
    squatted = bases / "squatted"
    squatted.mkdir()
    room = squatted / ("pymux-%d" % os.getuid())
    room.mkdir(mode=0o755)
    monkeypatch.setenv("PYMUX_TMPDIR", str(squatted))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(bases / "runtime"))
    (bases / "runtime").mkdir()

    assert socket_directory() == str(bases / "runtime" / ("pymux-%d" % os.getuid()))


def test_every_room_failing_names_the_last_reason(bases):
    room = bases / ("pymux-%d" % os.getuid())
    room.mkdir(mode=0o755)

    with pytest.raises(OSError, match="unsafe permissions"):
        socket_directory()


def test_a_symlink_in_its_place_is_refused(bases):
    elsewhere = bases / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, bases / ("pymux-%d" % os.getuid()))

    with pytest.raises(OSError, match="is not a directory"):
        socket_directory()


def test_a_file_in_its_place_is_refused(bases):
    (bases / ("pymux-%d" % os.getuid())).write_text("")

    with pytest.raises(OSError, match="is not a directory"):
        socket_directory()


def test_a_readable_room_is_refused(bases):
    room = bases / ("pymux-%d" % os.getuid())
    room.mkdir(mode=0o755)

    with pytest.raises(OSError, match="unsafe permissions"):
        socket_directory()


def test_a_room_owned_by_someone_else_is_refused(bases, monkeypatch):
    real = os.getuid()
    room = bases / ("pymux-%d" % real)
    room.mkdir(mode=0o700)
    # The owner check compares what lstat saw against what getuid says.
    # Naming a foreign uid makes the two disagree with nothing to chown.
    monkeypatch.setattr(os, "getuid", lambda: real + 1)

    with pytest.raises(OSError, match="unsafe permissions"):
        socket_directory()


def test_no_usable_base_at_all_is_a_refusal(bases, monkeypatch):
    dead = bases / "dead"
    dead.mkdir()
    dead.chmod(0o555)
    monkeypatch.setenv("PYMUX_TMPDIR", str(dead))
    monkeypatch.setattr("platformdirs.PlatformDirs", lambda: FakePlatformDirs(dead))
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(dead))

    try:
        with pytest.raises(OSError, match="no base"):
            socket_directory()
    finally:
        dead.chmod(0o755)
