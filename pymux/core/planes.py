"""
How big each window's plane is. A pane has one pty, so it has one size
however many clients look at it: `window-size` picks the client whose
terminal decides, a session nobody watches keeps the size it was given,
and the chrome any watcher draws comes off the plane for everybody.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from prompt_toolkit.data_structures import Size
from ptterm import Terminal

from ..enums import WindowSize, Woke
from ..layout.geometry import change_pane_size
from ..log import logger
from ..options.window_options import WindowOptions
from .session import DEFAULT_SIZE, Session
from .window_tree import Window

if TYPE_CHECKING:
    from ..connection.client_state import ClientState
    from ..ids import WindowId
    from ..mirror.link import PeerLink
    from .main import Pymux
    from .pane import Pane

__all__ = ["Planes", "remember_the_size_made_at"]


def _any_of_them_wants_chrome(watchers: Iterable[ClientState]) -> bool:
    """
    Whether any of those clients is not in full screen.

    **Nobody watching is yes**, which is the answer the plane was
    measured with before `full-screen` belonged to a client. It is not
    the obvious reading -- nothing draws a titlebar over a window
    nobody looks at, so the plan reserves a row that stays empty -- and
    changing it moves the size of every detached pane in the
    collection: measured, and `pymux-vterm`, `pymux-alacritty` and
    `pymux-pictures` all read a different screen. That is its own
    question, and it is Lillecarl/pymux#474.
    """
    return not watchers or any(not one.options.full_screen for one in watchers)


def remember_the_size_made_at(window: Window, pane: Pane) -> None:
    """
    Keep the size a new window's one pane was given as the window's own
    while nobody watches it. `size_with_no_client` gave the pane the
    whole plane, so the two are the same number, and the window keeps it
    whatever the clients do after. Lillecarl/pymux#817.
    """
    terminal = pane.terminal
    assert isinstance(terminal, Terminal), "`_create_pane` builds a terminal pane"
    window.unwatched_size = Size(rows=terminal.screen.lines, columns=terminal.screen.columns)


def _plane_of_clients(clients: Sequence[ClientState], rule: WindowSize) -> Size:
    """
    The plane those clients give a window under `window-size` `rule`,
    less the status rows. `manual` without a size is `smallest`.

    A client that attached with `-r` is left out of the size. **Unless
    they all are** -- then they decide it between them, because the
    alternative is a plane no terminal here can show. tmux does the
    same, and for the same reason (`resize.c:95-107`).
    Lillecarl/pymux#467.

    **Every client, and not the ones that size it**, gives status rows.
    A read-only client still draws its own status line, so the rows it
    needs come off the plane even while its columns do not. The tallest
    one decides: a phone whose window list wraps to three rows takes
    three off everybody's plane, and the others draw background there.
    Lillecarl/pymux#723.
    """
    sizing = [one for one in clients if not one.ignore_size] or list(clients)
    if rule is WindowSize.LATEST:
        size = max(sizing, key=lambda client: client.last_used).app.output.get_size()
    else:
        pick = max if rule is WindowSize.LARGEST else min
        sizes = [client.app.output.get_size() for client in sizing]
        size = Size(rows=pick(one.rows for one in sizes), columns=pick(one.columns for one in sizes))
    status_rows = max(one.layout_manager.status.rows() for one in clients)
    return Size(rows=size.rows - status_rows, columns=size.columns)


class Planes:
    "The size of each window's plane, for every client that watches it."

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux
        #: The room each server that mirrors windows of this one has for
        #: them, by window id. A link with none is not here, so a frame
        #: on a server nobody mirrors pays one test. Lillecarl/pymux#759.
        self.peer_rooms: dict[PeerLink, dict[WindowId, Size]] = {}

    def _anyone_is_full_screen(self) -> bool:
        """
        Whether any client of this server wants no chrome.

        **The cheap half of the plane's question.** Until somebody asks
        for full screen the answer below is the server's option, and no
        window has to be walked for it. `clients_watching` sets the
        application of every client and asks which window it is on, and
        a plan asks the question four times per pane: measured at +128
        bytecode instructions each, which `checks.pymux-frame` read as
        10% of a one-pane window's neighbour cost.
        Lillecarl/pymux#471.
        """
        return any(one.options.full_screen for one in self.pymux._client_states.values())

    def any_watcher_shows_pane_status(self, window: Window | None = None) -> bool:
        """
        Whether any client watching that window draws the titlebar of a
        pane.

        **This is the plane's question, and `show_pane_status` is
        not.** The plane is one size for every client watching a
        window, because a pane has one pty, so it keeps the rows the
        chrome of any watcher needs and a client that dropped its own
        draws background in them. The other way round -- a plane that
        grew when the last client with chrome turned it off -- resizes
        everybody's programs because somebody else changed their mind.
        `size` asks the same of the status line.

        **Nobody watching is yes**, which is what the plane was
        measured with before this question existed.
        `_any_of_them_wants_chrome` says why it is not the other
        answer. Lillecarl/pymux#471.
        """
        session = self.pymux.session_of_window(window) if window is not None else None
        wanted = (session or self.pymux.current_session).options.pane_border_status
        if not self._anyone_is_full_screen():
            return wanted
        return wanted and _any_of_them_wants_chrome(self.pymux.clients_watching(window))

    def size(self, window: Window | None = None) -> Size:
        """
        How big the plane of that window is, in cells.

        **A plane is shared and a view is not.** A pane has one pty, so
        it has one size however many clients look at it (decision 10 of
        `docs/layout-engine-plan.md`), and this is that one size. A
        client bigger than the plane draws background around it, and a
        client smaller than it moves its view over it.

        **The window's `window-size` option decides which client.**
        `smallest` is the default and what pymux always did; `largest`
        gives the plane to the biggest client watching, `latest` to
        whoever used one last, and `manual` to nobody. tmux has the
        same four, and leaves a client too small to see the whole
        window stuck at the top left of it; here that client moves its
        view instead.

        The status line comes off the bottom of a client's terminal,
        because it is not part of any window.
        `geometry.room_for_panes` takes the rows the chrome
        around the panes wants off what is left. **A manual size is
        already the window's own**, so nothing comes off it.

        **Nobody watching yet is the session's `default_size`**, and no
        status row comes off it. A window exists before a client
        attaches to it, and the program in a pane needs a size from the
        first byte it writes. `new-session -x -y` is what names that
        size. Lillecarl/pymux#459.

        **A session nobody named a size for takes the clients' size**,
        by the same `window-size` rule, once: when the window is made.
        An agent's window is then the size the person's would be, and a
        program in it is never resized behind its back.
        Lillecarl/pymux#817. **A window somebody watched keeps the size
        it had then**, so looking away resizes nothing either.
        """
        if window is None:
            window = self.pymux.arrangement.get_active_window()
        if window is None:
            return self.size_with_no_client(None)

        if window.options.window_size is WindowSize.MANUAL and window.manual_size is not None:
            return window.manual_size

        watchers = self.pymux.clients_watching(window)
        peers = self._peers_watching(window)

        if not watchers and not peers:
            return self.size_with_no_client(self.pymux.session_of_window(window), window)

        size = self._watched_size(window, watchers, peers)
        # Kept for when nobody watches: a program is not resized because
        # a person looked away.
        window.unwatched_size = size
        return size

    def _watched_size(self, window: Window, watchers: Sequence[ClientState], peers: list[tuple[Size, int]]) -> Size:
        "The plane the clients and the peers watching `window` give it, by its `window-size`."
        rule = window.options.window_size
        if not peers:
            return _plane_of_clients(watchers, rule)

        # (room, last used): the clients here as one plane, each peer as
        # the room it has, its own status rows already off it.
        viewers = list(peers)
        if watchers:
            viewers.append(
                (_plane_of_clients(watchers, rule), max(client.last_used for client in watchers)),
            )
        if rule is WindowSize.LATEST:
            return max(viewers, key=lambda viewer: viewer[1])[0]
        pick = max if rule is WindowSize.LARGEST else min
        return Size(
            rows=pick(room.rows for room, _ in viewers),
            columns=pick(room.columns for room, _ in viewers),
        )

    def _peers_watching(self, window: Window) -> list[tuple[Size, int]]:
        """
        The room each server that mirrors this window has for it, and
        when a person there last used a mirror here. A peer counts as a
        client does, and its room is what its own clients give the
        window. Lillecarl/pymux#759.
        """
        if not self.peer_rooms:
            return []
        return [
            (rooms[window.window_id], link.last_used)
            for link, rooms in self.peer_rooms.items()
            if window.window_id in rooms
        ]

    def size_with_no_client(self, session: Session | None, window: Window | None = None) -> Size:
        """
        How big a window of that session is while nobody is watching it.

        The size it had when somebody last watched it. Before anybody
        did, the size `new-session -x -y` named, when it named one, and
        otherwise what the clients give a window made now
        (`size_for_a_new_window`).

        **The watched size comes first.** `-x -y` names a size for
        before a person looks. After them, it would resize every
        program they leave, and resize it again when they come back.
        """
        if window is not None and window.unwatched_size is not None:
            return window.unwatched_size
        if session is not None and session.default_size is not None:
            return session.default_size
        return self.size_for_a_new_window(session)

    def size_for_a_new_window(self, session: Session | None) -> Size:
        """
        What the clients on the server give a window made now, by the
        `window-size` a new window of that session starts with
        (`set-window-option -g`). With no client at all, the same two
        numbers tmux answers.
        """
        clients = self.pymux.clients
        if not clients:
            return DEFAULT_SIZE
        defaults = session.arrangement.window_defaults if session is not None else {}
        rule = defaults.get("window_size")
        if not isinstance(rule, WindowSize):
            rule = WindowOptions().window_size
        return _plane_of_clients(clients, rule)

    def resize_pane_for_program(self, pane: Pane, lines: int | None, columns: int | None) -> None:
        """
        Give a pane the size that the program inside it asks for.

        Three sequences ask: DECSLPP, and the two forms of a window
        resize. xterm has one window and does as it is told. A pane
        does not: it sits in a layout, and making one pane taller makes
        another shorter. So a program only gets its way when the person
        says it may, with "set-option allow-program-resize on".

        The layout holds weights and not cells, and a weight of one is
        about one cell, so the change is what "resize-pane" would do.
        The other panes give up the room, and the size of the window
        holds. So a pane cannot ask for more than there is.

        Never raises: this runs on the read path of a pane, and one
        sequence may not stop it.
        """
        if not self.pymux.options_of_pane(pane).allow_program_resize:
            return

        try:
            window = self.pymux._window_holding(pane)
            if window is None:
                return

            process = pane.process
            if lines is not None and process.sy:
                change_pane_size(self.pymux, window, pane, down=lines - process.sy)
            if columns is not None and process.sx:
                change_pane_size(self.pymux, window, pane, right=columns - process.sx)

            self.pymux.invalidate(Woke.PANE_RESIZED)
        except Exception:
            logger.exception("Failed to resize a pane for the program in it.")
