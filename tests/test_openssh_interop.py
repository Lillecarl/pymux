"""
The ssh:// client against a real openssh sshd. Lillecarl/pymux#436.

`test_ssh_client.py` says what this is for: both sides of that suite
are asyncssh, so it does not gate openssh interop. Here the far side is
the `sshd` binary itself, and what is judged is the three things the
client asks of it -- the unix socket channel it has always used, and
the two forward directions.

**The sandbox runs a whole sshd.** It needs three things that are easy
to get wrong, and each one cost a run to find:

- **An absolute path.** `sshd` refuses to start when it was found on
  PATH: "sshd requires execution with an absolute path". `PYMUX_SSHD`
  carries the store path, and the check sets it.
- **Its own everything.** A host key, an authorised key and a config
  made here, so nothing of the machine's is read and nothing of the
  user's is trusted. It listens on loopback.
- **A free port picked before it starts.** sshd takes a port in its
  config and cannot be asked for any free one.
- **A user with a shell that exists.** The sandbox gives `nixbld` the
  shell `/noshell`, and sshd refuses: "User nixbld not allowed because
  shell /noshell does not exist". There is no option to turn that check
  off, so `nss_wrapper` puts a passwd database in front of it with the
  same name and uid and a shell that is really there.

`PYMUX_SSHD` unset skips the module, so a plain unit run does not try.

**The echo answers in upper case.** A forward that looped back to the
caller would return the bytes it was sent, and a test that only
checked "something came back" would pass on it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute

from pymux.client.forwards import Forwards
from pymux.forwarding import ANY_PORT, Direction, Forward

#: Where the check puts the sshd it wants used. Absolute, because sshd
#: refuses to run any other way.
SSHD = os.environ.get("PYMUX_SSHD", "")

#: The passwd database sshd is given instead of the sandbox's, and the
#: shell it names. Both come from the check.
NSS_WRAPPER = os.environ.get("PYMUX_NSS_WRAPPER", "")
LOGIN_SHELL = os.environ.get("PYMUX_LOGIN_SHELL", "/bin/sh")

pytestmark = pytest.mark.skipif(
    not SSHD, reason="PYMUX_SSHD names no sshd. checks.pymux-openssh sets it."
)

#: How long to wait for sshd to answer on its port, in seconds.
SSHD_STARTS_IN = 10.0


def _free_port() -> int:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        return held.getsockname()[1]


def _keys(where: Path) -> tuple[Path, Path]:
    "A host key and a client key, made here so nothing else is trusted."
    made = []
    for name in ("host_key", "client_key"):
        path = where / name
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "", "-q"],
            check=True,
        )
        made.append(path)
    return made[0], made[1]


def _a_user_with_a_shell(where: Path) -> str:
    """
    The name sshd will accept for this uid, and the databases that make
    it acceptable.

    sshd refuses a user whose shell is not a real file, and the sandbox
    gives every uid `/noshell`. `nss_wrapper` answers `getpwnam` from a
    file instead, so the same uid gets the same name and a shell that
    exists. Outside the sandbox there is nothing to fix and the real
    database is used.
    """
    import grp
    import pwd

    me = pwd.getpwuid(os.getuid())

    if not NSS_WRAPPER:
        return me.pw_name

    passwd = where / "passwd"
    passwd.write_text(
        "%s:x:%d:%d:pymux test:%s:%s\n"
        % (me.pw_name, me.pw_uid, me.pw_gid, where, LOGIN_SHELL)
    )

    group = where / "group"
    try:
        group_name = grp.getgrgid(me.pw_gid).gr_name
    except KeyError:
        group_name = me.pw_name
    group.write_text("%s:x:%d:\n" % (group_name, me.pw_gid))

    os.environ["LD_PRELOAD"] = NSS_WRAPPER
    os.environ["NSS_WRAPPER_PASSWD"] = str(passwd)
    os.environ["NSS_WRAPPER_GROUP"] = str(group)

    return me.pw_name


@pytest.fixture(scope="module")
def sshd() -> "tuple[int, str]":
    """
    A real sshd on loopback, and the key that authenticates to it.

    Module scoped: a key exchange and a process start cost more than
    every test here put together, and nothing a test does changes the
    server.
    """
    where = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-openssh"
    shutil.rmtree(where, ignore_errors=True)
    where.mkdir(parents=True)

    host_key, client_key = _keys(where)
    port = _free_port()
    who = _a_user_with_a_shell(where)

    (where / "sshd_config").write_text(
        "\n".join(
            [
                "Port %d" % (port,),
                "ListenAddress 127.0.0.1",
                "HostKey %s" % (host_key,),
                "AuthorizedKeysFile %s.pub" % (client_key,),
                "PidFile %s" % (where / "sshd.pid",),
                # The sandbox owns these files and the modes are its
                # own, which sshd would otherwise refuse.
                "StrictModes no",
                "UsePAM no",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                # Enough to name a refused authentication, which
                # reaches the client as "Connection lost" and nothing
                # else.
                "LogLevel DEBUG1",
                # The three the client depends on. All are openssh's
                # defaults; naming them means this says what it relies
                # on rather than inheriting it, and a future openssh
                # that changes a default fails here loudly.
                "AllowTcpForwarding yes",
                "AllowStreamLocalForwarding yes",
                "GatewayPorts no",
                "",
            ]
        )
    )

    # **Its log goes to a file, and the teardown prints it.** A refused
    # authentication reaches the client as "Connection lost" and
    # nothing else, so without this a failure here says only that
    # something went wrong on the other side.
    # **Never `-d` here.** A debug flag on the command line also means
    # "serve one connection and exit", and the readiness probe below
    # spends it: every test then gets a refused connection. The log
    # level belongs in the config, where it says nothing about how many
    # connections to take.
    log = where / "sshd.log"
    running = subprocess.Popen(
        [SSHD, "-f", str(where / "sshd_config"), "-D", "-e"],
        stdout=log.open("w"),
        stderr=subprocess.STDOUT,
        text=True,
    )

    ends_at = time.monotonic() + SSHD_STARTS_IN
    while time.monotonic() < ends_at:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        if running.poll() is not None:
            raise RuntimeError("sshd died: %s" % (log.read_text(),))
        time.sleep(0.05)
    else:
        running.kill()
        raise RuntimeError("sshd never listened on %d." % (port,))

    try:
        yield port, str(client_key), who
    finally:
        running.terminate()
        try:
            running.wait(timeout=5)
        except subprocess.TimeoutExpired:
            running.kill()
        # pytest keeps this and shows it only when something failed,
        # which is exactly when the far side's reason is wanted.
        print("--- sshd said ---\n%s" % (log.read_text(),))


def _connect_to(sshd):
    import asyncssh

    port, client_key, who = sshd
    return asyncssh.connect(
        "127.0.0.1",
        port=port,
        known_hosts=None,
        client_keys=[client_key],
        username=who,
    )


async def _echoing(tasks) -> int:
    "Start a TCP echo that answers in upper case; give back its port."

    async def handle(stream) -> None:
        async with stream:
            try:
                async for chunk in stream:
                    await stream.send(chunk.upper())
            except anyio.EndOfStream:
                pass

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    tasks.start_soon(listener.serve, handle)
    return listener.extra(SocketAttribute.local_address)[1]


async def _spoken_through(port: int, said: bytes = b"hello") -> bytes:
    async with await anyio.connect_tcp("127.0.0.1", port) as stream:
        await stream.send(said)
        return await stream.receive()


async def test_openssh_takes_a_local_forward(sshd):
    """
    `-L` through the real thing: a `direct-tcpip` channel that openssh
    opens, rather than the asyncssh server standing in for it.
    """
    async with anyio.create_task_group() as tasks:
        echo_port = await _echoing(tasks)

        async with _connect_to(sshd) as connection:
            forwards = Forwards()
            opened = await forwards.add(
                connection,
                Forward(Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port),
            )

            assert opened.error == "", opened.error
            assert opened.port != ANY_PORT
            assert await _spoken_through(opened.port) == b"HELLO"

            forwards.close()

        tasks.cancel_scope.cancel()


async def test_openssh_takes_a_remote_forward(sshd):
    """
    `-R` through the real thing: a `tcpip-forward` global request that
    openssh answers, and the port it chose coming back in the reply.
    """
    async with anyio.create_task_group() as tasks:
        echo_port = await _echoing(tasks)

        async with _connect_to(sshd) as connection:
            forwards = Forwards()
            opened = await forwards.add(
                connection,
                Forward(
                    Direction.REMOTE, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port
                ),
            )

            assert opened.error == "", opened.error
            # openssh puts the port it bound in the reply, and nothing
            # else can tell the person where to connect.
            assert opened.port != ANY_PORT
            assert await _spoken_through(opened.port) == b"HELLO"

            forwards.close()

        tasks.cancel_scope.cancel()


async def test_openssh_takes_the_unix_channel_the_client_lives_on(sshd):
    """
    `direct-streamlocal@openssh.com`, which is how every `ssh://` attach
    reaches the server's socket.

    **It had no test against openssh before this one.** The whole
    client rests on it, and the suite that covers it talks to asyncssh
    on both sides.
    """
    where = Path(os.environ.get("TMPDIR", "/tmp")) / "pymux-openssh"
    socket_path = str(where / "unix.sock")

    async def handle(stream) -> None:
        async with stream:
            try:
                async for chunk in stream:
                    await stream.send(chunk.upper())
            except anyio.EndOfStream:
                pass

    listener = await anyio.create_unix_listener(socket_path)

    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, handle)

            async with _connect_to(sshd) as connection:
                reader, writer = await connection.open_unix_connection(socket_path)
                writer.write(b"hello")
                assert await reader.read(5) == b"HELLO"
                writer.close()

            tasks.cancel_scope.cancel()
    finally:
        await listener.aclose()
        Path(socket_path).unlink(missing_ok=True)


async def test_a_forward_comes_back_on_a_new_openssh_connection(sshd):
    """
    The reconnect promise, against openssh rather than a stand-in. The
    wanted set opens again on the next connection, and the port it had
    is free for it to take.
    """
    async with anyio.create_task_group() as tasks:
        echo_port = await _echoing(tasks)
        forwards = Forwards()
        wanted = Forward(
            Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port
        )

        async with _connect_to(sshd) as first:
            was = await forwards.add(first, wanted)
            assert was.error == "", was.error

        # The listeners are the table's own, so the link going does not
        # close them. `SshClient._attached` does that as an attachment
        # ends; here the connection is closed under the table, and the
        # socket is left with nowhere to carry to.
        with pytest.raises((anyio.BrokenResourceError, anyio.EndOfStream, OSError)):
            await _spoken_through(was.port)

        forwards.close()

        async with _connect_to(sshd) as second:
            await forwards.reopen(second)
            again = forwards.opened()[0]

            assert again.error == "", again.error
            assert await _spoken_through(again.port) == b"HELLO"

            forwards.close()

        tasks.cancel_scope.cancel()


async def test_openssh_narrows_a_remote_forward_rather_than_refusing_it(sshd):
    """
    **It binds loopback and reports success.** A person who asked for
    `0.0.0.0` gets a listener nothing off the machine can reach, and no
    error says so. Measured here against `GatewayPorts no`, which is
    openssh's default.

    Nothing downstream can find out either: the `tcpip-forward` reply
    carries a port and no address. That is why `forward-port` says so
    in the question instead of claiming the address afterwards --
    `the_far_side_may_narrow` holds the rule.
    Lillecarl/pymux#440, Lillecarl/pymux#444.
    """
    async with anyio.create_task_group() as tasks:
        echo_port = await _echoing(tasks)

        async with _connect_to(sshd) as connection:
            forwards = Forwards()
            opened = await forwards.add(
                connection,
                Forward(Direction.REMOTE, "0.0.0.0", ANY_PORT, "127.0.0.1", echo_port),
            )

            assert opened.error == "", opened.error
            # It is reachable on loopback, which is what openssh did
            # with the request rather than what was asked for.
            assert await _spoken_through(opened.port) == b"HELLO"

            forwards.close()

        tasks.cancel_scope.cancel()
