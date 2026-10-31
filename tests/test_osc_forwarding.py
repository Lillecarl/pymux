"""
Tests for the OSC sequences that pymux passes from a pane to the
terminal of a client.

A pane cannot serve the clipboard, a desktop notification or the shape
of the pointer. pymux writes those to the outer terminal. The payload
comes from a program in a pane, so it is checked first.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pyte import escape
from pyte.osc import Osc, PointerShapes
from pyte.sequences.csi import csi
from pyte.sequences.strings import Terminator, osc

from pymux.core.main import Pymux
from pymux.enums import Clipboard, NotificationPopups
from pymux.notify.osc import MAX_OSC_LENGTH, build_osc
from pymux.terminals import pointer_shape_of


def sequence(code, param):
    return "\x1b]%s;%s\x1b\\" % (code, param)


# ----------------------------------------------------------------------
# Building the sequence.


@pytest.mark.parametrize(
    ("code", "param"),
    [
        ("52", "c;aGVsbG8="),
        ("52", "p;"),  # Clear the primary selection.
        ("52", "cp;aGVsbG8="),  # Two selections at once.
        ("52", "s0;YQ=="),
        ("99", "i=1:d=0:p=title;Build ready"),
        ("99", "i=1:p=body;More text"),
        ("22", "pointer"),
        ("22", ""),  # Pop the shape.
    ],
)
def test_plain_payload_becomes_sequence(code, param):
    assert build_osc(code, param) == sequence(code, param)


def test_payload_with_escape_byte_is_dropped():
    "An escape ends the sequence early, and what follows runs as a command."
    assert build_osc("99", "i=1;done" + osc("0", "owned", end=Terminator.BEL)) is None
    assert build_osc("22", "pointer" + csi(escape.ED, 2)) is None


@pytest.mark.parametrize(
    "param",
    [
        "a\x07b",  # The bell also ends an OSC.
        "a\x00b",
        "a\nb",
        "a\x7fb",
        "a\x9cb",  # The eight bit string terminator.
        "a\x90b",
    ],
)
def test_payload_with_control_character_is_dropped(param):
    assert build_osc("99", param) is None


def test_payload_with_text_of_user_survives():
    "Only the control characters go. Text of any language stays."
    assert build_osc("99", "i=1;Bygget är klart ✅") is not None


def test_payload_that_is_too_long_is_dropped():
    "A broken half of a base64 payload writes a broken clipboard."
    data = "a" * (MAX_OSC_LENGTH - 2)
    assert build_osc("52", "c;" + data) is not None
    assert build_osc("52", "c;" + data + "aa") is None


@pytest.mark.parametrize(
    "param",
    [
        "c;?",  # A query. ptterm drops it; this is the second check.
        "c;not base64!",
        "c;aGVsbG8=;extra",
        "x;aGVsbG8=",  # Not a selection.
        "aGVsbG8=",  # No selection at all.
        "",
    ],
)
def test_clipboard_payload_that_is_not_base64_is_dropped(param):
    assert build_osc("52", param) is None


def test_same_payload_is_fine_for_notification():
    "Only OSC 52 reaches the clipboard, so only it is read that strictly."
    assert build_osc("99", "c;not base64!") is not None


# ----------------------------------------------------------------------
# Which clients receive it.


class FakeConnection:
    "A client connection that collects what pymux writes to it."

    def __init__(self):
        self.written = []
        self._pointer_shape_sent = ""
        # A kitty: it shows OSC 99. Lillecarl/pymux#497.
        self.replies = SimpleNamespace(notification_osc=Osc.NOTIFICATION)
        self.pointer_cell = None

    def forward_osc(self, data):
        self.written.append(data)

    def set_pointer_shape(self, shape):
        "The same rule as the real one: only a change goes out."
        if shape == self._pointer_shape_sent:
            return
        self._pointer_shape_sent = shape
        self.forward_osc("\x1b]22;%s\x1b\\" % shape)


class FakeSession:
    "A session with no overlay, which is what `Focus.pane_of` reads."

    overlay_pane = None
    arrangement = None


class FakeClientState:
    app = None
    session = FakeSession()
    temporary = False
    border_drag = None
    # Out of sight, so a notification goes out. Lillecarl/pymux#799.
    terminal_focused = False
    chooser = None
    popups: tuple = ()
    options = SimpleNamespace(notification_popups=NotificationPopups.OFF)


class FakeScreen:
    """
    Only the part of a screen that a forward reads.

    The stack is the real `PointerShapes`, so a change to how a screen
    holds a shape reaches this fake instead of going around it.
    """

    def __init__(self, pointer_shape=""):
        self.pointer_shapes = PointerShapes()
        if pointer_shape:
            self.pointer_shapes.stack.append(pointer_shape)


class FakePane:
    "A pane is named by its id when a notification answer comes back."

    def __init__(self, pane_id=7, pointer_shape=""):
        self.pane_id = pane_id
        self.screen = FakeScreen(pointer_shape)


def make_pymux(focused=(), pane=None, clipboard=Clipboard.ON):
    """
    A pymux with two client connections. `focused` names the ones that
    look at `pane`.

    The clipboard is open to a pane here, which is not the default:
    most of these tests are about what a pane's OSC 52 does once it is
    allowed through, and `set-clipboard` is judged on its own below.
    """
    pymux = Pymux()
    pymux.options.set_clipboard = clipboard
    connections = [FakeConnection(), FakeConnection()]
    states = [FakeClientState(), FakeClientState()]
    pymux._client_states = dict(zip(connections, states))
    pymux.focus.pane_of = lambda state: pane if states.index(state) in focused else None
    return pymux, connections


def test_clipboard_write_reaches_every_client():
    pymux, connections = make_pymux()
    pymux.options.set_clipboard = Clipboard.ON
    pymux.outward.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    for connection in connections:
        assert connection.written == [sequence("52", "c;aGVsbG8=")]


def test_notification_reaches_every_client():
    "A pane out of sight is exactly what a notification is for."
    pymux, connections = make_pymux()
    pymux.outward.forward_osc(FakePane(), "99", "i=mine;done")
    # The identifier is replaced by one that names the pane, so that
    # the answer of the terminal finds its way back, and a click is
    # asked for.
    for connection in connections:
        assert connection.written == [sequence("99", "i=1:a=report;done")]


def test_notification_skips_client_with_no_connection():
    "The same temporary client as in `test_shape_skips_client_with_no_connection`."
    pymux, connections = make_pymux()
    pymux._client_states = {None: FakeClientState(), **pymux._client_states}
    pymux.outward.forward_osc(FakePane(), "99", "i=mine;done")
    for connection in connections:
        assert connection.written == [sequence("99", "i=1:a=report;done")]


def test_clipboard_write_skips_client_with_no_connection():
    "Copy mode fans out the same way `forward_osc` does."
    pymux, connections = make_pymux()
    pymux._client_states = {None: FakeClientState(), **pymux._client_states}
    pymux.outward.write_user_clipboard("hello")
    for connection in connections:
        assert connection.written == [sequence("52", "c;aGVsbG8=")]


def test_notification_without_identifier_gets_one():
    "So a click on it can be routed. Lillecarl/pymux#779."
    pymux, connections = make_pymux()
    pymux.outward.forward_osc(FakePane(), "99", "d=0;half a message")
    assert connections[0].written == [sequence("99", "i=1:d=0:a=report;half a message")]


#: What a program writes, as (code, parameter) chunks. OSC 99 comes in
#: two, so the second is a body: `d=0` says more follows.
FROM = {
    "osc 99": [("99", "i=mine:d=0:p=title;Build"), ("99", "i=mine:p=body;done")],
    "osc 777": [("777", "notify;Build;done")],
    "osc 9": [("9", "Build done")],
}


@pytest.mark.parametrize(
    ("source", "shows", "wanted"),
    [
        ("osc 99", Osc.SIMPLE_NOTIFICATION, [("777", "notify;Build;done")]),
        ("osc 99", Osc.MESSAGE, [("9", "Build: done")]),
        ("osc 777", Osc.SIMPLE_NOTIFICATION, [("777", "notify;Build;done")]),
        ("osc 777", Osc.MESSAGE, [("9", "Build: done")]),
        ("osc 9", Osc.SIMPLE_NOTIFICATION, [("777", "notify;Build done;")]),
        ("osc 9", Osc.MESSAGE, [("9", "Build done")]),
        ("osc 99", None, []),
        ("osc 777", None, []),
        ("osc 9", None, []),
    ],
)
def test_each_terminal_gets_a_notification_in_the_osc_it_shows(source, shows, wanted):
    "Every notification OSC in, every one out, per client. Lillecarl/pymux#497."
    pymux, (connection, _other) = make_pymux()
    connection.replies.notification_osc = shows
    for code, param in FROM[source]:
        pymux.outward.forward_osc(FakePane(), code, param)
    assert connection.written == [sequence(*one) for one in wanted]


def test_a_kitty_gets_osc_777_as_osc_99():
    pymux, (connection, _other) = make_pymux()
    pymux.outward.forward_osc(FakePane(), "777", "notify;Build;done")
    assert connection.written == [
        sequence("99", "i=1:a=report:u=1:e=1:d=0:p=title;QnVpbGQ="),
        sequence("99", "i=1:a=report:u=1:e=1:p=body;ZG9uZQ=="),
    ]


def test_answer_finds_pane_that_asked():
    pymux, _connections = make_pymux()
    pymux.outward.forward_osc(FakePane(pane_id=7), "99", "i=mine:a=report;done")
    route, param = pymux.notifications.incoming("i=1")
    assert (route.pane_id, route.reports, param) == (7, True, "i=mine")


# ----------------------------------------------------------------------
# The shape of the pointer.
#
# It is not passed on as it arrives. It belongs to the pane, the screen
# of the pane holds it, and every client is given the shape of the pane
# that it looks at.


def test_shape_reaches_client_that_looks_at_pane():
    pane = FakePane(pointer_shape="pointer")
    pymux, connections = make_pymux(focused=[1], pane=pane)
    pymux.outward.forward_osc(pane, "22", "pointer")
    assert connections[0].written == []
    assert connections[1].written == [sequence("22", "pointer")]


def test_shape_goes_away_when_focus_leaves_pane():
    pane = FakePane(pointer_shape="pointer")
    pymux, connections = make_pymux(focused=[0], pane=pane)
    pymux.terminals.sync_pointer_shape()
    assert connections[0].written == [sequence("22", "pointer")]

    # The client looks at another pane now, which asks for no shape.
    pymux.focus.pane_of = lambda state: None
    pymux.terminals.sync_pointer_shape()
    assert connections[0].written[-1] == sequence("22", "")


def test_same_shape_goes_out_once():
    pane = FakePane(pointer_shape="pointer")
    pymux, connections = make_pymux(focused=[0], pane=pane)
    pymux.terminals.sync_pointer_shape()
    pymux.terminals.sync_pointer_shape()
    assert connections[0].written == [sequence("22", "pointer")]


def test_client_that_never_saw_shape_is_not_told_to_reset_it():
    pymux, connections = make_pymux()
    pymux.terminals.sync_pointer_shape()
    assert connections[0].written == []


def test_shape_skips_client_with_no_connection():
    """
    A socket command runs under a temporary client with no
    connection. There is no terminal to send to, and stopping for it
    would starve every client after it -- the `None` entry stands
    first here, so the old code served nobody. Lillecarl/pymux#493.
    """
    pane = FakePane(pointer_shape="pointer")
    pymux, connections = make_pymux(focused=[0, 1], pane=pane)
    pymux._client_states = {None: FakeClientState(), **pymux._client_states}
    pymux.terminals.sync_pointer_shape()
    assert connections[0].written == [sequence("22", "pointer")]
    assert connections[1].written == [sequence("22", "pointer")]


def test_no_pane_asks_for_no_shape():
    """
    A client that looks at no pane asks for no shape, and neither does
    a pane whose program never named one.
    """
    pymux, _connections = make_pymux()
    assert pointer_shape_of(None) == ""
    assert pointer_shape_of(FakePane()) == ""


def test_unsafe_payload_reaches_nobody():
    pymux, connections = make_pymux()
    pymux.outward.forward_osc(FakePane(), "99", ("i=1;done" + osc("0", "owned", end=Terminator.BEL)))
    for connection in connections:
        assert connection.written == []


@pytest.mark.parametrize("mode", [Clipboard.OFF, Clipboard.EXTERNAL])
def test_clipboard_option_stops_clipboard_only(mode):
    pymux, connections = make_pymux()
    pymux.options.set_clipboard = mode

    pymux.outward.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    assert connections[0].written == []

    pymux.outward.forward_osc(FakePane(), "99", "i=1;done")
    assert connections[0].written == [sequence("99", "i=1:a=report;done")]


def test_a_pane_reaches_the_clipboard_when_the_option_says_on():
    pymux, connections = make_pymux()
    pymux.options.set_clipboard = Clipboard.ON
    pymux.outward.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    assert connections[0].written == [sequence("52", "c;aGVsbG8=")]


def test_a_pane_is_refused_the_clipboard_by_default():
    """
    tmux ships "external", where a copy the person makes goes out and
    a program in a pane does not: a program on the far side of an ssh
    connection would otherwise write what that person pastes next.
    Lillecarl/pymux#378.
    """
    assert Pymux().options.set_clipboard is Clipboard.EXTERNAL


def test_broken_connection_does_not_stop_pane():
    "This runs on the read path of a pane. It may not raise."

    class BrokenConnection(FakeConnection):
        def forward_osc(self, data):
            raise OSError("the pipe is gone")

    pymux = Pymux()
    pymux._client_states = {BrokenConnection(): FakeClientState()}
    pymux.outward.forward_osc(FakePane(), "99", "i=1;done")  # Does not raise.


def test_client_without_focus_information_gets_no_pointer_shape():
    "`Focus.pane_of` needs a running application, and says None without one."
    pymux = Pymux()
    assert pymux.focus.pane_of(FakeClientState()) is None


# ----------------------------------------------------------------------
# The paste buffer of the session.


def test_what_pane_copies_reaches_paste_buffer():
    pymux, _connections = make_pymux()
    pymux.outward.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    assert pymux.clipboard.get_data().text == "hello"


def test_text_of_user_survives_way_back():
    pymux, _connections = make_pymux()
    pymux.outward.forward_osc(FakePane(), "52", "c;QnlnZ2V0IMOkciBrbGFydA==")
    assert pymux.clipboard.get_data().text == "Bygget är klart"


def test_selection_that_is_cleared_keeps_buffer():
    "An empty payload clears a selection of the user, not the buffer."
    pymux, _connections = make_pymux()
    pymux.outward.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    pymux.outward.forward_osc(FakePane(), "52", "p;")
    assert pymux.clipboard.get_data().text == "hello"


def test_payload_that_is_not_base64_reaches_nothing():
    pymux, connections = make_pymux()
    pymux.outward.forward_osc(FakePane(), "52", "c;not base64!")
    assert pymux.clipboard.get_data().text == ""
    assert connections[0].written == []


@pytest.mark.parametrize("mode", [Clipboard.OFF, Clipboard.EXTERNAL])
def test_clipboard_option_stops_paste_buffer_as_well(mode):
    "A pane that may not reach the clipboard may not reach the buffer."
    pymux, _connections = make_pymux(clipboard=mode)
    pymux.outward.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    assert pymux.clipboard.get_data().text == ""


def test_buffer_is_one_for_whole_session():
    "Two clients share it, so a copy in one pastes in the other."
    pymux = Pymux()
    assert pymux.clipboard is not None
