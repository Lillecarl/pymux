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
    about the sockets a server leaves behind.
    """
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("getpass.getuser", lambda: "someone")

    open_sockets = []

    def make(names, gap=0.05):
        for name in names:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(tmp_path / name))
            listener.listen(1)
            open_sockets.append(listener)
            time.sleep(gap)

    yield make

    for listener in open_sockets:
        listener.close()


def _names():
    "The sockets that `list_socket_names` gives back, in its order."
    return [os.path.basename(path) for path in list_socket_names()]


def test_the_newest_server_comes_first(sockets):
    sockets(
        ["pymux.sock.someone.0", "pymux.sock.someone.1", "pymux.sock.someone.2"]
    )
    assert _names() == [
        "pymux.sock.someone.2",
        "pymux.sock.someone.1",
        "pymux.sock.someone.0",
    ]


def test_the_number_does_not_decide(sockets):
    "The oldest server can hold the highest number, after a restart."
    sockets(["pymux.sock.someone.7", "pymux.sock.someone.0"])
    assert _names()[0] == "pymux.sock.someone.0"


def test_another_user_is_not_listed(sockets):
    sockets(["pymux.sock.someone.0", "pymux.sock.somebody.0"])
    assert _names() == ["pymux.sock.someone.0"]
