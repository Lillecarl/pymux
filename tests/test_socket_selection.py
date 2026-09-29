"""
Which server "pymux attach" reaches when nobody names one.

A server with no name takes the lowest number that is free, so a second
server on the same machine gets a higher number than the first. `attach`
without "-S" takes the first server that `list_clients` gives back, and
that list came straight out of `glob`, which has no order.

So a person who started a second server and attached could land on
either one. They usually landed on the old one, and every change in the
new server looked like it had done nothing.
"""

import os
import socket
import time

import pytest

from pymux.client.posix import list_socket_names

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the posix client needs a unix socket"
)


@pytest.fixture
def sockets(tmp_path, monkeypatch):
    """
    Make sockets that a client can connect to, oldest first.

    A real socket, not a plain file, so that the test says something
    about the sockets a server leaves behind. The override names for
    the socket room are taken out, so the room is the one under the
    patched temp directory, beside the flat sockets a test binds.
    """
    monkeypatch.delenv("PYMUX_TMPDIR", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("getpass.getuser", lambda: "someone")

    open_sockets = []

    def make(names, gap=0.05):
        for name in names:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(tmp_path / name))
            # **Room for every probe.** Nothing here accepts, and
            # `nobody_answers` connects once per candidate per call, so
            # a queue of one parks the second probe until it times out.
            # A real server accepts at once, which is why its own
            # `listen(0)` is enough there. Lillecarl/pymux#454.
            listener.listen(64)
            open_sockets.append(listener)
            time.sleep(gap)

    yield make

    for listener in open_sockets:
        listener.close()


def _names():
    "The sockets that `list_socket_names` gives back, in its order."
    return [os.path.basename(path) for path in list_socket_names()]


def test_newest_server_comes_first(sockets):
    sockets(["pymux.sock.someone.0", "pymux.sock.someone.1", "pymux.sock.someone.2"])
    assert _names() == [
        "pymux.sock.someone.2",
        "pymux.sock.someone.1",
        "pymux.sock.someone.0",
    ]


def test_number_does_not_decide(sockets):
    "The oldest server can hold the highest number, after a restart."
    sockets(["pymux.sock.someone.7", "pymux.sock.someone.0"])
    assert _names()[0] == "pymux.sock.someone.0"


def test_another_user_is_not_listed(sockets):
    sockets(["pymux.sock.someone.0", "pymux.sock.somebody.0"])
    assert _names() == ["pymux.sock.someone.0"]


def test_a_socket_in_the_room_is_listed(sockets, tmp_path):
    # The mode the room itself would have: the client verifies the room
    # it finds, and a 0755 one is a refusal.
    room = tmp_path / ("pymux-%d" % os.getuid())
    room.mkdir(mode=0o700)
    sockets([str(room / "pymux.sock.someone.0")])
    assert _names() == ["pymux.sock.someone.0"]


def test_the_room_and_the_flat_place_are_one_list(sockets, tmp_path):
    """
    One release of the flat layout: a server that bound before the
    room still answers, and an attach reaches the newest of either.
    """
    room = tmp_path / ("pymux-%d" % os.getuid())
    room.mkdir(mode=0o700)
    sockets(
        [
            "pymux.sock.someone.7",
            str(room / "pymux.sock.someone.0"),
        ]
    )
    assert _names() == ["pymux.sock.someone.0", "pymux.sock.someone.7"]


# ----------------------------------------------------------------------
# A server that is not there.
#
# A killed server leaves its socket file behind, and nothing ever took
# one away. The file is a socket by every test the filesystem has, so
# both readers offered it -- and `attach` offers the newest first, so
# the dead one went in front of a live server that was one entry down.
# Lillecarl/pymux#454.


@pytest.fixture
def a_dead_socket(tmp_path):
    "Bind a name and close it. The file stays, and nobody answers it."

    def make(name):
        path = str(tmp_path / name)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(1)
        listener.close()
        time.sleep(0.05)
        return path

    return make


def test_a_socket_nobody_answers_is_not_a_server(sockets, a_dead_socket):
    a_dead_socket("pymux.sock.someone.0")
    sockets(["pymux.sock.someone.1"])

    assert _names() == ["pymux.sock.someone.1"]


def test_the_dead_one_does_not_go_in_front(sockets, a_dead_socket):
    "It is the newest, which is the case a person meets."
    sockets(["pymux.sock.someone.1"])
    a_dead_socket("pymux.sock.someone.0")

    assert _names() == ["pymux.sock.someone.1"]


def test_the_lock_a_bind_takes_is_not_a_server(sockets, tmp_path):
    "It sits in the room and its name matches the glob."
    sockets(["pymux.sock.someone.0"])
    (tmp_path / "pymux.sock.someone.0.lock").write_text("")

    assert _names() == ["pymux.sock.someone.0"]


def test_the_library_answers_the_same_list(sockets, a_dead_socket):
    "`Server.list()` reads this one, and the two must agree."
    from libpymux.sockets import socket_paths

    a_dead_socket("pymux.sock.someone.0")
    sockets(["pymux.sock.someone.1"])

    assert [os.path.basename(one) for one in socket_paths()] == [
        "pymux.sock.someone.1"
    ]


# ----------------------------------------------------------------------
# Taking a name back.


def test_a_new_server_takes_the_dead_name_rather_than_the_next(
    sockets, a_dead_socket, tmp_path
):
    """
    The numbers only ever went up, so a person read a bigger one in
    `PYMUX` after every crash, and a hundred of them stopped a server
    starting at all.
    """
    from libpymux.sockets import socket_directory
    from pymux.pipes.posix import _bind_posix_socket

    room = socket_directory()
    dead = os.path.join(room, "pymux.sock.someone.0")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(dead)
    listener.listen(1)
    listener.close()

    name, bound = _bind_posix_socket()
    try:
        assert os.path.basename(name) == "pymux.sock.someone.0"
    finally:
        bound.close()
        os.unlink(name)


def test_a_name_somebody_answers_on_is_left_alone(sockets, tmp_path):
    "The next index, which is what a second server has always taken."
    from libpymux.sockets import socket_directory
    from pymux.pipes.posix import _bind_posix_socket

    room = socket_directory()
    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    held.bind(os.path.join(room, "pymux.sock.someone.0"))
    held.listen(1)

    try:
        name, bound = _bind_posix_socket()
    finally:
        held.close()
    try:
        assert os.path.basename(name) == "pymux.sock.someone.1"
    finally:
        bound.close()
        os.unlink(name)
