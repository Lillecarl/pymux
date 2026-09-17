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

The tests are coroutines, which anyio's pytest plugin runs.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pymux.client.ssh import SshClient, is_ssh_url, ssh_target
from pymux.main import Pymux


PANE_COMMAND = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)


# ----------------------------------------------------------------------
# The address.


def test_it_reads_host_and_path():
    target = ssh_target("ssh://dynhetz/tmp/pymux.sock.carl.0")

    assert target.host == "dynhetz"
    assert target.path == "/tmp/pymux.sock.carl.0"
    assert target.username is None
    assert target.port is None


def test_it_reads_user_and_port():
    target = ssh_target("ssh://carl@dynhetz:2222/tmp/sock")

    assert (target.username, target.host, target.port) == ("carl", "dynhetz", 2222)
    assert target.path == "/tmp/sock"


def test_path_with_more_than_one_part_survives():
    target = ssh_target("ssh://host/run/user/1000/pymux/sock")
    assert target.path == "/run/user/1000/pymux/sock"


def test_no_path_is_answered_after_connecting():
    """
    `ssh://host` is the whole address a person wants to type. Which
    socket that is depends on the other machine, so it is not decided
    here.
    """
    assert ssh_target("ssh://carl@dynhetz").path is None
    assert ssh_target("ssh://carl@dynhetz/").path is None


def test_named_path_still_wins():
    assert ssh_target("ssh://carl@dynhetz/run/sock").path == "/run/sock"


def test_the_fallback_is_pinned():
    "The flat first server, for a far side older than `find`."
    from pymux.client.ssh import default_socket

    assert default_socket("carl") == "/tmp/pymux.sock.carl.0"


def test_the_client_asks_the_far_side_to_find():
    "The contract the far side answers: `pymux find` prints one path."
    from pymux.client.ssh import FIND_COMMAND

    assert FIND_COMMAND == "pymux find"


def test_address_with_no_machine_is_refused():
    with pytest.raises(ValueError):
        ssh_target("ssh:///tmp/sock")


def test_path_is_not_address():
    with pytest.raises(ValueError):
        ssh_target("/tmp/pymux.sock.carl.0")


def test_what_counts_as_address():
    assert is_ssh_url("ssh://host/tmp/sock")
    assert not is_ssh_url("/tmp/pymux.sock.carl.0")
    assert not is_ssh_url("")
    assert not is_ssh_url(None)


def test_client_factory_picks_this_one():
    from pymux.client import create_client

    assert isinstance(create_client("ssh://host/tmp/sock"), SshClient)


def test_machine_is_never_somewhere_to_listen():
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


def create_key(where: Path, name: str):
    "A key pair on disk, the way ssh wants one."
    import asyncssh

    key = asyncssh.generate_private_key("ssh-ed25519")
    private = where / name
    private.write_text(key.export_private_key().decode())
    private.chmod(0o600)
    (where / ("%s.pub" % name)).write_text(key.export_public_key().decode())
    return private, where / ("%s.pub" % name)


async def create_ssh_server(
    where: Path,
    socket_path: str,
    session_env: dict | None = None,
    allow_exec: bool = True,
):
    """
    A server that answers one client key and forwards to a unix socket.

    `unix_connection_requested` is what makes this stand in for sshd:
    asyncssh opens the socket itself and joins the two ends, which is
    what `direct-streamlocal@openssh.com` asks for.

    `session_env` is the environment a session runs its command in,
    and `None` inherits this process's, the way an sshd session
    inherits the machine's. The command really runs, through a shell:
    what the client reads back is the answer of a real `pymux find`,
    not a string this test made up.
    """
    import asyncssh

    host_key, _ = create_key(where, "host")
    client_key, client_pub = create_key(where, "client")

    async def answer_sessions(process) -> None:
        ran = await asyncio.create_subprocess_shell(
            process.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=session_env,
        )
        out, _ = await ran.communicate()
        process.stdout.write(out.decode("utf-8", "replace"))
        process.exit(0)

    class OneSocket(asyncssh.SSHServer):
        def connection_made(self, conn) -> None:
            pass

        def begin_auth(self, username: str) -> bool:
            return True  # A key is required.

        def unix_connection_requested(self, dest_path: str):
            # True lets asyncssh open it. Only the one this test made,
            # so a fault cannot reach anything else on the machine.
            return dest_path == socket_path

    options = dict(
        server_factory=OneSocket,
        server_host_keys=[str(host_key)],
        authorized_client_keys=str(client_pub),
    )
    if allow_exec:
        # The session that runs `pymux find`. sshd offers it; nothing
        # is installed for it.
        options["process_factory"] = answer_sessions

    server = await asyncssh.listen("127.0.0.1", 0, **options)
    port = server.get_addresses()[0][1]
    return server, port, str(client_key)


