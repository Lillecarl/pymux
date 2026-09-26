"""
Forwarded ports over the `ssh://` client. Lillecarl/pymux#436.

The spelling is judged on its own, because it is pure. The table is
judged against a real asyncssh server and a real echo server: a forward
passes when bytes written to the listening port come back changed, so
nothing passes by writing to itself.

**The echo answers in upper case on purpose.** A forward that quietly
loops back to the caller would return the same bytes, and a test that
only checked "something came back" would pass on it.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketAttribute

from pymux.client.forwards import Forwards
from pymux.forwarding import (
    ANY_PORT,
    BadForward,
    Direction,
    Forward,
    LOOPBACK,
    loopback_port,
    parse_forward,
    parse_listen,
    the_far_side_may_narrow,
    with_port,
)

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size

from pymux.client.ssh import SshClient

from session import once, over_connection
from test_ssh_client import create_ssh_server, live_servers

SIZE = Size(rows=24, columns=80)

# ----------------------------------------------------------------------
# The spelling.


def test_three_parts_listen_on_loopback():
    """
    **The default is loopback, never every interface.** openssh
    defaults the same way: a forward reaches a service that chose to
    listen on loopback, and putting it on a laptop's wifi address
    publishes it to whoever else is on that network.
    """
    forward = parse_forward(Direction.LOCAL, "8080:localhost:3000")

    assert forward.listen_host == LOOPBACK
    assert forward.listen_port == 8080
    assert forward.dest_host == "localhost"
    assert forward.dest_port == 3000


def test_four_parts_name_where_to_listen():
    forward = parse_forward(Direction.REMOTE, "0.0.0.0:9222:localhost:9222")

    assert forward.listen_host == "0.0.0.0"
    assert forward.listen_port == 9222


def test_an_empty_listen_host_is_still_loopback():
    "`:8080:localhost:3000` names no address, so it is not every address."
    assert parse_forward(Direction.LOCAL, ":8080:localhost:3000").listen_host == LOOPBACK


def test_zero_asks_for_any_port():
    assert parse_forward(Direction.LOCAL, "0:localhost:3000").listen_port == ANY_PORT


@pytest.mark.parametrize(
    "spec",
    [
        "8080",
        "8080:localhost",
        "a:b:c:d:e",
        "8080:localhost:nonsense",
        "70000:localhost:3000",
        "8080::3000",
    ],
)
def test_a_spelling_that_cannot_be_read_says_so(spec):
    "Every one of these used to be a forward that silently did nothing."
    with pytest.raises(BadForward):
        parse_forward(Direction.LOCAL, spec)


def test_only_a_remote_bind_off_loopback_may_be_narrowed():
    """
    `GatewayPorts` governs the remote listener and nothing else, so a
    `-L` and a loopback `-R` are exactly what they say.
    """
    assert the_far_side_may_narrow(parse_forward(Direction.REMOTE, "0.0.0.0:22:h:22"))
    assert not the_far_side_may_narrow(parse_forward(Direction.REMOTE, "22:h:22"))
    assert not the_far_side_may_narrow(
        parse_forward(Direction.LOCAL, "0.0.0.0:22:h:22")
    )


# ----------------------------------------------------------------------
# The URL a pane printed. Lillecarl/pymux#437.


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://localhost:3000/", ("localhost", 3000)),
        ("http://127.0.0.1:8080", ("127.0.0.1", 8080)),
        ("https://localhost:8443/app?x=1#y", ("localhost", 8443)),
        ("http://[::1]:5000/", ("::1", 5000)),
        ("ws://localhost:9222/devtools", ("localhost", 9222)),
        # No port, so the scheme says which one.
        ("http://localhost/", ("localhost", 80)),
        ("https://localhost/", ("localhost", 443)),
        # Not this machine's business.
        ("https://example.com/", None),
        ("https://example.com:8080/", None),
        # Nothing to forward: no scheme that implies a port, and none given.
        ("file:///tmp/page.html", None),
        ("mailto:someone@localhost", None),
        ("localhost:3000", None),
        # A netloc that is not a netloc.
        ("http://localhost:nonsense/", None),
    ],
)
def test_only_a_loopback_url_names_a_port_to_forward(url, expected):
    """
    **The answer is `None` for nearly every URL**, and it has to be: a
    forward binds a port on the machine somebody is sitting at, and an
    ordinary web address needs none.
    """
    assert loopback_port(url) == expected


def test_a_url_moves_to_the_port_that_was_free():
    "The path, the query and the fragment are the person's, so they stay."
    assert (
        with_port("http://localhost:3000/app?x=1#y", 54321)
        == "http://localhost:54321/app?x=1#y"
    )
    assert with_port("http://[::1]:5000/", 54321) == "http://[::1]:54321/"
    assert with_port("http://carl@localhost/", 8080) == "http://carl@localhost:8080/"


def test_the_listening_end_alone_names_a_forward():
    assert parse_listen("8080") == (LOOPBACK, 8080)
    assert parse_listen("0.0.0.0:8080") == ("0.0.0.0", 8080)


def test_a_forward_spells_itself_back():
    forward = parse_forward(Direction.LOCAL, "8080:localhost:3000")
    assert forward.spell() == "-L localhost:8080:localhost:3000"
    assert parse_forward(Direction.REMOTE, "9222:localhost:9222").spell().startswith("-R")


# ----------------------------------------------------------------------
# The table, against a real connection.


@asynccontextmanager
async def echoing():
    """
    A TCP server on loopback that answers in upper case.

    The change is what proves the bytes went through it: a forward that
    looped back to the caller would return them as they were sent.
    """

    async def handle(stream) -> None:
        async with stream:
            try:
                async for chunk in stream:
                    await stream.send(chunk.upper())
            except anyio.EndOfStream:
                pass

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(SocketAttribute.local_address)[1]

    # **Cancel before closing, never the other way round.** `serve` is
    # parked in `accept`, and closing the listener under it raises
    # `ClosedResourceError` out of the task group -- which fails the
    # test on the way out, after its body passed.
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(listener.serve, handle)
            try:
                yield port
            finally:
                tasks.cancel_scope.cancel()
    finally:
        await listener.aclose()


@asynccontextmanager
async def ssh_connection(**server_options):
    "An asyncssh client joined to the fake sshd of the ssh client tests."
    import tempfile

    import asyncssh

    where = Path(tempfile.mkdtemp())
    socket_path = str(where / "pymux.sock")
    server, port, client_key = await create_ssh_server(
        where, socket_path, allow_exec=False, **server_options
    )

    try:
        async with asyncssh.connect(
            "127.0.0.1",
            port=port,
            known_hosts=None,
            client_keys=[client_key],
            username="anybody",
        ) as connection:
            yield connection
    finally:
        server.close()


def _free_port() -> int:
    """
    A port nothing is listening on.

    Bound and released, which is the usual small race: nothing else in
    this check binds loopback ports, and a named port is what the
    replacing test needs to mean anything.
    """
    import socket

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        return held.getsockname()[1]


async def _spoken_through(port: int, said: bytes = b"hello") -> bytes:
    "Write to a forwarded port, and read what the echo answered."
    async with await anyio.connect_tcp("127.0.0.1", port) as stream:
        await stream.send(said)
        return await stream.receive()


async def test_a_local_forward_carries_bytes():
    """
    `-L`: the client listens here and the far sshd opens the
    destination. The whole path is exercised -- a `direct-tcpip`
    channel, the server's `connection_requested`, and the echo.
    """
    async with echoing() as echo_port, ssh_connection() as connection:
        forwards = Forwards()
        opened = await forwards.add(
            connection,
            Forward(Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port),
        )

        assert opened.error == ""
        assert opened.port != ANY_PORT, "A bound listener has to report its port."
        assert await _spoken_through(opened.port) == b"HELLO"

        forwards.close()


async def test_a_remote_forward_carries_bytes():
    """
    `-R`: the far sshd listens and hands each connection back down the
    link, where this side opens the destination. It is what a program
    on the workstation needs to reach Chrome's CDP port on the laptop.
    """
    async with echoing() as echo_port, ssh_connection() as connection:
        forwards = Forwards()
        opened = await forwards.add(
            connection,
            Forward(Direction.REMOTE, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port),
        )

        assert opened.error == ""
        assert opened.port != ANY_PORT
        assert await _spoken_through(opened.port) == b"HELLO"

        forwards.close()


async def test_a_refused_remote_forward_is_a_reason_and_not_a_fault():
    """
    `AllowTcpForwarding no` is what the rig does with
    `allow_forward=False`.

    **It must not raise.** A forward is opened again on every
    reconnect, and one the far side refuses would otherwise take the
    person's panes down with it. The reason lands in the table, where
    `list-forwards` shows it.
    """
    async with echoing() as echo_port, ssh_connection(allow_forward=False) as connection:
        forwards = Forwards()
        opened = await forwards.add(
            connection,
            Forward(Direction.REMOTE, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port),
        )

        assert opened.error, "A refusal has to say why."
        assert forwards.opened()[0].error == opened.error


async def test_a_refused_local_forward_opens_and_fails_per_connection():
    """
    **A `-L` forward cannot know that the far side will refuse it**, and
    this records that rather than wishing otherwise.

    The two directions ask different things. `-R` is a `tcpip-forward`
    global request, which the sshd answers at once, so a refusal is a
    failure to open. `-L` binds a port on this machine and nothing
    crosses the link until somebody connects to it -- the `direct-tcpip`
    channel is opened per connection, and that is the first moment the
    sshd can say no.

    So the listener is open and the table is clean, and every connection
    through it dies. `list-forwards` cannot show a reason here; the
    person finds out by using it. Lillecarl/pymux#436.
    """
    async with echoing() as echo_port, ssh_connection(allow_forward=False) as connection:
        forwards = Forwards()
        opened = await forwards.add(
            connection,
            Forward(Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port),
        )

        assert opened.error == "", "A local bind does not ask the far side."

        # The refusal arrives as a reset: asyncssh accepted the
        # connection here, asked for the channel, was told no, and had
        # nothing left to do but drop what it had accepted.
        with pytest.raises((anyio.BrokenResourceError, anyio.EndOfStream, OSError)):
            await _spoken_through(opened.port)

        forwards.close()


async def test_the_wanted_set_comes_back_on_a_new_connection():
    """
    **The reason this class exists.** `_link_again` builds a new
    connection and every listener of the old one dies with it. openssh
    loses its forwards there for good; this opens them again.
    """
    async with echoing() as echo_port:
        forwards = Forwards()
        wanted = Forward(
            Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port
        )

        async with ssh_connection() as first:
            was = await forwards.add(first, wanted)
            assert was.error == ""

        # The link is gone, and with it the listener.
        with pytest.raises(OSError):
            await _spoken_through(was.port)

        async with ssh_connection() as second:
            await forwards.reopen(second)
            again = forwards.opened()[0]

            assert again.error == "", again.error
            assert await _spoken_through(again.port) == b"HELLO"

            forwards.close()


async def test_a_reconnect_that_changed_nothing_says_nothing():
    "A named port comes back as itself, so there is nothing to tell."
    async with echoing() as echo_port:
        forwards = Forwards()
        wanted = Forward(
            Direction.LOCAL, "127.0.0.1", _free_port(), "127.0.0.1", echo_port
        )

        async with ssh_connection() as first:
            assert (await forwards.add(first, wanted)).error == ""

        async with ssh_connection() as second:
            assert await forwards.reopen(second) == ""
            forwards.close()


async def test_a_forward_that_came_back_on_another_port_says_so():
    """
    **The case that costs a person their session otherwise.** A forward
    that asked for any free port rarely gets the same one twice, and
    whatever was pointed at the old number is pointing at nothing.
    Lillecarl/pymux#442.
    """
    async with echoing() as echo_port:
        forwards = Forwards()
        wanted = Forward(
            Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port
        )

        async with ssh_connection() as first:
            was = await forwards.add(first, wanted)
            assert was.error == ""

        async with ssh_connection() as second:
            said = await forwards.reopen(second)
            now = forwards.opened()[0].port

            if now == was.port:
                pytest.skip("the operating system handed back the same port")

            assert str(now) in said, said
            assert "moved" in said, said
            forwards.close()


async def test_a_forward_that_could_not_come_back_says_why():
    "A port taken while the link was down is a reason, not a silence."
    async with echoing() as echo_port:
        forwards = Forwards()
        port = _free_port()
        wanted = Forward(Direction.LOCAL, "127.0.0.1", port, "127.0.0.1", echo_port)

        async with ssh_connection() as first:
            assert (await forwards.add(first, wanted)).error == ""

        # Somebody else took it while the link was down.
        squatter = await anyio.create_tcp_listener(
            local_host="127.0.0.1", local_port=port
        )
        try:
            async with ssh_connection() as second:
                said = await forwards.reopen(second)

                assert "Could not forward" in said, said
                assert str(port) in said, said
        finally:
            await squatter.aclose()


async def test_a_forward_that_is_removed_does_not_come_back():
    "Removing stops the wanting, so the next reconnect does not undo it."
    async with echoing() as echo_port:
        forwards = Forwards()
        wanted = Forward(
            Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port
        )

        async with ssh_connection() as first:
            await forwards.add(first, wanted)
            assert len(forwards) == 1

            gone = forwards.remove(
                (Direction.LOCAL, "127.0.0.1", ANY_PORT)
            )
            assert gone == wanted
            assert len(forwards) == 0

        async with ssh_connection() as second:
            await forwards.reopen(second)
            assert forwards.opened() == []


async def test_asking_twice_leaves_one_listener():
    """
    Two listeners on one port is a race nobody wins, so the second ask
    replaces the first rather than joining it.

    **A named port is what proves it.** With `add` not closing the one
    that was there, the second bind is the same address twice and the
    operating system refuses it -- so the empty error is the whole
    assertion.
    """
    async with echoing() as echo_port, ssh_connection() as connection:
        wanted = Forward(Direction.LOCAL, "127.0.0.1", _free_port(), "127.0.0.1", echo_port)

        forwards = Forwards()
        first = await forwards.add(connection, wanted)
        assert first.error == "", first.error

        again = await forwards.add(connection, wanted)

        assert again.error == "", again.error
        assert len(forwards) == 1, "The same listening end replaces, never adds."
        assert await _spoken_through(again.port) == b"HELLO"

        forwards.close()


# ----------------------------------------------------------------------
# A forward pymux opened for a URL. Lillecarl/pymux#437.


@contextmanager
def _clock(forwards: Forwards):
    "Drive the idle time by hand, rather than by waiting for it."
    stands_at = [1000.0]
    forwards._now = lambda: stands_at[0]
    yield stands_at


async def test_a_url_forward_moves_when_its_port_is_taken():
    """
    **The URL's port is a wish and not a promise.** Something on this
    machine already listens on it, and the page still has to open, so
    the forward takes any free port and the URL is rewritten to it.

    A person's forward never does this: they named that number.
    """
    async with echoing() as echo_port, ssh_connection() as connection:
        asked = _free_port()
        squatter = await anyio.create_tcp_listener(
            local_host="127.0.0.1", local_port=asked
        )
        try:
            forwards = Forwards()
            opened = await forwards.add(
                connection,
                Forward(Direction.LOCAL, "127.0.0.1", asked, "127.0.0.1", echo_port),
                idle=60,
            )

            assert opened.error == "", opened.error
            assert opened.port != asked
            assert await _spoken_through(opened.port) == b"HELLO"

            forwards.close()
        finally:
            await squatter.aclose()


async def test_two_url_forwards_that_both_moved_keep_their_own_entries():
    """
    **The bug that a shared name would give.** A forward is known by
    where it listens, so two that both fell back to any free port would
    claim one entry and the second would close the first. The name stays
    the port the URL asked for, which is different for each.
    Lillecarl/pymux#441.
    """
    async with echoing() as echo_port, ssh_connection() as connection:
        first, second = _free_port(), _free_port()
        assert first != second

        squatters = [
            await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=port)
            for port in (first, second)
        ]
        try:
            forwards = Forwards()
            one = await forwards.add(
                connection,
                Forward(Direction.LOCAL, "127.0.0.1", first, "127.0.0.1", echo_port),
                idle=60,
            )
            two = await forwards.add(
                connection,
                Forward(Direction.LOCAL, "127.0.0.1", second, "127.0.0.1", echo_port),
                idle=60,
            )

            assert len(forwards) == 2
            assert one.port != two.port
            assert await _spoken_through(one.port) == b"HELLO"
            assert await _spoken_through(two.port) == b"HELLO"

            forwards.close()
        finally:
            for squatter in squatters:
                await squatter.aclose()


async def test_a_url_forward_leaves_a_typed_one_alone():
    """
    Somebody typed `forward-port -L 8080:...` for a reason. A pane then
    printing `http://localhost:8080` must not silently point that port
    somewhere else.
    """
    async with echoing() as echo_port, ssh_connection() as connection:
        forwards = Forwards()
        port = _free_port()
        typed = Forward(Direction.LOCAL, "127.0.0.1", port, "127.0.0.1", echo_port)
        assert (await forwards.add(connection, typed)).error == ""

        refused = await forwards.add(
            connection,
            Forward(Direction.LOCAL, "127.0.0.1", port, "127.0.0.1", echo_port + 1),
            idle=60,
        )

        assert refused.error, "A URL forward has to say why it did nothing."
        assert len(forwards) == 1
        assert await _spoken_through(port) == b"HELLO", "The typed forward stands."

        forwards.close()


async def test_the_same_url_twice_keeps_the_one_listener():
    "Opening a page again is a use of the forward, not a second one."
    async with echoing() as echo_port, ssh_connection() as connection:
        forwards = Forwards()
        wanted = Forward(
            Direction.LOCAL, "127.0.0.1", _free_port(), "127.0.0.1", echo_port
        )

        first = await forwards.add(connection, wanted, idle=60)
        again = await forwards.add(connection, wanted, idle=60)

        assert first.error == "" and again.error == ""
        assert again.port == first.port
        assert len(forwards) == 1
        assert await _spoken_through(first.port) == b"HELLO"

        forwards.close()


async def test_a_url_forward_nobody_used_is_reaped():
    "The port goes back to the machine when the page is done with it."
    async with echoing() as echo_port, ssh_connection() as connection:
        forwards = Forwards()

        with _clock(forwards) as stands_at:
            opened = await forwards.add(
                connection,
                Forward(
                    Direction.LOCAL, "127.0.0.1", _free_port(), "127.0.0.1", echo_port
                ),
                idle=60,
            )
            assert opened.error == ""

            stands_at[0] += 61
            gone = forwards.reap()

        assert len(gone) == 1
        assert len(forwards) == 0, "A reaped forward is not wanted any more."
        with pytest.raises(OSError):
            await _spoken_through(opened.port)


async def test_using_a_url_forward_puts_off_the_reaping():
    """
    **The accept handler is the whole activity signal.** asyncssh calls
    it per incoming connection, and without it a page that is being used
    would lose its port on the clock.
    """
    async with echoing() as echo_port, ssh_connection() as connection:
        forwards = Forwards()

        with _clock(forwards) as stands_at:
            opened = await forwards.add(
                connection,
                Forward(
                    Direction.LOCAL, "127.0.0.1", _free_port(), "127.0.0.1", echo_port
                ),
                idle=60,
            )

            stands_at[0] += 30
            assert await _spoken_through(opened.port) == b"HELLO"

            stands_at[0] += 40  # 70 since it opened, 40 since it was used.
            assert forwards.reap() == []
            assert len(forwards) == 1

            stands_at[0] += 30
            assert len(forwards.reap()) == 1

        forwards.close()


async def test_a_forward_somebody_typed_is_never_reaped():
    "They asked for it, so nothing but them takes it away."
    async with echoing() as echo_port, ssh_connection() as connection:
        forwards = Forwards()

        with _clock(forwards) as stands_at:
            await forwards.add(
                connection,
                Forward(
                    Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port
                ),
            )

            stands_at[0] += 60 * 60 * 24
            assert forwards.reap() == []
            assert len(forwards) == 1

        forwards.close()


async def test_a_url_forward_that_went_idle_while_the_link_was_down_stays_gone():
    "There is nothing to come back for, so the reconnect does not bring it."
    async with echoing() as echo_port:
        forwards = Forwards()
        wanted = Forward(
            Direction.LOCAL, "127.0.0.1", _free_port(), "127.0.0.1", echo_port
        )

        with _clock(forwards) as stands_at:
            async with ssh_connection() as first:
                assert (await forwards.add(first, wanted, idle=60)).error == ""

            stands_at[0] += 61

            async with ssh_connection() as second:
                assert await forwards.reopen(second) == ""
                assert forwards.opened() == []


# ----------------------------------------------------------------------
# The client's own glue: the packet it is asked with, and the reconnect.


async def test_the_client_opens_what_the_server_asked_for():
    """
    `_forward_asked` end to end: the packet the server sends, the
    listener it produces, and the answer that goes back.

    **The answer is the whole table**, so the server's copy is right
    after a reconnect as well as after a change, and the bound port
    reaches the person who asked for any free one.
    """
    import tempfile

    sent = []
    where = Path(tempfile.mkdtemp())
    socket_path = str(where / "pymux.sock")

    async with echoing() as echo_port, ssh_connection() as connection:
        client = SshClient("ssh://127.0.0.1:1%s" % (socket_path,))
        client._send_packet = sent.append

        await client._forward_asked(
            connection,
            {
                "cmd": "forward",
                "direction": "local",
                "listen_host": "127.0.0.1",
                "listen_port": ANY_PORT,
                "dest_host": "127.0.0.1",
                "dest_port": echo_port,
            },
        )

        assert len(sent) == 1
        assert sent[0]["cmd"] == "forwards"
        listed = sent[0]["data"]
        assert len(listed) == 1
        assert listed[0]["error"] == ""

        port = listed[0]["port"]
        assert port != ANY_PORT
        # The person asked for any port, so the message names the one
        # they got. Without this they have nothing to connect to.
        assert str(port) in sent[0]["message"], sent[0]["message"]
        assert await _spoken_through(port) == b"HELLO"

        await client._forward_asked(
            connection,
            {
                "cmd": "forward",
                "remove": True,
                "direction": "local",
                "listen_host": "127.0.0.1",
                "listen_port": ANY_PORT,
            },
        )

        assert sent[1]["data"] == []
        assert "Stopped forwarding" in sent[1]["message"]


async def test_the_message_does_not_claim_an_address_it_cannot_know():
    """
    A remote bind off loopback opens, and what the client says about it
    has to stop short of the address.

    **The reply carries a port and no address**, so "Forwarding
    -R 0.0.0.0:2222" would be an assertion the client cannot support.
    `checks.pymux-openssh` shows the other half: a real openssh under
    its default binds loopback here and reports success.
    Lillecarl/pymux#444.
    """
    sent = []

    async with echoing() as echo_port, ssh_connection() as connection:
        client = SshClient("ssh://127.0.0.1/tmp/nowhere.sock")
        client._send_packet = sent.append

        await client._forward_asked(
            connection,
            {
                "cmd": "forward",
                "direction": "remote",
                "listen_host": "0.0.0.0",
                "listen_port": ANY_PORT,
                "dest_host": "127.0.0.1",
                "dest_port": echo_port,
            },
        )

        said = sent[0]["message"]
        assert sent[0]["data"][0]["error"] == "", said
        assert "loopback only" in said, said
        assert "0.0.0.0" in said, said


async def test_a_loopback_forward_is_reported_without_a_caveat():
    "The common case says what happened, with nothing hedged onto it."
    sent = []

    async with echoing() as echo_port, ssh_connection() as connection:
        client = SshClient("ssh://127.0.0.1/tmp/nowhere.sock")
        client._send_packet = sent.append

        await client._forward_asked(
            connection,
            {
                "cmd": "forward",
                "direction": "local",
                "listen_host": "127.0.0.1",
                "listen_port": ANY_PORT,
                "dest_host": "127.0.0.1",
                "dest_port": echo_port,
            },
        )

        assert "loopback only" not in sent[0]["message"], sent[0]["message"]


async def test_connecting_again_brings_the_forwards_back():
    """
    **The promise this feature makes**, through the client's own
    `_connect` and not through `Forwards.reopen` alone: a link that
    dropped gives the tunnels back with the panes.

    `_link_again` calls the same `_connect`, so what passes here is
    what a person gets after their laptop wakes up.
    """
    import tempfile

    where = Path(tempfile.mkdtemp())
    socket_path = str(where / "pymux.sock")

    async with echoing() as echo_port:
        async with live_servers(socket_path) as (_pymux, port, client_key):
            client = SshClient(
                "ssh://127.0.0.1:%d%s" % (port, socket_path),
                known_hosts=None,
                client_keys=[client_key],
                username="anybody",
            )

            connection, _reader = await client._connect()
            was = await client.forwards.add(
                connection,
                Forward(
                    Direction.LOCAL, "127.0.0.1", ANY_PORT, "127.0.0.1", echo_port
                ),
            )
            assert was.error == ""

            connection.close()
            with pytest.raises(OSError):
                await _spoken_through(was.port)

            # What `_link_again` does, and the only line under test.
            again, _reader = await client._connect()
            try:
                back = client.forwards.opened()[0]
                assert back.error == "", back.error
                assert await _spoken_through(back.port) == b"HELLO"
            finally:
                again.close()


# ----------------------------------------------------------------------
# The commands, and what they ask of a client.


async def _errors_of(session, command) -> list:
    "What a command complained about, as a person would read it."
    session.pymux.command_error = []
    try:
        session.pymux.handle_command(command)
        return list(session.pymux.command_error)
    finally:
        session.pymux.command_error = None


def _forward_requests(seen: list) -> list:
    """
    The forward requests among the packets a client received.

    The memory pipe carries what the socket carries, which is JSON
    ending at a zero byte, so this reads it the way a real client
    does rather than expecting objects.
    """
    asked = []

    for packet in seen:
        if isinstance(packet, (bytes, bytearray)):
            packet = packet.decode("utf-8")
        for one in packet.split("\0"):
            if not one.strip():
                continue
            read = json.loads(one)
            if read.get("cmd") == "forward":
                asked.append(read)

    return asked


async def _listed(session, command) -> list:
    session.pymux.command_output = []
    try:
        session.pymux.handle_command(command)
        return list(session.pymux.command_output)
    finally:
        session.pymux.command_output = None


async def test_a_client_on_a_socket_cannot_forward():
    """
    **The error a person meets first**, because most clients are local.
    A unix socket is already on the machine the server runs on, so
    there is nothing to tunnel and no connection to tunnel through.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = await _errors_of(session, "forward-port -L 8080:localhost:3000")

        assert any("SSH" in line for line in said), said


