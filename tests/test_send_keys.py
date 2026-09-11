"""
`send-keys` types into a pane, and `-R` puts the pane back.

`-R` is the command a person runs when a program has left the pane in
a state they cannot type out of, so it is the one command that has to
work when nothing else does. It raised `AttributeError` and reset
nothing: it called `reset` on prompt_toolkit's `Screen`, which has no
such method, so the call after it never ran. Nothing tested it.
Lillecarl/pymux#118.
"""

import sys

import pytest

from pymux.commands.commands import call_command_handler
from pymux.main import Pymux
from pyte import escape
from pyte.modes import PrivateMode
from pyte.sequences import Csi, csi, set_mode

COLUMNS = 20
LINES = 5


@pytest.fixture
def pymux():
    "A server with one window, whose program ends at once."
    mux = Pymux()
    mux.create_window("%s -c pass" % (sys.executable,))
    try:
        yield mux
    finally:
        for window in list(mux.arrangement.windows):
            for pane in list(window.panes):
                process = getattr(pane, "process", None)
                if process is not None and not process.is_terminated:
                    process.kill()


def create_pane(mux, data: str = ""):
    "The active pane, sized, with `data` drawn on it."
    pane = mux.arrangement.get_active_window().active_pane
    pane.screen.resize(LINES, COLUMNS)
    if data:
        pane.terminal.terminal_control.stream.feed(data)
    return pane


def send(mux, *arguments):
    "Run `send-keys`, and give back what reached the program."
    pane = mux.arrangement.get_active_window().active_pane
    written = []
    pane.process.write_input = written.append
    errors = []
    mux.add_command_error = errors.append
    mux.show_message = lambda message: None

    call_command_handler("send-keys", mux, list(arguments))
    return "".join(written), errors


def test_a_reset_clears_what_the_program_drew(pymux):
    pane = create_pane(pymux, "hello\r\nworld")
    assert pane.screen.page.data_buffer

    send(pymux, "-R")

    assert not pane.screen.page.data_buffer


def test_a_reset_puts_the_cursor_home(pymux):
    pane = create_pane(pymux, "hello\r\nworld")

    send(pymux, "-R")

    assert (pane.screen.pt_cursor_position.y, pane.screen.pt_cursor_position.x) == (
        0,
        0,
    )


def test_a_reset_takes_back_a_mode_a_program_set(pymux):
    "DECSCNM turns the whole pane over, and a person cannot type it off."
    pane = create_pane(pymux, set_mode(PrivateMode.REVERSE_VIDEO))
    assert pane.screen.has_reverse_video

    send(pymux, "-R")

    assert not pane.screen.has_reverse_video


def test_a_reset_leaves_the_alternate_screen(pymux):
    "A program that died inside vim leaves the pane on the other screen."
    pane = create_pane(pymux, set_mode(PrivateMode.ALTERNATE_SCREEN_WITH_CURSOR))
    assert pane.screen.in_alternate_screen

    send(pymux, "-R")

    assert not pane.screen.in_alternate_screen


def test_a_reset_says_nothing_to_the_program(pymux):
    "It puts the terminal back. The program hears nothing of it."
    create_pane(pymux, "hello")
    assert send(pymux, "-R") == ("", [])


def test_the_keys_reach_the_program(pymux):
    create_pane(pymux)
    written, errors = send(pymux, "Enter")
    assert errors == []
    assert written == "\r"


