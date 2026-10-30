"""
What the panes and the clients are told about each other's terminal.

A pane learns the cell size, the keyboard and the colours of the
clients that look at it; a client learns the pointer shape and the
keyboard flags of the pane it looks at. Each `sync_` sends only when the
answer changed since it last sent. Lillecarl/pymux#775.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar

from libpymux.protocol import Field, Packet
from pyte.images import ASSUMED_CELL_HEIGHT, ASSUMED_CELL_WIDTH
from pyte.keep import Keep
from pyte.keys import KeyboardFlag

from .enums import ExtendedKeys
from .log import logger
from .notify.osc import NO_PROGRESS
from .style.colors import theme_color_base
from .style.style import DEFAULT_THEME
from .wayland.portal import color_scheme_of, theme_background

if TYPE_CHECKING:
    from .connection.client_state import ClientState
    from .connection.server import ServerConnection
    from .core.main import Pymux
    from .core.session import Session
    from .core.window_tree import Pane
    from .ids import SessionId

__all__ = ["Terminals", "pointer_shape_of"]


def pointer_shape_of(pane: Pane | None) -> str:
    """
    The shape of the pointer that the program in a pane asks for.

    An empty string means that it asked for none, and that the
    terminal of the user picks the shape itself.
    """
    if pane is None:
        return ""
    return pane.screen.pointer_shapes.shape


class Terminals:
    "What each pane and each client was last told; the server's registry says the rest."

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: Every field is rebuilt: the next sync tells everybody again.
    KEEP: ClassVar[dict[str, Keep]] = {
        "pymux": Keep.REBUILT,
        "_kitty_flags_sent": Keep.REBUILT,
        "_keyboard_state_sent": Keep.REBUILT,
        "_cell_size_sent": Keep.REBUILT,
        "_scheme_from": Keep.REBUILT,
    }

    def __init__(self, pymux: Pymux) -> None:
        self.pymux = pymux

        #: Kitty keyboard protocol flags last sent to the clients. (The
        #: flags of the focused pane; clients enable the protocol on
        #: their outer terminals accordingly.)
        self._kitty_flags_sent: int | None = None

        # What the panes were last told about the keyboards of the
        # clients: the flags, each session's `synthesize-key-events` and
        # `extended-keys`. (None: nothing was told yet.)
        self._keyboard_state_sent: tuple[int, tuple[tuple[SessionId, bool], ...], ExtendedKeys] | None = None

        # The cell size the panes were last told. (None: nothing yet.)
        self._cell_size_sent: tuple[int, int] | None = None

        # What the portal's scheme was last worked out from.
        self._scheme_from: tuple[object, ...] | None = None

    def sync_pointer_shape(self) -> None:
        """
        Give every client the shape of the pointer that the pane it
        looks at asks for.

        Two clients can look at two panes, so this is answered for each
        of them. A client that moves to a pane which asks for no shape
        is told so, or the pointer keeps the shape of a pane that the
        user left.
        """
        try:
            for connection, client_state in self.pymux._client_states.items():
                # A socket command runs under a temporary client with
                # no connection: no terminal to send to, and stopping
                # here would starve every client after it.
                # Lillecarl/pymux#493.
                if connection is None:
                    continue
                connection.set_pointer_shape(self._pointer_shape_for(connection, client_state))
        except Exception:
            logger.exception("Sending the shape of the pointer failed.")

    def sync_pointer_shape_of(self, connection: ServerConnection) -> None:
        "The same for one client, whose pointer moved."
        client_state = self.pymux._client_states.get(connection)
        if client_state is None:
            return
        try:
            # A move draws nothing, so a frame is asked for to carry the shape.
            if connection.set_pointer_shape(self._pointer_shape_for(connection, client_state)):
                client_state.app.invalidate()
        except Exception:
            logger.exception("Sending the shape of the pointer failed.")

    def _pointer_shape_for(self, connection: ServerConnection, client_state: ClientState) -> str:
        """
        A line's resize shape while the client drags it or the pointer is
        over it, else the shape the pane asks for. The mouse handler
        under the pointer says which line, so a line under a menu has
        none. Lillecarl/pymux#819.
        """
        drag = client_state.border_drag
        if drag is not None:
            return drag.seam.pointer_shape
        cell = connection.pointer_cell
        if cell is not None:
            column, row = cell
            handlers = client_state.app.renderer.mouse_handlers.mouse_handlers.get(row)
            shape = getattr(handlers.get(column) if handlers is not None else None, "pointer_shape", None)
            if shape:
                return shape
        return pointer_shape_of(self.pymux.focused_pane_of(client_state))

    def sync_progress(self) -> None:
        """
        Give every client the progress of the pane it looks at, as the
        pointer's shape above. Lillecarl/pymux#788.
        """
        try:
            for connection, client_state in self.pymux._client_states.items():
                if connection is None:
                    continue
                pane = self.pymux.focused_pane_of(client_state)
                connection.set_progress(pane.progress if pane is not None else NO_PROGRESS)
        except Exception:
            logger.exception("Sending the progress failed.")

    def keyboard_source_flags(self) -> int:
        """
        What the terminals of the clients can report of the keyboard
        protocol, in the flags of that protocol.

        A pane may be looked at by several clients at once, and a key
        can come from any of them. So only what every attached client
        can report counts. No client at all reports nothing.

        Only an attached client counts. A connection that runs one
        command and leaves has no terminal of the user behind it, and
        it never asked one what it does.
        """
        # A client that runs in the process that started pymux has no
        # connection, so nothing ever asked its terminal what it can
        # report. It counts as reporting nothing, which is what it
        # already did.
        #
        # **The temporary client of a socket command is not here.** It
        # never asked a terminal anything, so its report of nothing
        # would drag the mask of every real client down with it for as
        # long as the command runs. Lillecarl/pymux#420.
        masks = [
            0 if connection is None else connection.replies.kitty_source_flags
            for connection, client_state in self.pymux._client_states.items()
            if not client_state.temporary
        ]
        if not masks:
            return 0
        common = masks[0]
        for mask in masks[1:]:
            common &= mask
        return common

    def sync_keyboard_source_flags(self) -> None:
        """
        Tell every pane what the keyboards of the clients can report.

        A pane answers the query of its program with the flags that it
        really gets, and that answer needs this. (Only walks the panes
        when something changed.)
        """
        state = (
            self.keyboard_source_flags(),
            tuple((session.session_id, session.options.synthesize_key_events) for session in self.pymux.sessions),
            self.pymux.options.extended_keys,
        )
        if state == self._keyboard_state_sent:
            return
        self._keyboard_state_sent = state
        for pane in list(self.pymux.panes_by_id.values()):
            self.tell_pane_about_keyboard(pane)

    def cell_size(self, session: Session | None = None) -> tuple[int, int]:
        """
        How big one cell is, in pixels, for the panes of this server, or
        for those of `session`.

        **The cell of the client a person used last**, of `session` when
        one watches it. A pane serves several clients and they do not
        share a cell size, so one of them has to speak for the pane, and
        every other terminal stretches a picture drawn for that cell. The
        terminal in front of the person draws it pixel for pixel: a
        Wayland window drawn for a 7x16 cell was grainy and 1.4 times too
        wide in a kitty with a 10x18 one. Lillecarl/pymux#753. Clients
        nobody typed in tie, and the first to attach wins.

        A client that never answered "CSI 16 t" is passed over rather
        than counted at its default, and a connection that runs one
        command and leaves is not a terminal anybody is looking at.
        With nobody to ask, the panes keep the size `pyte` assumes.
        Lillecarl/pymux#369.
        """
        known = [
            client_state
            for connection, client_state in self.pymux._client_states.items()
            if connection is not None and not client_state.temporary and connection.graphics.cell_size_known
        ]
        watching = [client_state for client_state in known if client_state.session is session] or known
        if not watching:
            return (ASSUMED_CELL_WIDTH, ASSUMED_CELL_HEIGHT)
        graphics = max(watching, key=lambda client_state: client_state.last_used).connection.graphics
        return (graphics.cell_width, graphics.cell_height)

    def sync_cell_size(self) -> None:
        """
        Tell every pane how big a cell is. (Only walks the panes when
        the answer changed.)

        A client attaching, a client leaving and a terminal answering
        the query all reach this. The answer moves rarely: it takes the
        client that speaks for the panes going away, or the first one
        ever to answer.
        """
        size = self.cell_size()
        if size == self._cell_size_sent:
            return
        self._cell_size_sent = size
        for pane in list(self.pymux.panes_by_id.values()):
            self.tell_pane_about_cell_size(pane)

    def tell_pane_about_cell_size(self, pane: Pane) -> None:
        """
        Tell one pane how big a cell is. Never raises: this runs when a
        client attaches and when a pane starts, and neither may stop
        for it.
        """
        # A window pane's screen, or an older ptterm's, cannot be told.
        # It then keeps the size `pyte` assumes.
        screen: Any = pane.screen
        with suppress(AttributeError):
            screen.set_cell_size(*self.cell_size())

    def sync_keyboard(self) -> None:
        """
        Tell the panes and the clients everything about the keyboard.

        `set-option extended-keys` calls this, because that option
        moves both halves at once: what a pane may ask for, and what
        the terminal of a client is put into. Lillecarl/pymux#173.
        """
        self.sync_keyboard_source_flags()
        self.sync_kitty_flags()

    def tell_pane_about_keyboard(self, pane: Pane) -> None:
        """
        Tell one pane what the keyboards of the clients can report.

        Never raises: this runs when a client attaches and when a pane
        starts, and neither may stop for it.
        """
        try:
            screen: Any = pane.screen
        except Exception:
            return
        try:
            screen.keyboard_source_flags = self.keyboard_source_flags()
            screen.synthesize_key_events = self.pymux.options_of_pane(pane).synthesize_key_events
            screen.extended_keys_allowed = self.pymux.options.extended_keys is not ExtendedKeys.OFF
        except AttributeError:
            # A window pane's screen, or an older ptterm's, knows nothing
            # about the keyboard of the host. It then claims what a pane
            # asks for.
            pass

    def tell_pane_about_colours(self, pane: Pane) -> None:
        """
        Tell one pane what the colours around it are: the theme's,
        when the theme owns the whole screen, and the terminal of the
        latest client otherwise. Never raises: a pane that starts
        without either, or whose embedder knows no `set_color_base`,
        answers from the table `pyte` reports, as before.
        Lillecarl/pymux#283.
        """
        try:
            # The theme colours the whole screen, and the palette
            # a program asks for is part of that screen.
            #
            # **A pane has one palette and its clients may have two
            # themes.** So this takes the theme of the client a
            # person used last, which is the rule the line below
            # already follows for a terminal's own colours, and the
            # one that decides where a browser opens.
            # Lillecarl/pymux#223.
            painted = self.pymux.options_of_pane(pane).paint_screen
            base = theme_color_base(self.latest_theme()) if painted else self.latest_client_color_base()
            if base is None:
                return
            pane.screen.set_color_base(base)
        except Exception:
            # The pane keeps the base it had, and the log says what
            # was swallowed: a pane that answers from the wrong table
            # is a fault nobody sees otherwise.
            logger.exception("Telling a pane about the colours failed.")

    def sync_color_bases(self) -> None:
        """
        Tell every pane what the terminal of the latest client draws
        with.

        A pane answers the colour queries of its program with the
        theme of that terminal, so what a program learns is what the
        person in front of it sees. The latest is the client a person
        used last, by the stamp that `window-size latest` reads -- the
        same rule that decides where a browser opens.

        Nothing attached, or a client that answered nothing, leaves
        every pane on the table `pyte` reports. Lillecarl/pymux#283.
        """
        for pane in list(self.pymux.panes_by_id.values()):
            self.tell_pane_about_colours(pane)
        self.sync_color_scheme()

    def sync_color_scheme(self) -> None:
        """
        Tell the portal whether the screen of the client used last is
        light or dark, for the programs in window panes.
        Lillecarl/pymux#812.

        The background decides: the theme's under `paint-screen`, drawn
        swapped under `swap-light-and-dark-colors`, and the terminal's own
        otherwise, since that is what shows behind the panes. Nothing
        attached leaves the last answer standing. A client's every key
        reaches this, so the answer is worked out again only when what it
        is worked out from moved.
        """
        latest = self.pymux.latest_client()
        if latest is None:
            return
        painted = latest.session.options.paint_screen
        theme = latest.theme_in_use
        swapped = painted and latest.options.swap_light_and_dark_colors
        background = None
        if not painted:
            # The in-process connection never asked its terminal.
            with suppress(AttributeError):
                background = latest.default_colors.background
        source = (theme, painted, swapped, background)
        if source == self._scheme_from:
            return
        self._scheme_from = source
        self.pymux.portal.scheme_is(color_scheme_of(background or theme_background(theme), swapped))

    def latest_theme(self) -> str:
        """
        The theme of the client a person used last.

        What a pane answers a colour query with, when the theme owns
        the screen. A theme belongs to one client now, and a pane
        belongs to all of them, so this is the tie break; nothing
        attached falls back to the theme a new client starts on.
        Lillecarl/pymux#223.
        """
        latest = self.pymux.latest_client()
        return DEFAULT_THEME if latest is None else latest.theme_in_use

    def latest_client_color_base(self):
        """
        The colour base of the client a person used last, or `None`
        when nobody is attached, or when that client asked its terminal
        nothing.
        """
        latest = self.pymux.latest_client()
        if latest is None:
            return None
        try:
            return latest.default_colors.color_base()
        except AttributeError:
            # A connection that never asked its terminal -- the
            # in-process one -- has no colours to hand over.
            return None

    def keyboard_flags_for_client(self) -> int:
        """
        What the terminal of a client is asked to report.

        Two things ask. The pane in front of the person asks for what
        its program pushed, and that has to reach the terminal or the
        program does not get the keys it wants. **pymux asks as well.**
        A bare Escape is also the first byte of every escape sequence,
        so a terminal that does not disambiguate leaves the parser
        holding it until a timeout says no more is coming: half a
        second before the command line closes, and the same half second
        before any alt key does anything. A terminal that
        disambiguates sends "CSI 27 u", which starts nothing, so the
        key arrives on the press.

        The two are one set of flags, because one terminal sends the
        keys of both. A pane reads what it asked for all the same: the
        key data reaches it as the terminal wrote it, and
        `Screen.encode_key` writes it again in the encoding of that
        pane. Lillecarl/pymux#164.

        `set-option extended-keys off` asks for nothing at all, and
        the client then writes "CSI = 0 ; 1 u" to put the terminal
        back in the legacy encoding. Lillecarl/pymux#173.
        """
        if self.pymux.options.extended_keys is ExtendedKeys.OFF:
            return 0
        return KeyboardFlag.DISAMBIGUATE | self.pymux.get_focused_kitty_flags()

    def sync_kitty_flags(self) -> None:
        """
        Send the kitty keyboard protocol flags to all clients, so that
        they can enable the protocol on their outer terminals. (Only
        sends when the value changed.)
        """
        flags = self.keyboard_flags_for_client()
        if flags == self._kitty_flags_sent:
            return
        self._kitty_flags_sent = flags
        try:
            for connection in self.pymux.connections:
                connection._send_packet({Field.CMD: Packet.KITTY_KEYBOARD, Field.DATA: {"flags": flags}})
        except Exception:
            logger.exception("Sending kitty keyboard flags failed.")