async def test_a_spelling_that_cannot_be_read_says_so():
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        said = await _errors_of(session, "forward-port -L nonsense")

        assert any("nonsense" in line for line in said), said


async def test_giving_neither_direction_says_so():
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = await _errors_of(session, "forward-port")

        assert any("-L" in line for line in said), said


async def test_the_request_reaches_the_client_that_asked():
    """
    The packet a forward really is: the server asks, and the machine
    the person sits at acts. It carries both ends resolved, so the
    client parses no spelling of its own.
    """
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        with as_the_person(session):
            session.pymux.handle_command("forward-port -L 8080:localhost:3000")

        asked = await once(
            lambda: _forward_requests(seen),
            2.0,
            "The client was never asked to forward anything.",
        )

    assert asked[0] == {
        "cmd": "forward",
        "direction": "local",
        "listen_host": "localhost",
        "listen_port": 8080,
        "dest_host": "localhost",
        "dest_port": 3000,
    }


async def test_removing_asks_by_the_listening_end_alone():
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        session.pymux.handle_command("unforward-port -R 9222")

        asked = await once(
            lambda: _forward_requests(seen),
            2.0,
            "The client was never asked to stop forwarding.",
        )

    assert asked[0] == {
        "cmd": "forward",
        "remove": True,
        "direction": "remote",
        "listen_host": "localhost",
        "listen_port": 9222,
    }