#: What a keyboard sends for each key that `send-keys` can name.
#:
#: **These are the bytes a person's terminal really sends.** A key press
#: never goes through the table that `send-keys` reads: ptterm hands the
#: pane what the terminal sent, and the screen re-spells it for the
#: modes the pane turned on. So the two roads to a pane have to end in
#: the same bytes, and for 52 keys they did not. Lillecarl/pymux#119.
THE_KEYS = [
    ("Up", csi(escape.CUU)),
    ("Down", csi(escape.CUD)),
    ("Left", csi(escape.CUB)),
    ("Right", csi(escape.CUF)),
    # Home and End go out in the letter form, which is what xterm
    # sends and what their modified forms already used: ctrl+Home is
    # "\x1b[1;5H". The tilde form was the first of four that
    # `ANSI_SEQUENCES` lists, and the inversion took the first, so
    # plain Home and ctrl+Home disagreed about which key they were.
    ("Home", "\x1b[H"),
    ("End", "\x1b[F"),
    # And the backspace key sends a delete, which is what a fresh pty
    # reports as its erase character. DECBKM is what asks for "\x08".
    ("BSpace", "\x7f"),
    ("BTab", csi(Csi.CBT)),
    ("DC", "\x1b[3~"),
    ("IC", "\x1b[2~"),
    ("PageUp", "\x1b[5~"),
    ("PageDown", "\x1b[6~"),
    # The whole function row. F1 to F4 have an SS3 form and F5 upwards
    # do not, which is what a terminal sends and what the pane expects.
    #
    # **F5 went out as "\x1b[[E", which only the Linux console makes.**
    # `ANSI_SEQUENCES` lists that form first for F5, and the inversion
    # takes the first. F1 to F4 have the SS3 form ahead of it and F6
    # upwards have no such form, so F5 was the one key of the twelve
    # that was wrong, and only a walk over all of them says so.
    ("F1", "\x1bOP"),
    ("F2", "\x1bOQ"),
    ("F3", "\x1bOR"),
    ("F4", "\x1bOS"),
    ("F5", "\x1b[15~"),
    ("F6", "\x1b[17~"),
    ("F7", "\x1b[18~"),
    ("F8", "\x1b[19~"),
    ("F9", "\x1b[20~"),
    ("F10", "\x1b[21~"),
    ("F11", "\x1b[23~"),
    ("F12", "\x1b[24~"),
    ("C-Up", csi(escape.CUU, 1, 5)),
    ("C-Left", csi(escape.CUB, 1, 5)),
    ("S-Up", csi(escape.CUU, 1, 2)),
    ("Tab", "\t"),
    ("Escape", "\x1b"),
    ("C-c", "\x03"),
    # The carriage return and the line feed, which are two keys.
    ("C-m", "\r"),
    ("C-j", "\n"),
    ("Enter", "\r"),
]


@pytest.mark.parametrize("name, expected", THE_KEYS)
def test_a_key_sends_what_a_keyboard_sends(pymux, name, expected):
    create_pane(pymux)
    written, errors = send(pymux, name)
    assert errors == []
    assert written == expected


#: The keys a pane in the legacy encoding has no form for.
#:
#: **Each of these used to be sent as a different key, in silence.**
#: ctrl+shift+a went as ctrl+a, super+a went as "a", and a person had
#: no way to find out. Lillecarl/pymux#168 wrote the first trade down
#: as intended; Carl asked for the opposite on Lillecarl/pymux#237, and
#: he is right. If you meant ctrl+a, write ctrl+a.
THE_KEYS_A_LEGACY_PANE_CANNOT_READ = [
    ("C-S-a", "shift"),
    ("c-s-z", "shift"),
    ("Super-a", "super"),
    ("SUPER-A", "super"),
    ("Super-S-a", "super"),
    ("Super-up", "super"),
    ("Super-enter", "super"),
    ("Hyper-escape", "hyper"),
    ("ctrl+shift+a", "shift"),
    ("super+a", "super"),
    # Ctrl has a legacy form and super does not, so this one is
    # refused for the super alone.
    ("Super-C-a", "super"),
]


@pytest.mark.parametrize("name, lost", THE_KEYS_A_LEGACY_PANE_CANNOT_READ)
def test_a_key_the_pane_cannot_read_is_refused(pymux, name, lost):
    create_pane(pymux)

    written, errors = send(pymux, name)

    assert written == ""
    assert len(errors) == 1
    assert lost in errors[0]
    assert name in errors[0]


