"""
Opening a URL in the browser of a client.

The server opens none itself: it has no browser, and the browser of the
user runs on the machine of the client. What the server owns is who
receives the request and whether it asks first, and that is what these
tests judge. The client half is one call to `webbrowser`, judged on its
own at the bottom.
"""

import asyncio
import base64
import json
import sys
import webbrowser

import pytest

from a_session import once, over_a_connection
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from pymux.client.terminal import TerminalClient
from pymux.osc import open_url_of
from pymux.options import ALL_OPTIONS, SetOptionError
from test_command_mode import in_a_loop

URL = "https://example.com/auth"

#: A pane that is still there when the test looks at it. A program that
#: exits takes its pane, and then its window, with it.
A_PANE_THAT_STAYS = "%s -c 'import time; time.sleep(30)'" % (sys.executable,)

A_SIZE = Size(rows=24, columns=80)


def opens(packets):
    "The open packets among everything the server wrote."
    return [
        json.loads(packet) for packet in packets if json.loads(packet).get("cmd") == "open"
    ]


def a_pane(pymux, state):
    "A window with a pane in it, so a pane can ask for something."
    with set_app(state.app):
        pymux.create_window(A_PANE_THAT_STAYS)
    return pymux.arrangement.get_active_window().panes[0]


# ----------------------------------------------------------------------
# Who receives it.


@in_a_loop
async def test_the_command_opens_on_the_client():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)

        pymux.handle_command("open-url %s" % URL)

        # The write of a packet is a turn of the loop behind the
        # command that asked for it.
        await once(lambda: opens(packets), 5.0, "the command never opened anything")

        assert opens(packets) == [{"cmd": "open", "data": URL}]
        assert URL in state.message


@in_a_loop
async def test_last_targets_the_client_used_last():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        a, _ = await session.attach("a", A_SIZE)
        b, _ = await session.attach("b", A_SIZE)

        # Attaching counts as using, so b is the last one until a types.
        assert pymux.the_clients_to_open_on() == [b]
        session.typed(a, "x")
        await once(
            lambda: a.last_used > b.last_used, 5.0, "the typing never counted"
        )
        assert pymux.the_clients_to_open_on() == [a]

        pymux.handle_command("open-url %s" % URL)

        # One packet, for one client: `last` is not a broadcast.
        await once(lambda: opens(packets), 5.0, "the command never opened anything")
        assert len(opens(packets)) == 1


@in_a_loop
async def test_broadcast_reaches_every_client():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        await session.attach("a", A_SIZE)
        await session.attach("b", A_SIZE)
        pymux.open_url_target = "broadcast"

        pymux.handle_command("open-url %s" % URL)

        await once(
            lambda: len(opens(packets)) == 2, 5.0, "the broadcast never reached both"
        )
        assert opens(packets) == [{"cmd": "open", "data": URL}] * 2


# ----------------------------------------------------------------------
# Whether it asks first.


@in_a_loop
async def test_ask_asks_and_opens_nothing():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        pymux.open_url_mode = "ask"

        pymux.handle_command("open-url %s" % URL)

        assert opens(packets) == []
        assert URL in state.confirm_text
        assert state.confirm_command == "open-url -c %s" % URL


@in_a_loop
async def test_a_yes_opens():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        pymux.open_url_mode = "ask"
        pymux.handle_command("open-url %s" % URL)

        session.typed(state, "y")
        await once(lambda: opens(packets), 5.0, "the yes never opened anything")

        assert opens(packets) == [{"cmd": "open", "data": URL}]
        assert state.confirm_command is None


@in_a_loop
async def test_a_no_opens_nothing():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        pymux.open_url_mode = "ask"
        pymux.handle_command("open-url %s" % URL)

        session.typed(state, "n")
        await once(lambda: state.confirm_command is None, 5.0, "the no never cleared")

        assert opens(packets) == []


@in_a_loop
async def test_off_opens_nothing():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        pymux.open_url_mode = "off"

        pymux.handle_command("open-url %s" % URL)

        assert opens(packets) == []
        assert state.confirm_text is None


# ----------------------------------------------------------------------
# What a pane asks for.


@in_a_loop
async def test_an_openurl_of_a_pane_opens():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        pane = a_pane(pymux, state)

        payload = "OpenURL=:" + base64.b64encode(URL.encode()).decode()
        pymux.forward_osc(pane, "1337", payload)

        await once(lambda: opens(packets), 5.0, "the pane's request never arrived")
        assert opens(packets) == [{"cmd": "open", "data": URL}]


@in_a_loop
async def test_another_subcommand_of_1337_opens_nothing():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        pane = a_pane(pymux, state)

        pymux.forward_osc(pane, "1337", "File=name=t.png;inline=1:AAAA")
        await asyncio.sleep(0.3)

        assert opens(packets) == []


# ----------------------------------------------------------------------
# The words an option takes.


def test_an_open_url_option_takes_its_words_only():
    class _Holder:
        open_url_target = "last"
        open_url_mode = "open"

    holder = _Holder()
    assert ALL_OPTIONS["open-url-target"].get_all_values(holder) == ["broadcast", "last"]

    with pytest.raises(SetOptionError):
        ALL_OPTIONS["open-url-target"].set_value(holder, "nonsense")
    with pytest.raises(SetOptionError):
        ALL_OPTIONS["open-url-mode"].set_value(holder, "nonsense")

    ALL_OPTIONS["open-url-target"].set_value(holder, "broadcast")
    assert holder.open_url_target == "broadcast"


# ----------------------------------------------------------------------
# The client half.


def test_the_client_asks_its_platform_to_open(monkeypatch):
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)

    TerminalClient()._process(json.dumps({"cmd": "open", "data": URL}).encode("utf-8"))

    assert opened == [URL]