async def test_the_listing_reads_what_the_client_reported():
    """
    `list-forwards` draws the server's copy, which a client fills in
    when it reports. A forward that could not open is listed with the
    reason, because that is what a person asks when the port is dead.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        connection = session.pymux.clients[0].connection
        connection.can_forward = True
        connection.forwards = [
            {
                "direction": "local",
                "listen_host": "localhost",
                "port": 8080,
                "dest": "localhost:3000",
                "error": "",
            },
            {
                "direction": "remote",
                "listen_host": "localhost",
                "port": 9222,
                "dest": "localhost:9222",
                "error": "Address already in use",
            },
        ]

        said = "\n".join(await _listed(session, "list-forwards"))

    assert "-L localhost:8080 -> localhost:3000" in said, said
    assert "-R localhost:9222 -> localhost:9222 (Address already in use)" in said, said


# ----------------------------------------------------------------------
# The URL that brings its port with it. Lillecarl/pymux#437.

LOOPBACK_URL = "http://localhost:3000/"


def _open_requests(seen: list) -> list:
    "The open requests among the packets a client received."
    asked = []

    for packet in seen:
        if isinstance(packet, (bytes, bytearray)):
            packet = packet.decode("utf-8")
        for one in packet.split("\0"):
            if not one.strip():
                continue
            read = json.loads(one)
            if read.get("cmd") == "open":
                asked.append(read)

    return asked


async def _opened(session, seen, url=LOOPBACK_URL, can_forward=True) -> dict:
    "Ask the session to open a URL, and read the packet the client got."
    await session.attach("the client", SIZE)
    session.pymux.clients[0].connection.can_forward = can_forward

    session.pymux.handle_command("open-url %s" % (url,))

    asked = await once(
        lambda: _open_requests(seen),
        2.0,
        "The client was never asked to open anything.",
    )
    return asked[0]


async def test_a_loopback_url_carries_its_port():
    """
    **The whole feature in one packet.** `http://localhost:3000` on the
    browser of the machine at the keyboard means a service on that
    machine. The one the pane meant is on the other, so the port comes
    with the URL.
    """
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        packet = await _opened(session, seen)

    assert packet["data"] == LOOPBACK_URL
    assert packet["forward"] == {"host": "localhost", "port": 3000, "idle": 600}


async def test_an_ordinary_url_carries_nothing():
    "The machine at the keyboard reaches example.com by itself."
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        packet = await _opened(session, seen, url="https://example.com/")

    assert packet == {"cmd": "open", "data": "https://example.com/"}


async def test_a_client_that_cannot_forward_is_asked_for_nothing_extra():
    """
    A client on a unix socket is already on the machine the server runs
    on, so `localhost` means the same machine and there is nothing to
    tunnel.
    """
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        packet = await _opened(session, seen, can_forward=False)

    assert packet == {"cmd": "open", "data": LOOPBACK_URL}


async def test_the_option_can_turn_the_forwarding_off():
    "The URL still opens. It just will not work, which is what was asked for."
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        session.pymux.handle_command("set-option -g open-url-forward off")
        packet = await _opened(session, seen)

    assert "forward" not in packet


async def test_forwarding_turned_off_is_not_worked_around():
    "`forward-mode off` means no port is bound here. A URL is not a way past it."
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        session.pymux.handle_command("set-option -g forward-mode off")
        packet = await _opened(session, seen)

    assert packet == {"cmd": "open", "data": LOOPBACK_URL}


async def test_the_idle_time_is_the_option():
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        session.pymux.handle_command("set-option -g open-url-forward-idle 60")
        packet = await _opened(session, seen)

    assert packet["forward"]["idle"] == 60


async def test_the_question_names_the_port_it_would_bind():
    """
    **A yes allows two things**, and a question that named only the
    page would be collecting an answer to the smaller one.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True
        session.pymux.handle_command("set-option -g open-url-mode ask")

        session.pymux.handle_command("open-url %s" % (LOOPBACK_URL,))

        asked = _questions(session)
        assert len(asked) == 1, asked
        assert LOOPBACK_URL in asked[0] and "localhost:3000" in asked[0], asked


