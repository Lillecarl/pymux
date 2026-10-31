"""
A client attaching: what its `start-gui` packet says of it and of its
terminal, the client state and application it gets, and the clients it
takes the session from. A connection that may not attach is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TextIO, cast

import anyio
from libpymux.protocol import Field, Message, Packet
from libpymux.wire import MSGPACK_ZSTD, choose, encode_json, encode_msgpack
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import Input
from prompt_toolkit.output import ColorDepth

from ..core.environment import take_environment_from
from ..core.upgrade import welcome_back
from ..log import logger
from ..options.options import Scope, SetOptionError
from .client_io import FrameOutput, NoStdout, SocketStdout

if TYPE_CHECKING:
    from pymux.core.session import Session

    from .server import ServerConnection

__all__ = ["CANNOT_ATTACH", "create_app", "detach_the_others", "refuse_the_attach", "start_gui", "take_client_options"]


#: What a client is told when it may not attach here.
#:
#: `pymux integrated` holds the server and its one terminal in one
#: process. The socket it binds is for commands, and a client that
#: attached over it would own half a session that the other half can
#: end: `ctrl+b d` in the first terminal takes the process, and the
#: second terminal loses the session whatever the first person meant.
#: Lillecarl/pymux#159.
CANNOT_ATTACH = (
    "This pymux runs in one terminal, and that terminal is taken.\r\n"
    "The socket is for commands: pymux -S <socket> <command>.\r\n"
)


def start_gui(connection: ServerConnection, packet: Message) -> None:
    if not connection.may_attach:
        connection.spawn(refuse_the_attach(connection))
        return

    # The client reads msgpack, perhaps inside zstd: say which
    # in JSON, and write it from the next packet on.
    # `libpymux.wire`.
    wire = choose(packet.get(Field.WIRE), connection.pipe_connection.can_compress)
    if wire is not None and connection.encode is encode_json:
        connection.send_packet(
            {Field.CMD: Packet.WIRE, Field.WIRE: wire},
            compress_after=wire == MSGPACK_ZSTD,
        )
        connection.encode = encode_msgpack

    # Window frames as raw pixels, which the client encodes for
    # its own terminal. Only in msgpack, which carries bytes.
    # Lillecarl/pymux#634.
    if packet.get(Field.FRAMES) and connection.encode is encode_msgpack:
        connection.graphics.windows.send_frame = connection.send_frame
        # And the codecs it decodes, for `window-codec`.
        # Lillecarl/pymux#639.
        connection.graphics.windows.codecs = frozenset(packet.get(Field.CODECS) or ())
        # A client that says when it wrote a frame gets the next
        # one after that: the socket draining says only that the
        # kernel took it, and over SSH megabytes of frames wait
        # in the buffers past it, with every key's echo behind
        # them. Lillecarl/pymux#641.
        if packet.get(Field.SHOWS):
            connection.graphics.windows.frame_room = connection.pacing.frames_all_shown

    detach_other_clients = bool(packet[Field.DETACH_OTHERS])
    # `attach -x`: the others leave, and the terminals they were
    # in close. Lillecarl/pymux#347.
    hang_up_others = bool(packet.get(Field.HANG_UP_OTHERS))
    forced = packet[Field.COLOR_DEPTH]
    term = packet[Field.TERM]

    # The colour depth of the outer terminal. A flag of the
    # client forces one; otherwise the environment gives the
    # first answer and the probe can raise it later.
    connection.colors.forced = ColorDepth(forced) if forced else None
    connection.colors.term = term
    connection.colors.colorterm = packet.get(Field.COLORTERM, "")
    connection.hostname = packet.get(Field.HOSTNAME, "")
    # Whether `forward-port` can reach this client at all. Only
    # the SSH client says yes. Lillecarl/pymux#436.
    connection.can_forward = bool(packet.get(Field.FORWARDS))
    connection.answers_forwards = bool(packet.get(Field.REPORTS))
    connection.starts_browsers = bool(packet.get(Field.BROWSERS))
    connection.saves_files = bool(packet.get(Field.SAVES_FILES))
    connection.sends_files = bool(packet.get(Field.SENDS_FILES))
    connection.answers_ping = bool(packet.get(Field.PINGS))
    connection.pacing.paced = bool(packet.get(Field.PACED))
    connection.environment = packet.get(Field.ENVIRONMENT) or {}
    connection.ttyname = packet.get(Field.TTYNAME, "")
    connection.pid = packet.get(Field.PID) or 0
    connection.client_id = packet.get(Field.CLIENT_ID) or ""

    create_app(connection, color_depth=connection.colors.depth, term=term)

    # `pymux attach -r`. The client state exists from
    # `create_app` on, and the flag has to be on it before the
    # first key arrives. `attach-session -r` is the same two
    # lines for a client that is already here.
    # Lillecarl/pymux#467.
    if packet.get(Field.READ_ONLY) and connection.client_state is not None:
        connection.client_state.read_only = True
        connection.client_state.ignore_size = True

    if detach_other_clients or hang_up_others:
        detach_the_others(connection, hang_up=hang_up_others)

    # What this client's own configuration file said about it.
    # Applied before the first frame, so nothing draws with a
    # theme the person did not choose and then change.
    # Lillecarl/pymux#223.
    take_client_options(connection, packet.get(Field.CLIENT_OPTIONS) or [])

    # After the options: what the client had set while it ran
    # wins over what its file says. Lillecarl/pymux#409.
    if connection.client_state is not None:
        welcome_back(connection.pymux, connection.client_state, connection.client_id)

    # The session this client landed on takes the names that
    # follow a client: a display, an agent, a session bus.
    # `attach_client_to` does it for every later move between
    # sessions; this is the first attach, which does not go
    # through it. Lillecarl/pymux#271.
    if connection.client_state is not None:
        take_environment_from(connection.pymux, connection.client_state)

    # Sound, on this connection, for a client that plays it.
    # Lillecarl/pymux#657.
    if packet.get(Field.AUDIO) and connection.encode is encode_msgpack and connection.audio is None:
        connection.spawn(connection.carry_audio())

    if connection.answers_ping:
        connection.spawn(connection.watch_the_client())


def detach_the_others(connection: ServerConnection, hang_up: bool = False) -> None:
    """
    What `attach -d` means: take this session from whoever else
    holds it. `attach -x` is the same set of clients, told to hang
    up as well as to leave. Lillecarl/pymux#347.

    **The session, and not the server.** `Pymux.clients_on` holds
    that rule, for this and for `attach-session -d` alike.
    Lillecarl/pymux#345, Lillecarl/pymux#349.

    **This runs after `create_app`**, because until then this
    connection has no session to compare the others against.
    """
    if connection.client_state is None:
        return

    others = connection.pymux.clients_on(connection.client_state.session, except_for=connection.client_state)
    for other in others:
        theirs = other.connection
        if theirs is not None:
            theirs.detach_and_close(hang_up=hang_up)


def take_client_options(connection: ServerConnection, announced: list[Any]) -> None:
    """
    Set on this client what its own configuration file named.

    **A bad line never fails the attach.** A person with a typo in
    their configuration gets their panes and a message saying what
    was wrong; refusing to attach would leave them with a terminal
    they cannot use and a file they may not be able to reach.
    Lillecarl/pymux#223.
    """
    if connection.client_state is None:
        return

    for one in announced:
        try:
            name, value = one
            option = connection.pymux.option_tables[Scope.CLIENT][name]
        except KeyError, TypeError, ValueError:
            connection.client_state.message = "Invalid client option: %s" % (one,)
            continue

        try:
            option.set_value(connection.pymux, value, connection.client_state)
        except SetOptionError as e:
            # The line as they wrote it, then the reason. A person
            # reading "Expecting one of" needs to see what they
            # typed to know which line to go and fix.
            connection.client_state.message = "%s %s: %s" % (name, value, e.message)

    connection.pymux.terminals.sync_color_bases()


async def refuse_the_attach(connection: ServerConnection) -> None:
    """
    Tell this client that it cannot have the user interface, and
    close the connection. Lillecarl/pymux#159.

    **The write is awaited, and `send_packet` does not wait.** That
    one spawns the write into the scope of this connection, and
    closing cancels that scope: the reason the client came would
    reach it only if the cancel lost the race.
    """
    await connection.write_packet({Field.CMD: Packet.OUT, Field.DATA: CANNOT_ATTACH})
    # And a code to leave with. A client that attached reads this
    # too now, so a script hears the refusal rather than reading a
    # zero and calling it attached. Lillecarl/pymux#332.
    await connection.write_packet({Field.CMD: Packet.EXIT, Field.CODE: 1})

    logger.info("A client asked to attach to a server that serves one terminal.")
    connection.detach_and_close()


def create_app(
    connection: ServerConnection,
    color_depth: ColorDepth = ColorDepth.DEPTH_8_BIT,
    term: str = "xterm",
    start: bool = True,
    session: Session | None = None,
) -> None:
    """
    Make this client's state and application: on attach, or for a
    command that arrived over the socket.

    :param start: Start the application. (`False` for the temporary client
        that handles `run-command` packets. That one is removed right
        after the command handler ran.)
    :param session: The session this client is on. Without one it is
        the session a person looked at last.
    """
    stdout = (
        SocketStdout(
            connection.send_packet,
            connection.pymux.counters,
            connection.pacing.frame_went_out,
            connection.pacing.report_after_frame,
        )
        if start
        else NoStdout()
    )
    output = FrameOutput(
        cast(TextIO, stdout),
        lambda: connection.size,
        term=term,
    )
    output.keeps_placeholders = lambda: connection.graphics.forwards_placeholders
    output.pixel_mouse = lambda: connection.replies.pixel_mouse
    output.narrows_text_emoji = lambda: connection.replies.widens_text_emoji
    output.cluster_fit = connection.replies.cluster_fit

    client_state = connection.pymux.add_client(
        # A proxy: what it lacks of an `Input` it forwards to its pipe.
        input=cast("Input", connection.pipeinput),
        output=output,
        connection=connection,
        color_depth=color_depth,
        # The temporary client of a command that arrived over a socket is
        # not a client a person uses: it must not win "used last",
        # and what a command shows must go to a real one.
        temporary=not start,
        session=session,
    )
    connection.client_state = client_state

    def frame_begins(_app: object) -> None:
        output.frame_begins()

    def frame_ends(_app: object) -> None:
        output.frame_ends()

    client_state.app.before_render += frame_begins
    client_state.app.after_render += frame_ends

    if start:

        async def run() -> None:
            try:
                # The first window, before the first frame: a client
                # that arrives finds a session with a window, the
                # way attaching always did. Under its application,
                # so the new pane is focused where it is looked at.
                with set_app(client_state.app):
                    await connection.pymux.startup()
                # A server has no terminal of its own to print a
                # traceback on, and nobody to press ENTER, which is
                # what prompt_toolkit does with an exception in the
                # event loop. Let asyncio log it instead.
                await client_state.app.run_async(
                    set_exception_handler=False,
                    # **A server's application has no terminal of
                    # its own to be resized.** Each client reports
                    # its size in a packet, and SIGWINCH is one
                    # process-wide handler that only one
                    # application can hold: two of them save and
                    # restore it in the wrong order, and the last
                    # restore puts back an application that has
                    # already gone, with its whole layout behind
                    # it. Lillecarl/pymux#231.
                    handle_sigwinch=False,
                )
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:
                logger.exception("Application crashed.")
            finally:
                connection.close_connection()

        connection.spawn(run())
