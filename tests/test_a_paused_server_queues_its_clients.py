"""
A server that takes no client for a moment queues the ones that come.

An upgrade takes no client for about a second, from the pause to the
new image's first accept, and every client comes back in that second.
sshd connects without blocking, so a full queue refuses it and an SSH
client hears "open failed". At a backlog of 0 the queue held one.
Lillecarl/pymux#750.
"""

from __future__ import annotations

import socket

from pymux.core.main import Pymux
from pymux.pipes.posix import bind_and_listen_on_posix_socket

#: The clients that come back at once: more than a person runs.
CLIENTS = 10


def connect_without_blocking(path: str) -> list[socket.socket]:
    "Connect `CLIENTS` times the way sshd does. A full queue raises."
    connected = []
    try:
        for _ in range(CLIENTS):
            one = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connected.append(one)
            one.setblocking(False)
            one.connect(path)
    except BaseException:
        for one in connected:
            one.close()
        raise
    return connected


def test_a_listener_nobody_accepts_on_queues_every_client(tmp_path):
    listener = bind_and_listen_on_posix_socket(str(tmp_path / "s"), lambda _connection: None)
    try:
        for one in connect_without_blocking(listener.socket_name):
            one.close()
    finally:
        listener.socket.close()


def test_an_adopted_listener_takes_the_new_backlog(tmp_path):
    "A socket an older build bound carries that build's backlog across every exec."
    path = str(tmp_path / "s")
    old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old.bind(path)
    old.listen(0)
    pymux = Pymux()
    pymux.socket_name = path
    pymux.serving.adopt_listener(old.detach())
    try:
        for one in connect_without_blocking(path):
            one.close()
    finally:
        pymux.listener.socket.close()