async def test_asking_about_forwards_asks_about_a_url_that_brings_one():
    """
    `forward-mode ask` is about binding a port here, and this binds one.
    So the question is asked although `open-url-mode` would not ask.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True
        session.pymux.handle_command("set-option -g forward-mode ask")

        session.pymux.handle_command("open-url %s" % (LOOPBACK_URL,))

        assert len(_questions(session)) == 1


async def test_asking_about_forwards_leaves_an_ordinary_url_alone():
    "Nothing is bound for it, so the forwarding mode has no say."
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        session.pymux.handle_command("set-option -g forward-mode ask")
        packet = await _opened(session, seen, url="https://example.com/")

        assert packet["data"] == "https://example.com/"
        assert _questions(session) == []


async def test_answering_yes_opens_it_with_the_forward():
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True
        session.pymux.handle_command("set-option -g forward-mode ask")

        session.pymux.handle_command("open-url %s" % (LOOPBACK_URL,))
        await once(lambda: _questions(session), 2.0, "Nobody was asked.")

        answer = session.pymux.clients[0].answer()
        assert answer is not None

        with as_the_person(session):
            session.pymux.handle_command(answer)

        asked = await once(
            lambda: _open_requests(seen),
            2.0,
            "A confirmed open never reached the client.",
        )

    assert asked[0]["forward"]["port"] == 3000


# ----------------------------------------------------------------------
# The trust gate. Lillecarl/pymux#440.


def _questions(session) -> list:
    return [text for text, _command in session.pymux.clients[0].confirmations]


@contextmanager
def as_the_person(session):
    """
    Run a command the way the command bar does: inside the application
    of the client that is typing.

    **This is what makes the request in person.** `ClientState._handle_command`
    runs with that client's application current, so `get_client_state`
    finds it and `forwarding_client` says a person asked. A test that
    calls `handle_command` bare looks like a program in a pane instead,
    which is a different answer from the gate. Lillecarl/pymux#440.
    """
    with set_app(session.pymux.clients[0].app):
        yield


async def test_a_person_forwarding_loopback_is_not_asked():
    """
    The common case, and the reason the default is `on`: somebody types
    `forward-port -L` at their own keyboard, for a port on their own
    machine. A question there is a nuisance and no safer.
    """
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        with as_the_person(session):
            session.pymux.handle_command("forward-port -L 8080:localhost:3000")

        await once(
            lambda: _forward_requests(seen),
            2.0,
            "A person asking for a loopback forward was not obeyed.",
        )
        assert _questions(session) == []


async def test_binding_off_loopback_is_asked_even_when_the_mode_is_on():
    """
    `-R 0.0.0.0:2222:localhost:22` publishes the far machine's ssh to
    whatever network this one is on. openssh makes the same split, and
    calls it `GatewayPorts`.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        with as_the_person(session):
            session.pymux.handle_command("forward-port -R 0.0.0.0:2222:localhost:22")

        asked = _questions(session)
        assert len(asked) == 1, asked
        # Both ends, because where it listens is the whole question.
        assert "0.0.0.0:2222" in asked[0] and "localhost:22" in asked[0], asked
        # And that the answer is not pymux's to give. Asking somebody to
        # confirm the dangerous bind without saying openssh may quietly
        # make it a safe one asks about something that may not happen.
        # Lillecarl/pymux#444.
        assert "loopback only" in asked[0], asked