@asynccontextmanager
async def live_servers(socket_path: str, **server_options):
    """
    A pymux server bound at `socket_path`, serving, and an asyncssh
    server that forwards to it.

    The block runs with the pymux server, the asyncssh port and the
    client key. Everything made here is closed when the block ends.
    """
    import tempfile

    where = Path(tempfile.mkdtemp())

    pymux = Pymux()
    pymux.listen_on_socket(socket_path)

    # The bind is what names the server; the accepting is a task of
    # `running()`. A server that binds and does not serve takes no
    # client at all. Lillecarl/pymux#87.
    async with pymux.running():
        pymux.create_window(PANE_COMMAND)
        await asyncio.sleep(0.5)

        server, port, client_key = await create_ssh_server(
            where, socket_path, **server_options
        )
        try:
            yield pymux, port, client_key
        finally:
            server.close()
            pymux.stop()
            for window in list(pymux.arrangement.windows):
                for pane in list(window.panes):
                    if not pane.process.is_terminated:
                        pane.process.kill()


async def test_command_reaches_server_over_ssh():
    """
    The whole path: an address, a key exchange, a channel to the unix
    socket, and the packets of a command coming back.
    """
    import tempfile

    where = Path(tempfile.mkdtemp())
    socket_path = str(where / "pymux.sock")

    async with live_servers(socket_path) as (pymux, port, client_key):
        client = SshClient(
            "ssh://127.0.0.1:%d%s" % (port, socket_path),
            known_hosts=None,
            client_keys=[client_key],
            username="anybody",
        )

        said = []
        # A command whose answer is certainly not empty, so that an
        # empty one means the channel carried nothing.
        exit_code = await _what_it_says(
            client, "list-sessions -F '#{session_name}'", said
        )

    assert exit_code == 0
    assert "".join(said).strip() == pymux.session_name, said


async def test_a_server_in_its_room_is_found():
    """
    `ssh://host` with no path, and a server that bound in the per-UID
    room of Lillecarl/pymux#405.

    `pymux find` really runs, through a shell, on the far side of the
    fake sshd: its `$XDG_RUNTIME_DIR` is where this test put the
    room, so the finding and the binding happen on one side. The
    socket is deliberately not number zero, so the flat guess of a
    first server would miss. What passes this test is the command,
    and the channel opened to the path it printed.
    """
    import getpass
    import tempfile

    where = Path(tempfile.mkdtemp())
    room = where / ("pymux-%d" % os.getuid())
    room.mkdir(mode=0o700)
    socket_path = str(room / ("pymux.sock.%s.7" % (getpass.getuser(),)))

    room_env = {
        "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"],
        "XDG_RUNTIME_DIR": str(where),
    }

    async with live_servers(socket_path, session_env=room_env) as (
        pymux,
        port,
        client_key,
    ):
        client = SshClient(
            "ssh://127.0.0.1:%d" % (port,),
            known_hosts=None,
            client_keys=[client_key],
            username=getpass.getuser(),
        )
        assert client.target.path is None, "the address named no path"

        said = []
        exit_code = await _what_it_says(
            client, "list-sessions -F '#{session_name}'", said
        )

    assert client.path == socket_path, client.path
    assert exit_code == 0
    assert "".join(said).strip() == pymux.session_name, said


async def test_the_flat_guess_when_find_cannot_run(monkeypatch):
    """
    A server whose sessions refuse to run commands, or a pymux over
    there that predates `find`: the flat first server is the guess
    that is left.

    The socket is deliberately not where `pymux find` would have
    looked, so what passes this test is the guess, and nothing else.
    """
    import getpass
    import tempfile

    where = Path(tempfile.mkdtemp())
    socket_path = str(where / ("pymux.sock.%s.0" % (getpass.getuser(),)))

    monkeypatch.setattr(
        "pymux.client.ssh.default_socket",
        lambda username: socket_path,
    )

    async with live_servers(socket_path, allow_exec=False) as (
        pymux,
        port,
        client_key,
    ):
        client = SshClient(
            "ssh://127.0.0.1:%d" % (port,),
            known_hosts=None,
            client_keys=[client_key],
            username=getpass.getuser(),
        )
        assert client.target.path is None, "the address named no path"

        said = []
        exit_code = await _what_it_says(
            client, "list-sessions -F '#{session_name}'", said
        )

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
