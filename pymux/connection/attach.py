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
from .client import ClientRole
from .client_io import FrameOutput, NoStdout, SocketStdout

if TYPE_CHECKING:
    from pymux.core.session import Session

    from .client_state import ClientState
    from .server import ServerConnection

__all__ = ["CANNOT_ATTACH", "create_app", "detach_the_others", "refuse_the_attach", "start_gui", "take_client_options"]


#: What a client is told when it may not attach here.
#:
#: `pymux integrated` holds the server and its one terminal in one
#: process. The socket it binds is for commands, and a client that
#: attached over it would own half a session that the other half can
#: end: `ctrl+b d` in the first terminal takes the process, and the
#: second terminal loses the session whatever the first person meant.
#: Lillecarl/pyterm#161.
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

    detach_other_clients = bool(packet[Field.DETACH_OTHERS])
    # `attach -x`: the others leave, and the terminals they were
    # in close. Lillecarl/pyterm#349.
    hang_up_others = bool(packet.get(Field.HANG_UP_OTHERS))
    forced = packet[Field.COLOR_DEPTH]
    term = packet[Field.TERM]

    # What the packet says of the client goes on it before its
    # client state exists: `create_app` writes to the terminal, and a
    # paced client's first bytes already carry a status report.
    client = ClientRole(connection)

    # The colour depth of the outer terminal. A flag of the
    # client forces one; otherwise the environment gives the
    # first answer and the probe can raise it later.
    client.colors.forced = ColorDepth(forced) if forced else None
    client.colors.term = term
    client.colors.colorterm = packet.get(Field.COLORTERM, "")

    # Window frames as raw pixels, which the client encodes for
    # its own terminal. Only in msgpack, which carries bytes.
    # Lillecarl/pyterm#636.
    if packet.get(Field.FRAMES) and connection.encode is encode_msgpack:
        client.graphics.windows.send_frame = client.send_frame
        # And the codecs it decodes, for `window-codec`.
        # Lillecarl/pyterm#641.
        client.graphics.windows.codecs = frozenset(packet.get(Field.CODECS) or ())
        # A client that says when it wrote a frame gets the next
        # one after that: the socket draining says only that the
        # kernel took it, and over SSH megabytes of frames wait
        # in the buffers past it, with every key's echo behind
        # them. Lillecarl/pyterm#643.
        if packet.get(Field.SHOWS):
            client.graphics.windows.frame_room = client.pacing.frames_all_shown

    client.hostname = packet.get(Field.HOSTNAME, "")
    # Whether `forward-port` can reach this client at all. Only
    # the SSH client says yes. Lillecarl/pyterm#438.
    client.can_forward = bool(packet.get(Field.FORWARDS))
    client.answers_forwards = bool(packet.get(Field.REPORTS))
    client.answers_socks = bool(packet.get(Field.SOCKS))
    client.starts_browsers = bool(packet.get(Field.BROWSERS))
    client.saves_files = bool(packet.get(Field.SAVES_FILES))
    client.sends_files = bool(packet.get(Field.SENDS_FILES))
    client.answers_transfers = bool(packet.get(Field.KITTY_TRANSFERS))
    client.keeps_options = bool(packet.get(Field.KEEPS_OPTIONS))
    connection.answers_ping = bool(packet.get(Field.PINGS))
    client.pacing.paced = bool(packet.get(Field.PACED))
    client.environment = packet.get(Field.ENVIRONMENT) or {}
    client.ttyname = packet.get(Field.TTYNAME, "")
    client.pid = packet.get(Field.PID) or 0
    client.client_id = packet.get(Field.CLIENT_ID) or ""

    create_app(client, term=term)

    # `pymux attach -r`. The flag has to be on the client state
    # before the first key arrives. `attach-session -r` is the
    # same two lines for a client that is already here.
    # Lillecarl/pyterm#469.
    if packet.get(Field.READ_ONLY):
        client.state.read_only = True
        client.state.ignore_size = True

    if detach_other_clients or hang_up_others:
        detach_the_others(client, hang_up=hang_up_others)

    # What this client's own configuration file said about it.
    # Applied before the first frame, so nothing draws with a
    # theme the person did not choose and then change.
    # Lillecarl/pyterm#225.
    take_client_options(client, packet.get(Field.CLIENT_OPTIONS) or [])

    # After the options: what the client had set while it ran
    # wins over what its file says. Lillecarl/pyterm#411.
    welcome_back(connection.pymux, client.state, client.client_id)
    client.tell_options()

    # The session this client landed on takes the names that
    # follow a client: a display, an agent, a session bus.
    # `attach_client_to` does it for every later move between
    # sessions; this is the first attach, which does not go
    # through it. Lillecarl/pyterm#273.
    take_environment_from(connection.pymux, client.state)

    # Sound, on this connection, for a client that plays it.
    # Lillecarl/pyterm#659.
    if packet.get(Field.AUDIO) and connection.encode is encode_msgpack and client.audio is None:
        connection.spawn(client.carry_audio())

    if connection.answers_ping:
        connection.spawn(connection.watch_liveness())