async def test_a_pane_is_asked_although_a_person_would_not_be():
    """
    **The hole this closes.** Any command a pane runs reaches the
    server, so without this any program on the far machine could bind
    a port on the machine somebody is sitting at.

    A command over a connection of its own is what a pane's CLI sends.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        await session.command("forward-port -L 8080:localhost:3000")

        asked = await once(
            lambda: _questions(session),
            2.0,
            "A pane's forward was made without anybody being asked.",
        )
        assert len(asked) == 1, asked
        assert "8080" in asked[0]


async def test_answering_yes_forwards_it():
    "The question carries the command that a yes runs, and -c stops it asking again."
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        await session.command("forward-port -L 8080:localhost:3000")
        await once(
            lambda: _questions(session),
            2.0,
            "A pane's forward was never put as a question.",
        )

        answer = session.pymux.clients[0].answer()
        assert answer is not None

        # What a yes does: the same command again, with -c, from the
        # client that answered.
        with as_the_person(session):
            session.pymux.handle_command(answer)

        asked = await once(
            lambda: _forward_requests(seen),
            2.0,
            "A confirmed forward never reached the client.",
        )

    assert asked[0]["listen_port"] == 8080
    assert asked[0]["dest_port"] == 3000


async def test_a_pane_cannot_confirm_its_own_request():
    """
    **The gate would be one word wide without this.** `-c` is the
    answer to a question, and a question is answered at a keyboard. A
    pane that sends the flag itself answered nothing.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True

        await session.command("forward-port -c -L 8080:localhost:3000")

        asked = await once(
            lambda: _questions(session),
            2.0,
            "A pane confirmed its own forward.",
        )
        assert len(asked) == 1, asked


