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
import contextlib
import json
import os
import sys
import webbrowser

import pytest

from a_session import in_a_loop, once, over_a_connection, in_this_process
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from pymux.client.terminal import TerminalClient
from pymux.osc import open_url_of
from pymux.options import ALL_OPTIONS, SetOptionError

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


@contextlib.contextmanager
def an_environment(**values):
    """
    The environment with what is given set, and what is None gone.

    The async tests cannot take the `monkeypatch` fixture: the loop
    they run in is not pytest's, and `in_a_loop` passes no arguments
    through. So they say what the environment holds by hand.
    """
    saved = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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


@in_a_loop
async def test_a_command_from_a_pane_opens_in_the_browser_of_the_client():
    """
    `pymux open-url` typed in a pane reaches the server over a socket,
    and the server runs it under a fake CLI: a client state for the
    connection of the command, which closes the moment the command
    answers. It used to carry the newest "used last" stamp, so the
    open packet went to that connection and no browser opened.
    Lillecarl/pymux#261, found on a machine.
    """
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)

        got = await session.a_command("open-url %s" % URL)

        await once(
            lambda: opens(packets),
            5.0,
            "the command from a pane never opened anything",
        )
        assert opens(packets) == [{"cmd": "open", "data": URL}]

        # What the connection of the command received back: the answer
        # of the command, and nothing a browser was meant to read.
        await once(
            lambda: any(json.loads(packet).get("cmd") == "exit" for packet in got),
            5.0,
            "the command never answered",
        )
        assert opens(got) == []


@in_a_loop
async def test_the_fake_cli_of_a_command_is_not_a_client_anybody_used():
    """
    The client state of a command that arrived over a socket is never
    stamped, never a target, and gone when the command is done.
    """
    with in_this_process() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)

        temp = session.a_command("open-url %s" % URL)

        assert temp.temporary is True
        assert temp.last_used == 0
        assert temp not in pymux._client_states.values()

        # The message of the command went to the client a person is
        # at, not to the connection of the command.
        assert URL in (state.message or "")
        assert URL not in (temp.message or "")


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


@in_a_loop
async def test_ask_asks_on_every_client_of_a_broadcast():
    with over_a_connection() as session:
        pymux = session.pymux
        a, _ = await session.attach("a", A_SIZE)
        b, _ = await session.attach("b", A_SIZE)
        pymux.open_url_mode = "ask"
        pymux.open_url_target = "broadcast"

        pymux.handle_command("open-url %s" % URL)

        for state in (a, b):
            assert URL in state.confirm_text
            assert state.confirm_command == "open-url -c %s" % URL


@in_a_loop
async def test_a_confirmed_command_opens_without_asking():
    packets = []
    with over_a_connection(read_a_packet=packets.append) as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        pymux.open_url_mode = "ask"

        pymux.handle_command("open-url -c %s" % URL)
        await once(lambda: opens(packets), 5.0, "the command never opened anything")

        assert opens(packets) == [{"cmd": "open", "data": URL}]
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
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    TerminalClient()._process(json.dumps({"cmd": "open", "data": URL}).encode("utf-8"))

    assert opened == [URL]


