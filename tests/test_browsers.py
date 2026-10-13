"""
A browser a program on the server drives, on the client's machine: the
server's half. The client is the test, which answers the `browser`
packet itself. Lillecarl/pymux#653.
"""

from __future__ import annotations

import anyio
from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from session import once, over_connection

from pymux.commands.launch_browser import _hold

SIZE = Size(rows=24, columns=80)


def finished(got: list) -> tuple[int, str, str] | None:
    "A command's exit code, output and errors, once it answered."
    exits = [packet[Field.CODE] for packet in got if packet.get(Field.CMD) == Packet.EXIT]
    if not exits:
        return None
    said = lambda kind: "".join(packet[Field.DATA] for packet in got if packet.get(Field.CMD) == kind)  # noqa: E731
    return exits[0], said(Packet.OUT), said(Packet.ERR)


def question_of(state) -> str | None:
    "The launch the client is asked about, by id."
    for _text, command in state.confirmations:
        if command.startswith("launch-browser -c "):
            return command.split()[-1]
    return None


async def asked(session, sent: list):
    "A client that starts browsers, and a launch that waits on its question."
    state, _size = await session.attach("one", SIZE)
    state.connection.starts_browsers = True
    got = await session.command("launch-browser -- --headless=new https://example.org")
    launch = await once(lambda: question_of(state), 5.0, "the client was never asked")
    return state, got, launch


async def test_a_yes_starts_the_browser_and_the_launch_says_where_it_listens():
    sent: list = []
    async with over_connection(read_packet=sent.append) as session:
        state, got, launch = await asked(session, sent)
        session.pymux.browsers.approve(launch)
        state.answer()
        packet = await once(
            lambda: next((one for one in sent if one.get(Field.CMD) == Packet.BROWSER), None), 5.0, "no browser packet"
        )
        assert packet[Field.BROWSER] == launch
        assert packet[Field.ARGUMENTS] == ["--headless=new", "https://example.org"]
        assert packet[Field.PROGRAM] == "" and packet[Field.PROFILE] == ""
        state.connection._process(
            {Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.PATH: "/devtools/browser/abc"}
        )
        code, out, _err = await once(lambda: finished(got), 5.0, "the launch never answered")
        assert (code, out) == (0, "%d /devtools/browser/abc %s cdp\n" % (packet[Field.PORT], launch))


async def test_a_no_refuses_the_launch_at_once():
    async with over_connection() as session:
        state, got, _launch = await asked(session, [])
        state.answer()
        code, _out, err = await once(lambda: finished(got), 5.0, "the launch never answered")
        assert code == 1 and "said no" in err


async def test_a_program_cannot_answer_the_question_itself():
    "Only a person's yes counts, as with `open-url -c`. Lillecarl/pymux#443."
    async with over_connection() as session:
        state, _got, launch = await asked(session, [])
        answered = await session.command("launch-browser -c %s" % (launch,))
        code, _out, err = await once(lambda: finished(answered), 5.0, "the answer never answered")
        assert code == 1 and "only a person" in err
        assert not session.pymux.browsers.launches[launch].approved


async def test_a_client_that_starts_no_browser_is_not_asked():
    async with over_connection() as session:
        state, _size = await session.attach("one", SIZE)
        got = await session.command("launch-browser")
        code, _out, err = await once(lambda: finished(got), 5.0, "the launch never answered")
        assert code == 1 and "cannot start a browser" in err
        assert not state.confirmations


async def test_the_clients_reason_reaches_the_program():
    async with over_connection() as session:
        state, got, launch = await asked(session, [])
        session.pymux.browsers.approve(launch)
        state.answer()
        await once(lambda: session.pymux.browsers.launches[launch].port, 5.0, "no port chosen")
        state.connection._process(
            {Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.ERROR: "no Chromium browser here"}
        )
        code, _out, err = await once(lambda: finished(got), 5.0, "the launch never answered")
        assert code == 1 and "no Chromium browser here" in err


async def test_a_hold_ends_with_the_browser_and_stops_it_when_it_goes_first():
    sent: list = []
    async with over_connection(read_packet=sent.append) as session:
        state, got, launch = await asked(session, sent)
        session.pymux.browsers.approve(launch)
        state.answer()
        await once(lambda: session.pymux.browsers.launches[launch].port, 5.0, "no port chosen")
        state.connection._process({Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: launch, Field.PATH: "/p"})
        await once(lambda: finished(got), 5.0, "the launch never answered")

        # The browser ends by itself: the hold answers.
        ended = session.pymux.browsers.launches[launch].ended
        held = await session.command("hold-browser %s" % (launch,))
        await once(lambda: ended.statistics().tasks_waiting, 5.0, "the hold never waited")
        state.connection._process({Field.CMD: Packet.BROWSER_ENDED, Field.BROWSER: launch})
        assert (await once(lambda: finished(held), 5.0, "the hold never answered"))[0] == 0

        # The hold goes first, the way a killed `pymux-browser` does:
        # the browser is stopped.
        got = await session.command("launch-browser")
        second = await once(lambda: question_of(state), 5.0, "the client was never asked")
        session.pymux.browsers.approve(second)
        state.answer()
        await once(lambda: session.pymux.browsers.launches[second].port, 5.0, "no port chosen")
        state.connection._process({Field.CMD: Packet.BROWSER_STARTED, Field.BROWSER: second, Field.PATH: "/p"})
        await once(lambda: finished(got), 5.0, "the launch never answered")
        with anyio.move_on_after(0.2):
            await _hold(session.pymux, second, session.pymux.browsers.launches[second].ended)
        stop = {Field.CMD: Packet.BROWSER_STOP, Field.BROWSER: second}
        await once(lambda: stop in sent, 5.0, "the client was never told to stop the browser")
        assert second not in session.pymux.browsers.launches