async def test_the_mode_can_ask_for_everything():
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True
        session.pymux.handle_command("set-option -g forward-mode ask")

        with as_the_person(session):
            session.pymux.handle_command("forward-port -L 8080:localhost:3000")

        assert len(_questions(session)) == 1


async def test_the_mode_can_refuse_everything():
    """
    `off` refuses before it asks anybody. A person who turned
    forwarding off does not want a question about it either.
    """
    async with over_connection() as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True
        session.pymux.handle_command("set-option -g forward-mode off")

        said = await _errors_of(session, "forward-port -L 8080:localhost:3000")

        assert any("off" in line for line in said), said
        assert _questions(session) == []


async def test_removing_a_forward_is_never_asked():
    "Taking one away only removes something, so the reason to confirm does not apply."
    seen = []

    async with over_connection(read_packet=seen.append) as session:
        await session.attach("the client", SIZE)
        session.pymux.clients[0].connection.can_forward = True
        session.pymux.handle_command("set-option -g forward-mode ask")

        session.pymux.handle_command("unforward-port -L 8080")

        await once(
            lambda: _forward_requests(seen),
            2.0,
            "Removing a forward was not obeyed.",
        )
        assert _questions(session) == []


async def test_the_listing_says_so_when_there_is_nothing():
    async with over_connection() as session:
        await session.attach("the client", SIZE)

        said = "\n".join(await _listed(session, "list-forwards"))

    assert "No client is forwarding" in said, said
