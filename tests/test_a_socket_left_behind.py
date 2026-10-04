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

from __future__ import annotations

import errno
import os
import socket
import stat

import pytest

from pymux.pipes.posix import bind_and_listen_on_posix_socket

pytestmark = pytest.mark.skipif(os.name == "nt", reason="a unix socket and flock")


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


# ----------------------------------------------------------------------
# The lock beside the socket.
#
# The unlink above is safe because it happens under `<path>.lock`, and
# that lock file is a name in the same directory: for `pymux -S
# /tmp/shared.sock` it is a name another account can get to first. The
# per-UID room is 0700, so only an explicitly named socket in a
# directory somebody else can write is exposed -- the shape of
# Lillecarl/pymux#405 again. Lillecarl/pymux#455.


def test_a_lock_somebody_pointed_elsewhere_is_not_opened(tmp_path):
    """
    `os.open` follows a symlink, so a squatter got an flock on a file
    of their choosing and could hold a server start for as long as they
    liked.
    """
    path = tmp_path / "pymux.sock"
    _killed_server(path)

    theirs = tmp_path / "theirs"
    theirs.write_text("not pymux's")
    os.symlink(theirs, str(path) + ".lock")

    with pytest.raises(OSError) as refused:
        bind_and_listen_on_posix_socket(str(path), lambda _connection: None)

    assert refused.value.errno == errno.ELOOP
    assert theirs.read_text() == "not pymux's"


def test_a_lock_file_of_its_own_still_opens(tmp_path):
    "The guard costs nothing honest: nobody symlinks their own lock."
    path = tmp_path / "pymux.sock"
    _killed_server(path)
    (tmp_path / "pymux.sock.lock").write_text("")

    listener = bind_and_listen_on_posix_socket(str(path), lambda _c: None)
    try:
        assert stat.S_ISSOCK(os.stat(path).st_mode)
    finally:
        listener.close()
