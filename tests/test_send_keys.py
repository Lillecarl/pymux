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


def a_pane(mux, data: str = ""):
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
    pane = a_pane(pymux, "hello\r\nworld")
    assert pane.screen.page.data_buffer

    send(pymux, "-R")

    assert not pane.screen.page.data_buffer


def test_a_reset_puts_the_cursor_home(pymux):
    pane = a_pane(pymux, "hello\r\nworld")

    send(pymux, "-R")

    assert (pane.screen.pt_cursor_position.y, pane.screen.pt_cursor_position.x) == (
        0,
        0,
    )


def test_a_reset_takes_back_a_mode_a_program_set(pymux):
    "DECSCNM turns the whole pane over, and a person cannot type it off."
    pane = a_pane(pymux, set_mode(PrivateMode.REVERSE_VIDEO))
    assert pane.screen.has_reverse_video

    send(pymux, "-R")

    assert not pane.screen.has_reverse_video


def test_a_reset_leaves_the_alternate_screen(pymux):
    "A program that died inside vim leaves the pane on the other screen."
    pane = a_pane(pymux, set_mode(PrivateMode.ALTERNATE_SCREEN_WITH_CURSOR))
    assert pane.screen.in_alternate_screen

    send(pymux, "-R")

    assert not pane.screen.in_alternate_screen


def test_a_reset_says_nothing_to_the_program(pymux):
    "It puts the terminal back. The program hears nothing of it."
    a_pane(pymux, "hello")
    assert send(pymux, "-R") == ("", [])


def test_the_keys_reach_the_program(pymux):
    a_pane(pymux)
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
    ("Home", "\x1b[1~"),
    ("End", "\x1b[4~"),
    ("BSpace", "\x08"),
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
    a_pane(pymux)
    written, errors = send(pymux, name)
    assert errors == []
    assert written == expected


def test_control_and_shift_on_a_letter_sends_what_a_keyboard_sends(pymux):
    """
    A binding can name this key, and `send-keys` still has to give a
    pane the bytes a keyboard would have given it. The legacy encoding
    has no form of its own here -- ctrl+a and ctrl+shift+a are one
    control code -- so the answer is the control code.
    Lillecarl/pymux#168.
    """
    a_pane(pymux)

    written, errors = send(pymux, "C-S-a")

    assert errors == []
    assert written == "\x01"


def test_a_key_with_super_can_be_bound_and_sent(pymux):
    """
    tmux has no spelling for super, hyper or meta, so pymux writes
    them out. The order they are written in does not matter, and the
    case does not either. Lillecarl/pymux#181.
    """
    a_pane(pymux)

    for spelling in ("Super-a", "super-a", "SUPER-A"):
        assert send(pymux, spelling) == ("a", [])
    assert send(pymux, "Super-C-a") == ("\x01", [])
    assert send(pymux, "C-Super-a") == ("\x01", [])


def test_a_pane_gets_what_a_keyboard_it_can_hear_would_have_sent(pymux):
    """
    The legacy encoding cannot carry super, hyper or meta at all, so
    what is left is the key with the modifiers it can carry. The same
    trade `send-keys C-S-a` makes.
    """
    a_pane(pymux)

    assert send(pymux, "Super-S-a") == ("A", [])
    assert send(pymux, "Super-up") == ("\x1b[A", [])
    assert send(pymux, "Super-enter") == ("\r", [])
    assert send(pymux, "Hyper-escape") == ("\x1b", [])


def test_the_shift_spelling_is_case_insensitive(pymux):
    "A person writes a key the way it reads."
    a_pane(pymux)

    assert send(pymux, "c-s-z")[0] == "\x1a"
    assert send(pymux, "C-S-z")[0] == "\x1a"


def test_an_arrow_is_the_application_form_for_a_pane_that_asked(pymux):
    """
    "\\x1bOA" is the application cursor form, and DECCKM turns it on.

    The pane above asks for it, so it gets it. A pane that asks for
    nothing gets "\\x1b[A", which is the row for "Up" in the table
    above. `send-keys Up` used to send the application form to both,
    because the table held it as the plain answer.
    """
    a_pane(pymux, set_mode(PrivateMode.APPLICATION_CURSOR_KEYS))
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
    a_pane(pymux)
    written, errors = send(pymux, name)
    assert errors == []
    assert written == expected


def test_a_key_name_nobody_knows_goes_as_text(pymux):
    "tmux sends an argument it cannot name as the text it is."
    a_pane(pymux)
    assert send(pymux, "notakey")[0] == "notakey"


def test_dash_l_sends_the_names_as_text(pymux):
    "`-l` says to send what is written, so `Enter` is five letters."
    a_pane(pymux)
    assert send(pymux, "-l", "Enter")[0] == "Enter"


def test_keys_are_refused_while_a_person_reads_the_history(pymux):
    """
    The program is suspended in copy mode, so it cannot answer.
    Lillecarl/pymux#133.
    """
    pane = a_pane(pymux, "hello")
    pane.terminal.is_copying = True

    written, errors = send(pymux, "Enter")

    assert written == ""
    assert errors == ["pymux: Cannot send keys. Pane is in copy mode."]
