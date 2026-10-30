"""
What a client's terminal says back: every reply it sends, each read for
what it tells, and the facts they set. The probes that ask go out when
the client's detection starts.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar

from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from ptterm import Terminal
from pyte.keep import Keep
from pyte.osc import Osc

from ..clusters import CLUSTER_WIDTH_PROBE, PROBE_ROWS, PROBES, Drawn, table_width
from ..enums import ExtendedKeys, Woke
from ..keys.keys import FOCUS_IN, FOCUS_OUT
from ..log import logger
from ..notify.notifications import QUERY as NOTIFICATION_QUERY
from ..notify.notifications import QUERY_IDENTIFIER, can_close, is_click, read_identifier, split_payload
from ..style.colors import COLOR_QUERIES
from ..style.nearest import NEAREST, nearest_theme, wanted_from
from .client_io import PIXEL_MOUSE_ON
from .known_terminals import Identity, identify, wcwidth_name
from .terminal_answers import SURVEY, TerminalAnswers

if TYPE_CHECKING:
    from .server import ServerConnection

__all__ = ["TerminalReplies"]

#: The answer to "CSI ? 2026 $ p" (DECRQM): "CSI ? 2026 ; <state> $ y".
#: A state of 0 means the terminal does not know the mode. 1 and 2 are
#: set and reset, and 4 means it is on and cannot be turned off; all
#: three say the terminal understands it.
_SYNCHRONIZED_REPLY_RE = re.compile(r"^\x1b\[\?2026;(\d+)\$y$")

#: Whether the terminal reports the mouse in pixels (SGR pixel mode,
#: 1016): asked with DECRQM, answered like 2026. Lillecarl/pymux#648.
PIXEL_MOUSE_QUERY = "\x1b[?1016$p"
_PIXEL_MOUSE_REPLY_RE = re.compile(r"^\x1b\[\?1016;(\d+)\$y$")

#: Where a terminal draws a text emoji with U+FE0F after it. The probe
#: writes one at the start of row 2 and asks where the cursor is:
#: column 2 says one cell, column 3 says the terminal widened it, as
#: kitty alone of seven does. Row 2, because "CSI 1;2R" is shift+F3 as
#: well. The two cells are erased again, and the answer repaints.
#: Lillecarl/pymux#539.
EMOJI_WIDTH_PROBE = "\x1b7\x1b[2;1H✳️\x1b[6n\x1b[2;1H\x1b[2X\x1b8"
_EMOJI_WIDTH_REPLY_RE = re.compile(r"^\x1b\[2;(\d+)R$")

#: The answers to `CLUSTER_WIDTH_PROBE`, one per row. Lillecarl/pymux#801.
_CLUSTER_WIDTH_REPLY_RE = re.compile(r"^\x1b\[([%s]);(\d+)R$" % "".join(map(str, PROBES)))

#: The terminal switched between light and dark (DEC 2031).
_THEME_REPORT_RE = re.compile(r"^\x1b\[\?997;[12]n$")

#: The size and the pixels in one report (DEC 2048).
_INBAND_RESIZE_RE = re.compile(r"^\x1b\[48;(\d+);(\d+);(\d+);(\d+)t$")

#: The answer to `pacing.STATUS_REPORT`: 0 is ready, 3 a malfunction.
_STATUS_REPLY_RE = re.compile(r"^\x1b\[[03]n$")

#: An OSC reply of the outer terminal: the code, and everything between
#: the code and the terminator.
_OSC_REPLY_RE = re.compile(r"^(?:\x1b\]|\x9d)(\d+);(.*?)(?:\x1b\\|\x9c|\x07)$", re.DOTALL)


class TerminalReplies:
    """
    The replies of one connection's terminal, and what they told.
    """

    KEEP: ClassVar[dict[str, Keep]] = {
        "connection": Keep.REBUILT,
        # Asked again of the terminal the client reattaches from.
        "kitty_source_flags": Keep.REBUILT,
        "_kitty_supported": Keep.REBUILT,
        "notification_closes": Keep.REBUILT,
        "answered_notification_query": Keep.REBUILT,
        "detection_pending": Keep.DROPPED,
        # The client asks again when it reattaches to the new server.
        "terminal_said": Keep.DROPPED,
        # The next server asks the terminal again.
        "pixel_mouse": Keep.DROPPED,
        "sizes_in_band": Keep.DROPPED,  # the terminal reports again when the mode is set
        "widens_text_emoji": Keep.DROPPED,
        "clusters_drawn": Keep.DROPPED,
    }

    def __init__(self, connection: ServerConnection) -> None:
        self.connection = connection

        #: Whether this client's terminal reports the mouse in pixels.
        #: Lillecarl/pymux#648.
        self.pixel_mouse = False
        #: Whether this client's terminal reports its size in band, DEC
        #: 2048, pixels and all. Lillecarl/pymux#797.
        self.sizes_in_band = False
        #: Whether this client's terminal draws a text emoji with U+FE0F
        #: two columns wide, where a pane holds one. Lillecarl/pymux#539.
        self.widens_text_emoji = False
        #: How wide this client's terminal draws each kind of grapheme
        #: cluster, as it answered `CLUSTER_WIDTH_PROBE`.
        #: Lillecarl/pymux#801.
        self.clusters_drawn = Drawn()
        #: Every reply this client's terminal gave. Lillecarl/pymux#725.
        self.terminal_said = TerminalAnswers()
        #: Set from `start_detection` until the device attributes reply,
        #: which closes the detection. The client sends "kitty-detect"
        #: right after it queried its terminal, and the replies pass
        #: through this connection.
        self.detection_pending = False
        self._kitty_supported = False
        #: What the outer terminal can report of the keyboard protocol,
        #: in the flags of that protocol. The query of the detection
        #: asks for every flag, so the reply says which ones the
        #: terminal took. Zero means the legacy encoding only.
        self.kitty_source_flags = 0
        #: Whether the outer terminal can take a notification back off
        #: its screen (`close` in `p=`), which pymux does once one is
        #: seen. Lillecarl/pymux#799.
        self.notification_closes = False
        #: Whether the outer terminal answered the OSC 99 query.
        self.answered_notification_query = False

    def start_detection(self) -> None:
        """
        The client queried its terminal, and the server asks the rest:
        so a client of any age gets a pointer to the pixel
        (Lillecarl/pymux#648), the survey whose answers are only kept
        (Lillecarl/pymux#725), and what a notification's click can do,
        OSC 99 `p=?` (Lillecarl/pymux#779).
        """
        self.detection_pending = True
        self.connection.write_output_raw(
            PIXEL_MOUSE_QUERY
            + EMOJI_WIDTH_PROBE
            + (CLUSTER_WIDTH_PROBE if self.connection.size.rows >= PROBE_ROWS else "")
            + SURVEY
            + "\x1b]99;"
            + NOTIFICATION_QUERY
            + "\x1b\\"
        )
        self.connection.flush_output()

    def _apply_color_depth(self) -> None:
        """
        Render with the colour depth that the detection settled on.

        The first frames use the fallback from TERM and COLORTERM; the
        probe can raise it. A change needs a full repaint, because the
        colours already on the screen were written with the old depth.
        """
        if self.connection.client_state is None:
            return
        depth = self.connection.colors.depth
        if depth == self.connection.client_state.color_depth:
            return
        self.connection.client_state.color_depth = depth
        self.connection.request_repaint()

    def arrived(self, data: str) -> None:
        """
        A terminal reply arrived from the outer terminal. The replies of
        the keyboard flags query and of the graphics query say which
        protocols the terminal speaks. The device attributes reply comes
        last and closes the detection.

        An answer to a desktop notification is not part of that. It can
        arrive at any time, long after the detection, and it belongs to
        one pane.
        """
        # Not kept: one arrives at every switch between windows.
        if data in (FOCUS_IN, FOCUS_OUT):
            logger.debug("The terminal %s the focus.", "took" if data == FOCUS_IN else "lost")
            if self.connection.client_state is not None:
                self.connection.client_state.terminal_focused = data == FOCUS_IN
                self.connection.pymux.sync_pane_focus()
                if data == FOCUS_IN:
                    self.connection.pymux.client_was_used(self.connection.client_state)
                    self.connection.pymux.invalidate(Woke.CLIENT_FOCUSED)
            return
        # The terminal switched between light and dark. The report says
        # which, not the colours: those are asked again, and their
        # answers match the theme as at attach. Lillecarl/pymux#796.
        if _THEME_REPORT_RE.match(data):
            self.connection.write_output_raw(COLOR_QUERIES)
            self.connection.flush_output()
            return

        # Every reply is kept, whatever is done with it below.
        # Lillecarl/pymux#725.
        self.terminal_said.heard(data)

        # The terminal reports the mouse in pixels if asked: from here on
        # it is asked, with the rest of the mouse. Lillecarl/pymux#648.
        pixel_mouse = _PIXEL_MOUSE_REPLY_RE.match(data)
        if pixel_mouse is not None:
            # Only with a cell to divide by. xterm reports pixels and, by
            # default, keeps its cell size to itself; its answer to that
            # query came before this one, or never. Lillecarl/pymux#651.
            # DECRPM: 1 set, 2 reset, 3 always set; 4 is always reset,
            # which iTerm2 answers, and 0 a mode it does not know.
            if pixel_mouse.group(1) in ("1", "2", "3") and self.connection.graphics.cell_size_known:
                self._turn_on_pixel_mouse()
            return

        emoji_width = _EMOJI_WIDTH_REPLY_RE.match(data)
        if emoji_width is not None:
            widens = int(emoji_width.group(1)) > 2
            if widens != self.widens_text_emoji:
                self.widens_text_emoji = widens
                logger.info(
                    "The terminal of client %s draws a text emoji with U+FE0F wide.", self.connection.client_id or "?"
                )
                # The emoji already on its screen were written for the
                # other reading.
                self.connection.request_repaint()
            return

        cluster_width = _CLUSTER_WIDTH_REPLY_RE.match(data)
        if cluster_width is not None:
            fit = self.cluster_fit()
            row = int(cluster_width.group(1))
            self.clusters_drawn = self.clusters_drawn.answered(row, int(cluster_width.group(2)))
            self._compare_with_the_table(row)
            # The clusters already on its screen were fitted otherwise.
            if self.cluster_fit() != fit:
                self.connection.request_repaint()
            return

        # The terminal read a frame. Lillecarl/pymux#259.
        if _STATUS_REPLY_RE.match(data):
            self.connection.pacing.status_answered()
            return

        # The terminal processed a window pane's frame. Lillecarl/pymux#643.
        if self.connection.graphics.windows.frame_room is not None and self.connection.graphics.windows.is_frame_answer(
            data
        ):
            self.connection.pacing.frame_answered()
            return

        osc = _OSC_REPLY_RE.match(data)
        if osc is not None:
            if osc.group(1) == "99":
                self._route_notification(osc.group(2))
            else:
                # The colours the terminal draws with, defaults and
                # palette alike. It answers each on the code that
                # asked, so this reads them whenever they arrive: a
                # terminal that reports a theme change later says it
                # the same way. Anything learned is what the panes
                # answer their programs with, so they are told again.
                if self.connection.default_colors.handle_osc_reply(osc.group(1), osc.group(2)):
                    self._match_the_theme()
                    self.connection.pymux.terminals.sync_color_bases()
            return

        # The cell size, whenever it comes: asked again at each resize.
        # The client that attached before its terminal answered can
        # first speak for the panes now, and one whose cell changed
        # speaks again. Lillecarl/pymux#369, Lillecarl/pymux#774.
        new_cell = self.connection.graphics.take_cell_size(data)
        if new_cell is not None:
            if new_cell:
                self.connection.pymux.terminals.sync_cell_size()
            return

        # DEC 2048: the size and the pixels in one report, which the
        # terminal sends when the mode is set and at each resize. From
        # here on a resize needs no cell size query. Lillecarl/pymux#797.
        resize = _INBAND_RESIZE_RE.match(data)
        if resize is not None:
            rows, columns, height, width = map(int, resize.groups())
            self.sizes_in_band = True
            if rows and columns:
                self.connection.resized(Size(rows=rows, columns=columns))
                if self.connection.graphics.set_cell_size(width=width // columns, height=height // rows):
                    self.connection.pymux.terminals.sync_cell_size()
            return

        if not self.detection_pending:
            return

        if data.startswith("\x1b[?") and data.endswith("u"):
            # Reply of the "CSI ? u" query: the terminal supports the
            # keyboard protocol. The detection asks for every flag
            # first, so the number says which ones the terminal took,
            # and that is what it can report.
            self._kitty_supported = True
            try:
                self.kitty_source_flags = int(data[3:-1] or 0)
            except ValueError:
                self.kitty_source_flags = 0
            self.connection.pymux.terminals.sync_keyboard_source_flags()
            return

        # The answer to the synchronised output query. A terminal that
        # holds a frame back does not need the cursor hidden while the
        # frame is painted, and hiding it restarts the blink: at a frame
        # rate that is a cursor which never blinks.
        synchronized = _SYNCHRONIZED_REPLY_RE.match(data)
        if synchronized is not None:
            if self.connection.client_state is not None:
                self.connection.client_state.output.synchronized_output = synchronized.group(1) != "0"
            return

        # The graphics query reply and the device attributes say
        # something about the images. The reply of the colour probe says
        # how many colours the terminal takes.
        fit = self.cluster_fit()
        self.connection.graphics.handle_reply(data)
        self.connection.colors.handle_reply(data)
        # The clusters already on its screen were fitted otherwise.
        if self.cluster_fit() != fit:
            self.connection.request_repaint()

        # The secondary attributes end in `c` too, and are the survey's,
        # never the fence. Lillecarl/pymux#725.
        if not data.endswith("c") or data.startswith("\x1b[>"):
            return

        # Device attributes reply: the fence of the detection. What did
        # not answer by now is not supported.
        self.detection_pending = False
        self._apply_color_depth()
        self.connection.send_packet(
            {
                Field.CMD: Packet.KITTY_KEYBOARD,
                Field.DATA: {"supported": self.keyboard_is_supported()},
            }
        )
        # Enable the flags on this client's terminal as well. (Other
        # clients are kept in sync through `Terminals.sync_kitty_flags`,
        # which only sends on change, so a client that attaches after
        # the value settled would otherwise never hear it.)
        self.connection.send_packet(
            {
                Field.CMD: Packet.KITTY_KEYBOARD,
                Field.DATA: {"flags": self.connection.pymux.terminals.keyboard_flags_for_client()},
            }
        )

    def keyboard_is_supported(self) -> bool:
        """
        Whether this client's terminal may be put into the protocol.

        The detection decides it, and one option overrides the
        detection. `set-option extended-keys always` is for a terminal
        that speaks the protocol and does not answer the query that
        asks; there are some, and a person who knows theirs is one can
        say so. Lillecarl/pymux#173.
        """
        if self.connection.pymux.options.extended_keys is ExtendedKeys.ALWAYS:
            return True
        return self._kitty_supported

    @property
    def identity(self) -> Identity:
        "This client's terminal, as its answers name it. Lillecarl/pymux#827."
        return identify(self.connection.graphics.terminal_version, self.connection.colors.term)

    @property
    def notification_osc(self) -> Osc | None:
        """
        The notification OSC this client's terminal shows, or None when
        it is known to show none. The query answer goes first; a
        terminal with no OSC 99 is known by its name. Lillecarl/pymux#826.
        """
        if self.answered_notification_query:
            return Osc.NOTIFICATION
        return self.identity.notification_osc

    def _route_notification(self, param: str) -> None:
        """
        Give the answer to a desktop notification to the pane that
        asked for it, under the identifier that the pane chose.

        An answer that names no notification of a pane is dropped. It
        must not reach a pane as key presses.

        A click shows this client the pane, and reaches the program only
        when it asked for clicks itself. Lillecarl/pymux#779.
        """
        metadata, _semicolon, text = split_payload(param)
        if read_identifier(metadata) == QUERY_IDENTIFIER:
            self.notification_closes = can_close(text)
            self.answered_notification_query = True
            return

        routed = self.connection.pymux.notifications.incoming(param)
        if routed is None:
            return

        route, param = routed
        pane = self.connection.pymux.panes_by_id.get(route.pane_id)
        if pane is None:
            return
        if is_click(param):
            if route.focuses and self.connection.client_state is not None:
                self.connection.pymux.show_pane_on_click(self.connection.client_state, pane)
            if not route.reports:
                return
        # A window pane sends no notification to answer.
        if not isinstance(pane.terminal, Terminal):
            return

        try:
            pane.terminal.process.write_input("\x1b]99;%s\x1b\\" % param)
        except Exception:
            logger.exception("Giving a notification answer to a pane failed.")

    def cluster_fit(self) -> str | Drawn | None:
        """
        How this client's terminal draws a grapheme cluster: by what it
        answered the probe, or else by the name of its table in wcwidth.
        None where it draws each as a pane holds it.

        The answer goes first: WezTerm 20240203 draws a flag two
        columns wide, where wcwidth's table for it says one.
        """
        if self.clusters_drawn.heard():
            return self.clusters_drawn if self.clusters_drawn.differs() else None
        return wcwidth_name(self.connection.graphics.terminal_version, self.connection.colors.term)

    def _compare_with_the_table(self, row: int) -> None:
        # A table that disagrees with the terminal it is named for is
        # worth knowing about: wcwidth's, or a font of the terminal's.
        named = wcwidth_name(self.connection.graphics.terminal_version, self.connection.colors.term)
        if named is None:
            return
        kind, cluster = PROBES[row]
        drawn = getattr(self.clusters_drawn, kind)
        listed = table_width(cluster, named)
        if drawn != listed:
            logger.info(
                "The terminal of client %s draws a %s %d columns wide, where wcwidth's table for %s says %d.",
                self.connection.client_id or "?",
                kind,
                drawn,
                named,
                listed,
            )

    def _match_the_theme(self) -> None:
        """
        Choose again which known theme this terminal is nearest to.

        **After every reply, not once.** The answers arrive one at a
        time -- the background, then the foreground, then sixteen
        palette entries -- so a match made on the first of them is a
        match made on a third of what the terminal says. Each reply
        can move it, and the last one settles it.

        A client that named a theme is left alone. A terminal that
        reports a theme change later comes through here as well, and
        the match follows it. Lillecarl/pymux#346.
        """
        if self.connection.client_state is None or self.connection.client_state.options.theme != NEAREST:
            return

        found = nearest_theme(wanted_from(self.connection.default_colors))
        if found is None or found == self.connection.client_state.matched:
            return

        self.connection.client_state.matched = found
        self.connection.pymux.invalidate(Woke.THEME_WAS_CHOSEN)

    def pixel_cell(self) -> tuple[int, int, int] | None:
        "The cell size mouse reports are divided by, while they come in pixels, and their first pixel's number."
        if not self.pixel_mouse or not self.connection.graphics.cell_size_known:
            return None
        return self.connection.graphics.cell_width, self.connection.graphics.cell_height, self.identity.first_pixel

    def _turn_on_pixel_mouse(self) -> None:
        if self.pixel_mouse:
            return
        self.pixel_mouse = True
        logger.info("The terminal of client %s reports the mouse in pixels.", self.connection.client_id or "?")
        app = self.connection.client_state.app if self.connection.client_state is not None else None
        if app is not None and app.renderer._mouse_support_enabled:
            self.connection.write_output_raw(PIXEL_MOUSE_ON)
            self.connection.flush_output()
