"""
Reaching a server on another machine, over SSH.

`pymux -S ssh://carl@dynhetz/tmp/pymux.sock.carl.0 attach` draws the
panes here and runs them there. Lillecarl/pymux#90.

**The round trip runs against a real SSH stack.** An `asyncssh` server
stands in for sshd: it generates a host key, accepts one client key,
and answers `direct-streamlocal@openssh.com` by opening the unix socket
itself. So the packets go through key exchange, a channel and the
framing, which is what a mistake in any of those would show up in.

**It does not gate openssh interop.** Both sides are asyncssh here. The
thing this proves is that pymux's half is right: the address parses,
the channel carries the packets, and the server answers a client that
arrived this way. `PYMUX_ROUTE=ssh` in `tests/drive_with_pty.py`, over
a real sshd, is the check that would say the rest.

The tests are coroutines for the reason `test_command_mode.py` gives:
pymux carries no anyio and does not turn on `anyio_mode`.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from pymux.client.ssh import SshClient, is_an_ssh_url, the_ssh_target
from pymux.main import Pymux

from session import in_a_loop

A_PANE = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


# ----------------------------------------------------------------------
# The address.


def test_it_reads_a_host_and_a_path():
    target = the_ssh_target("ssh://dynhetz/tmp/pymux.sock.carl.0")

    assert target.host == "dynhetz"
    assert target.path == "/tmp/pymux.sock.carl.0"
    assert target.username is None
    assert target.port is None


def test_it_reads_a_user_and_a_port():
    target = the_ssh_target("ssh://carl@dynhetz:2222/tmp/sock")

    assert (target.username, target.host, target.port) == ("carl", "dynhetz", 2222)
    assert target.path == "/tmp/sock"


def test_a_path_with_more_than_one_part_survives():
    target = the_ssh_target("ssh://host/run/user/1000/pymux/sock")
    assert target.path == "/run/user/1000/pymux/sock"


def test_no_path_is_answered_after_connecting():
    """
    `ssh://host` is the whole address a person wants to type. Which
    socket that is depends on the other machine, so it is not decided
    here.
    """
    assert the_ssh_target("ssh://carl@dynhetz").path is None
    assert the_ssh_target("ssh://carl@dynhetz/").path is None


def test_a_named_path_still_wins():
    assert the_ssh_target("ssh://carl@dynhetz/run/sock").path == "/run/sock"


def test_the_fallback_is_the_first_server_of_the_user():
    "For a machine whose sshd offers no SFTP to list with."
    from pymux.client.ssh import the_default_socket

    assert the_default_socket("carl") == "/tmp/pymux.sock.carl.0"


def test_an_address_with_no_machine_is_refused():
    with pytest.raises(ValueError):
        the_ssh_target("ssh:///tmp/sock")


def test_a_path_is_not_an_address():
    with pytest.raises(ValueError):
        the_ssh_target("/tmp/pymux.sock.carl.0")


def test_what_counts_as_an_address():
    assert is_an_ssh_url("ssh://host/tmp/sock")
    assert not is_an_ssh_url("/tmp/pymux.sock.carl.0")
    assert not is_an_ssh_url("")
    assert not is_an_ssh_url(None)


def test_the_client_factory_picks_this_one():
    from pymux.client import create_client

    assert isinstance(create_client("ssh://host/tmp/sock"), SshClient)


def test_a_machine_is_never_somewhere_to_listen():
    """
    `_wait_for_server` looks for a file, and a machine is not one. It
    used to say "no server running" forever, which stopped
    `kill-server` over ssh.
    """
    from pymux.entry_points.run_pymux import _wait_for_server

    assert _wait_for_server("ssh://host/tmp/sock", timeout=0.01)
    assert not _wait_for_server("/tmp/a-socket-that-is-not-there", timeout=0.01)


# ----------------------------------------------------------------------
# The round trip.


def a_key(where: Path, name: str):
    "A key pair on disk, the way ssh wants one."
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    private = where / name
    private.write_text(key.export_private_key().decode())
    private.chmod(0o600)
    (where / ("%s.pub" % name)).write_text(key.export_public_key().decode())
    return private, where / ("%s.pub" % name)


async def an_ssh_server(where: Path, socket_path: str):
    """
    A server that answers one client key and forwards to a unix socket.

    `unix_connection_requested` is what makes this stand in for sshd:
    asyncssh opens the socket itself and joins the two ends, which is
    what `direct-streamlocal@openssh.com` asks for.
    """
    import asyncssh

    host_key, _ = a_key(where, "host")
    client_key, client_pub = a_key(where, "client")

    class OneSocket(asyncssh.SSHServer):
        def connection_made(self, conn) -> None:
            pass

        def begin_auth(self, username: str) -> bool:
            return True  # A key is required.

        def unix_connection_requested(self, dest_path: str):
            # True lets asyncssh open it. Only the one this test made,
            # so a fault cannot reach anything else on the machine.
            return dest_path == socket_path

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=OneSocket,
        server_host_keys=[str(host_key)],
        authorized_client_keys=str(client_pub),
        # The subsystem that lists the sockets when the address named
        # none. sshd offers it; nothing is installed for it.
        sftp_factory=True,
    )
    port = server.get_addresses()[0][1]
    return server, port, str(client_key)


@in_a_loop
async def test_a_command_reaches_a_server_over_ssh(tmp_path=None):
    """
    The whole path: an address, a key exchange, a channel to the unix
    socket, and the packets of a command coming back.
    """
    import tempfile

    where = Path(tempfile.mkdtemp())
    socket_path = str(where / "pymux.sock")

    pymux = Pymux()
    pymux.listen_on_socket(socket_path)
    pymux.create_window(A_PANE)
    await asyncio.sleep(0.5)

    server, port, client_key = await an_ssh_server(where, socket_path)

    client = SshClient(
        "ssh://127.0.0.1:%d%s" % (port, socket_path),
        known_hosts=None,
        client_keys=[client_key],
        username="anybody",
    )

    said = []
    try:
        # A command whose answer is certainly not empty, so that an
        # empty one means the channel carried nothing.
        exit_code = await _what_it_says(
            client, "list-sessions -F '#{session_name}'", said
        )
    finally:
        server.close()
        pymux.stop()
        for window in list(pymux.arrangement.windows):
            for pane in list(window.panes):
                if not pane.process.is_terminated:
                    pane.process.kill()

    assert exit_code == 0
    assert "".join(said).strip() == pymux.session_name, said


@in_a_loop
async def test_an_address_with_no_path_finds_the_socket_itself():
    """
    `ssh://host` alone, and nothing runs on the far side to answer it.

    The socket is deliberately **not** number zero, so a fallback to
    "the first server of this user" would open nothing. What passes
    this test is the SFTP listing.
    """
    import getpass
    import tempfile

    where = Path(tempfile.mkdtemp())
    # Where a real server binds, because that is what the listing
    # looks for. Seven, so that the guess would miss.
    socket_path = "/tmp/pymux.sock.%s.7" % (getpass.getuser(),)
    Path(socket_path).unlink(missing_ok=True)

    pymux = Pymux()
    pymux.listen_on_socket(socket_path)
    pymux.create_window(A_PANE)
    await asyncio.sleep(0.5)

    server, port, client_key = await an_ssh_server(where, socket_path)

    client = SshClient(
        "ssh://127.0.0.1:%d" % (port,),
        known_hosts=None,
        client_keys=[client_key],
        username=getpass.getuser(),
    )
    assert client.target.path is None, "the address named no socket"

    said = []
    try:
        exit_code = await _what_it_says(
            client, "list-sessions -F '#{session_name}'", said
        )
    finally:
        server.close()
        pymux.stop()
        for window in list(pymux.arrangement.windows):
            for pane in list(window.panes):
                if not pane.process.is_terminated:
                    pane.process.kill()
        Path(socket_path).unlink(missing_ok=True)

    assert client.path == socket_path, client.path
    assert exit_code == 0
    assert "".join(said).strip() == pymux.session_name, said


async def _what_it_says(client, command, said) -> int:
    "Run a command over the channel, and collect what the server wrote."
    import io
    from contextlib import redirect_stdout

    held = io.StringIO()
    with redirect_stdout(held):
        exit_code = await client._run_command(command)
    said.append(held.getvalue())
    return exit_code