def test_the_client_reports_a_browser_it_could_not_open(monkeypatch):
    sent = []
    client = TerminalClient()
    monkeypatch.setattr(client, "_send_packet", sent.append)
    monkeypatch.setattr(webbrowser, "open", lambda url: False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    client._process(json.dumps({"cmd": "open", "data": URL}).encode("utf-8"))

    assert sent == [{"cmd": "open-failed", "data": URL}]


def test_the_client_reports_a_browser_that_raised(monkeypatch):
    sent = []
    client = TerminalClient()
    monkeypatch.setattr(client, "_send_packet", sent.append)

    def broken(url):
        raise webbrowser.Error("could not locate a browser")

    monkeypatch.setattr(webbrowser, "open", broken)
    monkeypatch.setenv("DISPLAY", ":0")

    client._process(json.dumps({"cmd": "open", "data": URL}).encode("utf-8"))

    assert sent == [{"cmd": "open-failed", "data": URL}]


def test_the_client_reports_nothing_when_a_browser_opened(monkeypatch):
    sent = []
    client = TerminalClient()
    monkeypatch.setattr(client, "_send_packet", sent.append)
    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    monkeypatch.setenv("DISPLAY", ":0")

    client._process(json.dumps({"cmd": "open", "data": URL}).encode("utf-8"))

    assert sent == []


def test_a_machine_without_a_display_tries_no_browser(monkeypatch):
    asked = []
    sent = []
    client = TerminalClient()
    monkeypatch.setattr(client, "_send_packet", sent.append)
    monkeypatch.setattr(webbrowser, "open", asked.append)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    client._process(json.dumps({"cmd": "open", "data": URL}).encode("utf-8"))

    assert asked == []  # A text browser in $BROWSER cannot run either.
    assert sent == [{"cmd": "open-failed", "data": URL}]


# ----------------------------------------------------------------------
# The shim.


@in_a_loop
async def test_the_shim_names_the_opener_of_the_session():
    with in_this_process() as session:
        pymux = session.pymux
        pymux.open_url_shim = True
        pymux._ensure_the_open_url_shim()

        directory = pymux._open_url_shim_dir
        script = os.path.join(directory, "pymux-open-url")
        assert os.access(script, os.X_OK)
        assert os.path.islink(os.path.join(directory, "xdg-open"))
        with open(script) as f:
            assert f.read() == '#!/bin/sh\nexec pymux open-url -- "$@"\n'

        # A pane that starts again asks for nothing new.
        pymux._ensure_the_open_url_shim()
        assert pymux._open_url_shim_dir == directory


@in_a_loop
async def test_the_shim_rides_the_path_of_a_new_pane():
    with in_this_process() as session:
        pymux = session.pymux
        pymux.open_url_shim = True
        pymux._ensure_the_open_url_shim()

        with an_environment(PATH="/usr/bin", BROWSER=None):
            pymux._shim_the_environment_of_a_pane()

            assert os.environ["PATH"].startswith(pymux._open_url_shim_dir + os.pathsep)
            assert os.environ["BROWSER"] == os.path.join(
                pymux._open_url_shim_dir, "pymux-open-url"
            )


@in_a_loop
async def test_a_pane_that_starts_with_the_shim_finds_the_opener():
    "The whole hook, from the option through the fork to the program."
    with in_this_process() as session:
        pymux = session.pymux
        pymux.open_url_shim = True
        state, _ = await session.attach("only", A_SIZE)

        # The pane of this route starts narrow, and long output wraps
        # over rows and gets cut before a client's size reaches it. So
        # the pane prints one short line that is the whole verdict: is
        # $BROWSER the opener of the shim directory, and is that
        # directory the first thing on PATH?
        program = (
            "%s -c 'import os, time; p = os.environ[\"PATH\"].split(\":\")[0];"
            " b = os.environ.get(\"BROWSER\");"
            " print(\"M=\" + str(b == p + \"/pymux-open-url\")); time.sleep(30)'"
        ) % (sys.executable,)
        with set_app(state.app):
            pymux.create_window(program)
        pane = pymux.arrangement.get_active_window().panes[0]

        page_text = lambda: pane.screen.page.text(0, 23)
        await once(
            lambda: "M=" in page_text(),
            5.0,
            "the pane never printed its environment",
        )
        assert "M=True" in page_text()


@in_a_loop
async def test_the_shim_leaves_a_pane_alone_when_it_is_off():
    with in_this_process() as session:
        pymux = session.pymux

        with an_environment(PATH="/usr/bin", BROWSER=None):
            pymux._shim_the_environment_of_a_pane()

            assert os.environ["PATH"] == "/usr/bin"
            assert "BROWSER" not in os.environ


# ----------------------------------------------------------------------
# What a server says when a client reports back.


@in_a_loop
async def test_a_client_that_could_not_open_says_so_in_its_status_line():
    with over_a_connection() as session:
        pymux = session.pymux
        state, _ = await session.attach("only", A_SIZE)
        connection = state.connection

        connection._process(json.dumps({"cmd": "open-failed", "data": URL}))

        assert state.message == "Could not open %s in a browser on this machine." % URL
