"""
A client with no terminal, that records what pymux draws for a session.

`record-client` records a person's terminal: it needs the person, and it
records what their terminal could show. A recorder is a client of its own
on a pair of in-memory queues, so an agent records a demo with nobody
attached, at a size of its choosing, and the recording is the stream
after pymux's damage tracking: a typed key is the few bytes of its echo,
not a screen. Lillecarl/pyterm#1064.

It is a client in every way a frame is drawn, and nobody in every way a
person is: it never becomes the client used last, so no message, focus,
notification or terminal answer goes to it, and it never moves a pane a
person watches. Its terminal is fixed: xterm-256color, true colour, no
pacing, no pings, no environment and no detection to wait for.
"""

from __future__ import annotations

import contextvars
from typing import TYPE_CHECKING, Final

from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import ColorDepth

from pymux.connection.attach import create_app
from pymux.connection.client import ClientRole
from pymux.connection.recording import Recording
from pymux.connection.server import ServerConnection
from pymux.pipes.base import BrokenPipeError
from pymux.pipes.memory import connect_in_memory

if TYPE_CHECKING:
    from pymux.connection.client_state import ClientState
    from pymux.core.main import Pymux
    from pymux.core.session import Session
    from pymux.pipes.memory import MemoryConnection

__all__ = ["recorder_of", "start_recorder"]

TERM: Final = "xterm-256color"
#: A pause in a demo is an agent thinking between two calls, and a
#: player that showed it whole would show a person staring.
PAUSE_CAP: Final = 1.5


def recorder_of(pymux: Pymux, session: Session) -> ClientState | None:
    "The recorder on `session`, if one runs."
    return next(
        (state for state in pymux._client_states.values() if state.headless and state.session is session),
        None,
    )


def start_recorder(
    pymux: Pymux,
    session: Session,
    name: str,
    size: Size | None = None,
    pause_cap: float | None = None,
    full_screen: bool = False,
) -> ClientState:
    """
    Attach a recorder to `session` and start its recording.

    **Nobody watching: it holds the size**, `size` or else the plane the
    active window has now, so nothing moves when it arrives and nothing
    moves when a person attaches later. **A person watching: it takes
    that person's terminal size and stays out of the vote**, so it shows
    what they see and moves none of their panes.
    """
    server_end, client_end = connect_in_memory()
    connection = contextvars.copy_context().run(lambda: ServerConnection(pymux, server_end))
    pymux.connections.append(connection)
    if pymux.tasks is None:
        raise RuntimeError("a recorder starts inside `Pymux.running`")
    pymux.tasks.start_soon(_drain, client_end)

    window = session.arrangement.get_active_window()
    people: list[ClientState] = (
        [one for one in pymux.clients_watching(window) if not one.nobody_there] if window else []
    )
    if people:
        latest = max(people, key=lambda one: one.last_used)
        connection.size = size or latest.app.output.get_size()
    else:
        plane = pymux.planes.size(window) if window is not None else None
        connection.size = size or plane or Size(rows=24, columns=80)

    client = ClientRole(connection)
    client.colors.forced = ColorDepth.TRUE_COLOR
    client.colors.term = TERM
    client.colors.colorterm = "truecolor"
    state = create_app(client, term=TERM, session=session, headless=True)
    state.read_only = True
    state.ignore_size = True
    state.holds_size = not people
    state.options.full_screen = full_screen
    if not people and size is None and window is not None:
        # The plane is what the panes have; the status rows go on top.
        connection.size = Size(
            rows=connection.size.rows + state.layout_manager.status.rows(), columns=connection.size.columns
        )

    client.recording = Recording(
        name,
        connection.size.rows,
        connection.size.columns,
        TERM,
        pause_cap=PAUSE_CAP if pause_cap is None else pause_cap,
    )
    pymux.waking.redraw(state)
    pymux.invalidate()
    return state


async def _drain(client_end: MemoryConnection) -> None:
    "Take the frames off the queue, which has no bound: the recording kept them where they were sent."
    try:
        while True:
            await client_end.read()
    except BrokenPipeError:
        return
    finally:
        client_end.close()