@pytest.mark.parametrize(
    "name, expected",
    [
        ("C-S-a", "\x1b[97;6u"),
        ("super+a", "\x1b[97;9u"),
        ("Hyper-escape", "\x1b[27;17u"),
        ("ctrl+shift+a", "\x1b[97;6u"),
    ],
)
def test_the_same_keys_reach_a_pane_that_asked_for_them(pymux, name, expected):
    """
    Nothing about the key changed. The pane did, and that is the whole
    point: what can be sent belongs to the pane.
    """
    create_pane(pymux, "\x1b[>1u")

    assert send(pymux, name) == (expected, [])


def test_the_modifiers_a_legacy_pane_can_read_still_go(pymux):
    """
    ctrl is a control code and alt is an escape in front of the key.
    Both are ambiguous there, and ambiguous is not the same as absent.
    """
    create_pane(pymux)

    assert send(pymux, "C-a") == ("\x01", [])
    assert send(pymux, "M-a") == ("\x1ba", [])
    assert send(pymux, "M-C-a") == ("\x1b\x01", [])
    assert send(pymux, "shift+a") == ("A", [])


def test_an_arrow_is_the_application_form_for_a_pane_that_asked(pymux):
    """
    "\\x1bOA" is the application cursor form, and DECCKM turns it on.

    The pane above asks for it, so it gets it. A pane that asks for
    nothing gets "\\x1b[A", which is the row for "Up" in the table
    above. `send-keys Up` used to send the application form to both,
    because the table held it as the plain answer.
    """
    create_pane(pymux, set_mode(PrivateMode.APPLICATION_CURSOR_KEYS))
    assert send(pymux, "Up")[0] == "\x1bOA"


#: The keys a laptop keyboard leaves out, in both spellings.
#:
#: **`send-keys C-Home` used to type the six letters into the pane.**
#: The table `send-keys` read names `C-Left`, `C-Right`, `C-Up` and
#: `C-Down` and no other modified functional key, so ctrl worked on
#: four keys and on none of the other twenty-two. And an argument that
#: names no key is sent as the text it is, the way tmux does, so
#: nothing failed and nothing said so. Lillecarl/pymux#234.
#:
#: This is the key Lillecarl/pymux#220 exists for: a keyboard with no
#: Home key cannot answer a program that asks for ctrl+Home.
THE_KEYS_A_KEYBOARD_LEAVES_OUT = [
    ("C-Home", "\x1b[1;5H"),
    ("ctrl+home", "\x1b[1;5H"),
    ("C-End", "\x1b[1;5F"),
    ("ctrl+end", "\x1b[1;5F"),
    ("C-DC", "\x1b[3;5~"),
    ("ctrl+delete", "\x1b[3;5~"),
    ("C-PPage", "\x1b[5;5~"),
    ("ctrl+pageup", "\x1b[5;5~"),
    ("C-F5", "\x1b[15;5~"),
    ("ctrl+f5", "\x1b[15;5~"),
    ("S-Home", "\x1b[1;2H"),
    ("shift+home", "\x1b[1;2H"),
    ("ctrl+shift+end", "\x1b[1;6F"),
]


@pytest.mark.parametrize("name, expected", THE_KEYS_A_KEYBOARD_LEAVES_OUT)
def test_a_modified_key_the_older_table_never_named(pymux, name, expected):
    create_pane(pymux)
    written, errors = send(pymux, name)
    assert errors == []
    assert written == expected


def test_a_key_name_nobody_knows_goes_as_text(pymux):
    "tmux sends an argument it cannot name as the text it is."
    create_pane(pymux)
    assert send(pymux, "notakey")[0] == "notakey"


def test_dash_l_sends_the_names_as_text(pymux):
    "`-l` says to send what is written, so `Enter` is five letters."
    create_pane(pymux)
    assert send(pymux, "-l", "Enter")[0] == "Enter"


def test_keys_are_refused_while_a_person_reads_the_history(pymux):
    """
    The program is suspended in copy mode, so it cannot answer.
    Lillecarl/pymux#133.
    """
    pane = create_pane(pymux, "hello")
    pane.terminal.is_copying = True

    written, errors = send(pymux, "Enter")

    assert written == ""
    assert errors == ["pymux: Cannot send keys. Pane is in copy mode."]