def detach_the_others(client: ClientRole, hang_up: bool = False) -> None:
    """
    What `attach -d` means: take this session from whoever else
    holds it. `attach -x` is the same set of clients, told to hang
    up as well as to leave. Lillecarl/pyterm#349.

    **The session, and not the server.** `Pymux.clients_on` holds
    that rule, for this and for `attach-session -d` alike.
    Lillecarl/pyterm#347, Lillecarl/pyterm#351.

    **This runs after `create_app`**, because until then this
    connection has no session to compare the others against.
    """
    others = client.pymux.clients_on(client.state.session, except_for=client.state)
    for other in others:
        other.client.connection.detach_and_close(hang_up=hang_up)


def take_client_options(client: ClientRole, announced: list[Any]) -> None:
    """
    Set on this client what its own configuration file named.

    **A bad line never fails the attach.** A person with a typo in
    their configuration gets their panes and a message saying what
    was wrong; refusing to attach would leave them with a terminal
    they cannot use and a file they may not be able to reach.
    Lillecarl/pyterm#225.
    """
    for one in announced:
        try:
            name, value = one
            option = client.pymux.option_tables[Scope.CLIENT][name]
        except KeyError, TypeError, ValueError:
            client.state.message = "Invalid client option: %s" % (one,)
            continue

        try:
            option.set_value(client.pymux, value, client.state)
        except SetOptionError as e:
            # The line as they wrote it, then the reason. A person
            # reading "Expecting one of" needs to see what they
            # typed to know which line to go and fix.
            client.state.message = "%s %s: %s" % (name, value, e.message)

    client.pymux.terminals.sync_color_bases()


async def refuse_the_attach(connection: ServerConnection) -> None:
    """
    Tell this client that it cannot have the user interface, and
    close the connection. Lillecarl/pyterm#161.

    **The write is awaited, and `send_packet` does not wait.** That
    one spawns the write into the scope of this connection, and
    closing cancels that scope: the reason the client came would
    reach it only if the cancel lost the race.
    """
    await connection.write_packet({Field.CMD: Packet.OUT, Field.DATA: CANNOT_ATTACH})
    # And a code to leave with. A client that attached reads this
    # too now, so a script hears the refusal rather than reading a
    # zero and calling it attached. Lillecarl/pyterm#334.
    await connection.write_packet({Field.CMD: Packet.EXIT, Field.CODE: 1})

    logger.info("A client asked to attach to a server that serves one terminal.")
    connection.detach_and_close()


def create_app(
    client: ClientRole,
    term: str = "xterm",
    start: bool = True,
    session: Session | None = None,
) -> ClientState:
    """
    Make this client's state and application, and make it its
    connection's role: on attach, or for a command that arrived over
    the socket.

    :param start: Start the application. (`False` for the temporary client
        that handles `run-command` packets. That one is removed right
        after the command handler ran.)
    :param session: The session this client is on. Without one it is
        the session a person looked at last.
    """
    connection = client.connection
    stdout = (
        SocketStdout(
            connection.send_packet,
            connection.pymux.counters,
            client.pacing.frame_went_out,
            client.pacing.report_after_frame,
        )
        if start
        else NoStdout()
    )
    output = FrameOutput(
        cast(TextIO, stdout),
        lambda: connection.size,
        term=term,
    )
    output.keeps_placeholders = lambda: client.graphics.forwards_placeholders
    output.pixel_mouse = lambda: client.replies.pixel_mouse
    output.narrows_text_emoji = lambda: client.replies.widens_text_emoji
    output.cluster_fit = client.replies.cluster_fit

    client_state = connection.pymux.add_client(
        # A proxy: what it lacks of an `Input` it forwards to its pipe.
        input=cast("Input", client.pipeinput),
        output=output,
        client=client,
        color_depth=client.colors.depth,
        # The temporary client of a command that arrived over a socket is
        # not a client a person uses: it must not win "used last",
        # and what a command shows must go to a real one.
        temporary=not start,
        session=session,
    )
    connection.client = client
    connection.role = client

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
                    # it. Lillecarl/pyterm#233.
                    handle_sigwinch=False,
                )
            except anyio.get_cancelled_exc_class():
                raise
            except Exception:
                logger.exception("Application crashed.")
            finally:
                connection.close_connection()

        connection.spawn(run())

    return client_state
