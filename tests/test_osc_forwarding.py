"""
Tests for the OSC sequences that pymux passes from a pane to the
terminal of a client.

A pane cannot serve the clipboard, a desktop notification or the shape
of the pointer. pymux writes those to the outer terminal. The payload
comes from a program in a pane, so it is checked first.
"""
import pytest

from pymux.main import Pymux
from pymux.osc import MAX_OSC_LENGTH, build_osc


def sequence(code, param):
    return "\x1b]%s;%s\x1b\\" % (code, param)


# ----------------------------------------------------------------------
# Building the sequence.


@pytest.mark.parametrize(
    "code,param",
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
def test_a_plain_payload_becomes_a_sequence(code, param):
    assert build_osc(code, param) == sequence(code, param)


def test_a_payload_with_an_escape_byte_is_dropped():
    "An escape ends the sequence early, and what follows runs as a command."
    assert build_osc("99", "i=1;done\x1b]0;owned\x07") is None
    assert build_osc("22", "pointer\x1b[2J") is None


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
def test_a_payload_with_a_control_character_is_dropped(param):
    assert build_osc("99", param) is None


def test_a_payload_with_text_of_a_user_survives():
    "Only the control characters go. Text of any language stays."
    assert build_osc("99", "i=1;Bygget är klart ✅") is not None


def test_a_payload_that_is_too_long_is_dropped():
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
def test_a_clipboard_payload_that_is_not_base64_is_dropped(param):
    assert build_osc("52", param) is None


def test_the_same_payload_is_fine_for_a_notification():
    "Only OSC 52 reaches the clipboard, so only it is read that strictly."
    assert build_osc("99", "c;not base64!") is not None


# ----------------------------------------------------------------------
# Which clients receive it.


class FakeConnection:
    "A client connection that collects what pymux writes to it."

    def __init__(self):
        self.written = []
        self._pointer_shape_sent = ""

    def forward_osc(self, data):
        self.written.append(data)

    def set_pointer_shape(self, shape):
        "The same rule as the real one: only a change goes out."
        if shape == self._pointer_shape_sent:
            return
        self._pointer_shape_sent = shape
        self.forward_osc("\x1b]22;%s\x1b\\" % shape)


class FakeClientState:
    app = None


class FakeScreen:
    def __init__(self, pointer_shape=""):
        self.pointer_shape = pointer_shape


class FakeProcess:
    def __init__(self, pointer_shape=""):
        self.screen = FakeScreen(pointer_shape)


class FakePane:
    "A pane is named by its id when a notification answer comes back."

    def __init__(self, pane_id=7, pointer_shape=""):
        self.pane_id = pane_id
        self.process = FakeProcess(pointer_shape)


def make_pymux(focused=(), pane=None):
    """
    A pymux with two client connections. `focused` names the ones that
    look at `pane`.
    """
    pymux = Pymux()
    connections = [FakeConnection(), FakeConnection()]
    states = [FakeClientState(), FakeClientState()]
    pymux._client_states = dict(zip(connections, states))
    pymux.focused_pane_of = lambda state: (
        pane if states.index(state) in focused else None
    )
    return pymux, connections


def test_a_clipboard_write_reaches_every_client():
    pymux, connections = make_pymux()
    pymux.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    for connection in connections:
        assert connection.written == [sequence("52", "c;aGVsbG8=")]


def test_a_notification_reaches_every_client():
    "A pane out of sight is exactly what a notification is for."
    pymux, connections = make_pymux()
    pymux.forward_osc(FakePane(), "99", "i=mine;done")
    # The identifier is replaced by one that names the pane, so that
    # the answer of the terminal finds its way back.
    for connection in connections:
        assert connection.written == [sequence("99", "i=1;done")]


def test_a_notification_without_an_identifier_is_not_touched():
    pymux, connections = make_pymux()
    pymux.forward_osc(FakePane(), "99", "d=0;half a message")
    assert connections[0].written == [sequence("99", "d=0;half a message")]


def test_the_answer_finds_the_pane_that_asked():
    pymux, _connections = make_pymux()
    pymux.forward_osc(FakePane(pane_id=7), "99", "i=mine:a=report;done")
    answer = pymux.notifications.incoming("i=1")
    assert answer == (7, "i=mine")


# ----------------------------------------------------------------------
# The shape of the pointer.
#
# It is not passed on as it arrives. It belongs to the pane, the screen
# of the pane holds it, and every client is given the shape of the pane
# that it looks at.


def test_the_shape_reaches_the_client_that_looks_at_the_pane():
    pane = FakePane(pointer_shape="pointer")
    pymux, connections = make_pymux(focused=[1], pane=pane)
    pymux.forward_osc(pane, "22", "pointer")
    assert connections[0].written == []
    assert connections[1].written == [sequence("22", "pointer")]


def test_the_shape_goes_away_when_the_focus_leaves_the_pane():
    pane = FakePane(pointer_shape="pointer")
    pymux, connections = make_pymux(focused=[0], pane=pane)
    pymux.sync_pointer_shape()
    assert connections[0].written == [sequence("22", "pointer")]

    # The client looks at another pane now, which asks for no shape.
    pymux.focused_pane_of = lambda state: None
    pymux.sync_pointer_shape()
    assert connections[0].written[-1] == sequence("22", "")


def test_the_same_shape_goes_out_once():
    pane = FakePane(pointer_shape="pointer")
    pymux, connections = make_pymux(focused=[0], pane=pane)
    pymux.sync_pointer_shape()
    pymux.sync_pointer_shape()
    assert connections[0].written == [sequence("22", "pointer")]


def test_a_client_that_never_saw_a_shape_is_not_told_to_reset_it():
    pymux, connections = make_pymux()
    pymux.sync_pointer_shape()
    assert connections[0].written == []


def test_a_pane_without_a_screen_asks_for_no_shape():
    "A pane that never started has no screen to read."
    pymux, _connections = make_pymux()
    assert pymux.pointer_shape_of(None) == ""

    pane = FakePane()
    pane.process = None
    assert pymux.pointer_shape_of(pane) == ""


def test_an_unsafe_payload_reaches_nobody():
    pymux, connections = make_pymux()
    pymux.forward_osc(FakePane(), "99", "i=1;done\x1b]0;owned\x07")
    for connection in connections:
        assert connection.written == []


def test_the_clipboard_option_stops_the_clipboard_only():
    pymux, connections = make_pymux()
    pymux.enable_clipboard = False

    pymux.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    assert connections[0].written == []

    pymux.forward_osc(FakePane(), "99", "i=1;done")
    assert connections[0].written == [sequence("99", "i=1;done")]


def test_the_option_is_on_by_default():
    assert Pymux().enable_clipboard is True


def test_a_broken_connection_does_not_stop_the_pane():
    "This runs on the read path of a pane. It may not raise."

    class BrokenConnection(FakeConnection):
        def forward_osc(self, data):
            raise OSError("the pipe is gone")

    pymux = Pymux()
    pymux._client_states = {BrokenConnection(): FakeClientState()}
    pymux.forward_osc(FakePane(), "99", "i=1;done")  # Does not raise.


def test_a_client_without_focus_information_gets_no_pointer_shape():
    "`_has_focus` needs a running application, and says False without one."
    pymux = Pymux()
    assert pymux._has_focus(FakeClientState(), FakePane()) is False


# ----------------------------------------------------------------------
# The paste buffer of the session.


def test_what_a_pane_copies_reaches_the_paste_buffer():
    pymux, _connections = make_pymux()
    pymux.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    assert pymux.clipboard.get_data().text == "hello"


def test_text_of_a_user_survives_the_way_back():
    pymux, _connections = make_pymux()
    pymux.forward_osc(FakePane(), "52", "c;QnlnZ2V0IMOkciBrbGFydA==")
    assert pymux.clipboard.get_data().text == "Bygget är klart"


def test_a_selection_that_is_cleared_keeps_the_buffer():
    "An empty payload clears a selection of the user, not the buffer."
    pymux, _connections = make_pymux()
    pymux.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    pymux.forward_osc(FakePane(), "52", "p;")
    assert pymux.clipboard.get_data().text == "hello"


def test_a_payload_that_is_not_base64_reaches_nothing():
    pymux, connections = make_pymux()
    pymux.forward_osc(FakePane(), "52", "c;not base64!")
    assert pymux.clipboard.get_data().text == ""
    assert connections[0].written == []


def test_the_clipboard_option_stops_the_paste_buffer_as_well():
    pymux, _connections = make_pymux()
    pymux.enable_clipboard = False
    pymux.forward_osc(FakePane(), "52", "c;aGVsbG8=")
    assert pymux.clipboard.get_data().text == ""


def test_the_buffer_is_one_for_the_whole_session():
    "Two clients share it, so a copy in one pastes in the other."
    pymux = Pymux()
    assert pymux.clipboard is not None
