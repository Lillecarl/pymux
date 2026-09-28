"""
The socket file a killed server leaves on its name.

A server that goes down cleanly takes its socket file away. One that
is killed -- SIGKILL, a cgroup taken down with the service -- cannot,
and the file stays. A bind on that name then answers EADDRINUSE for
ever, because nothing is ever going to take the file away, so
`pymux -S <path> new-session` could never start a server on a path its
own last server had used.

tmux unlinks a socket that nobody answers on and binds. These say that
pymux does the same, and that it does not do it to a name that
something is listening on, or to anything that is not a socket.
Lillecarl/pymux#453.
"""

import os
import socket
import stat

import pytest

from pymux.pipes.posix import bind_and_listen_on_posix_socket

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="a unix socket and flock"
)


def _killed_server(path):
    "A socket file with nothing listening on it, the way a kill leaves one."
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.listen(0)
    # Closed without `unlink`, which is the whole point: the file stays
    # and the next bind finds it.
    sock.close()
    assert os.path.exists(path)


def test_the_name_is_taken_from_a_server_that_has_gone(tmp_path):
    path = tmp_path / "pymux.sock"
    _killed_server(path)

    listener = bind_and_listen_on_posix_socket(str(path), lambda _connection: None)
    try:
        assert listener.socket_name == str(path)
        assert stat.S_ISSOCK(os.lstat(path).st_mode)

        # And it really answers, which the file alone does not say.
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
        finally:
            client.close()
    finally:
        listener.close()


def test_a_server_that_answers_keeps_its_name(tmp_path):
    """
    **The one case this must never get wrong.** Unlinking the socket of
    a server that is alive would leave it listening on a name nothing
    can reach, and give the name to somebody else.
    """
    path = tmp_path / "pymux.sock"
    alive = bind_and_listen_on_posix_socket(str(path), lambda _connection: None)
    held = os.lstat(path).st_ino

    try:
        with pytest.raises(OSError) as refused:
            bind_and_listen_on_posix_socket(str(path), lambda _connection: None)

        assert refused.value.errno == 98  # EADDRINUSE

        # **The inode, and not a connect.** The file the first server
        # bound is still the file on that name: an unlink and a bind
        # would answer on the name too, and only the inode says which
        # of the two happened. A connect here would also block for
        # ever -- a listener that nobody accepts on holds one waiting
        # client, and the probe inside the bind is already that one.
        assert os.lstat(path).st_ino == held
    finally:
        alive.close()


def test_a_file_that_is_not_a_socket_is_left_alone(tmp_path):
    "Something else at that path is not ours to delete."
    path = tmp_path / "pymux.sock"
    path.write_text("not a socket")

    with pytest.raises(OSError):
        bind_and_listen_on_posix_socket(str(path), lambda _connection: None)

    assert path.read_text() == "not a socket"
