"""Helpers shared by the command modules."""

import argparse
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Pane
    from pymux.main import Pymux
    from pymux.arrangement import Window


from prompt_toolkit.application.current import get_app
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding.vi_state import InputMode
from pymux.commands import CommandException
from pymux.format import format_pymux_string
from pymux.key_spelling import why_a_pane_cannot_read
from pyte.keys import Unhearable


def find_window(pymux: "Pymux", target: str | None) -> Optional["Window"]:
    """
    Find a window for a tmux-style target.

    Supported targets: `@<window-id>`, `%<pane-id>` (the window that owns
    this pane), `<window-index>`, `:<window-index>`, and the window part of
    `session:window.pane`.
    """
    if target is None or target == "":
        return pymux.arrangement.get_active_window()

    # Strip the session part. (Pymux has one session per server.)
    if ":" in target:
        target = target.rsplit(":", 1)[1]

    # A pane ID target: `%<id>`. (Find the window that owns this pane.)
    if target.startswith("%"):
        pane = find_pane(pymux, target)
        if pane is not None:
            for w in pymux.arrangement.windows:
                if pane in w.panes:
                    return w
        return None

    if target.startswith("@"):
        window_id = target[1:]
        if window_id.isdigit():
            for w in pymux.arrangement.windows:
                if w.window_id == int(window_id):
                    return w

    if target.isdigit():
        return pymux.arrangement.get_window_by_index(int(target))

    return None


def find_pane(pymux: "Pymux", target: str | None) -> Optional["Pane"]:
    """
    Find a pane for a tmux-style target.

    Supported targets: `%<pane-id>`, `@<window-id>.<pane-index>`,
    `<window-index>.<pane-index>`, `.<pane-index>`, and the pane part of
    `session:window.pane`.
    """
    if target is None or target == "":
        return pymux.arrangement.get_active_pane()

    # A pane ID target: `%<id>`. (Look it up in all windows.)
    if target.startswith("%"):
        pane_id = target[1:]
        if pane_id.isdigit():
            pane_id_int = int(pane_id)
            for w in pymux.arrangement.windows:
                for p in w.panes:
                    if p.pane_id == pane_id_int:
                        return p
        return None

    window = pymux.arrangement.get_active_window()

    # Strip the session part.
    if ":" in target:
        _, _, target = target.rpartition(":")

    # Split off the pane part.
    pane_part: str | None = None
    if "." in target:
        target, _, pane_part = target.partition(".")

    if target:
        window = find_window(pymux, target)
        if window is None:
            return None

    if pane_part is None or pane_part == "":
        return window.active_pane

    if pane_part.isdigit():
        index = int(pane_part)
        if 0 <= index < len(window.panes):
            return window.panes[index]

    return None


def ask_the_person(
    pymux: "Pymux",
    message: str,
    command: str,
    default: str = "",
    completer=None,
) -> None:
    """
    Ask a question on the prompt, and run `command` with the answer in
    place of "%%".

    `completer` is what completes the answer, when the question knows
    what the answers are. A question that has one draws in a box, and
    the completions fill it.
    """
    client_state = pymux.get_client_state()

    client_state.prompt_text = message
    client_state.prompt_command = command
    client_state.prompt_completer = completer

    client_state.prompt_mode = True
    client_state.prompt_buffer.reset(Document(format_pymux_string(pymux, default)))

    get_app().layout.focus(client_state.prompt_buffer)
    get_app().vi_state.input_mode = InputMode.INSERT


def send_a_key(pane, event, written: str) -> None:
    """
    Write one key to a pane, or say that the pane cannot read it.

    **The pane decides what it can read, so the pane is asked.** A
    legacy pane turns super+a into "a" and ctrl+shift+a into ctrl+a,
    and used to be sent those in silence. Nobody typing `send-keys
    super+a` means "type an a". Lillecarl/pymux#237.
    """
    try:
        pane.process.write_input(pane.screen.encode_key_event(event, exactly=True))
    except Unhearable as cannot:
        raise CommandException(why_not(written, cannot))


def why_not(written: str, cannot: Unhearable) -> str:
    "Why a pane could not read a key, naming what a person wrote."
    return "%s: %s" % (
        written,
        why_a_pane_cannot_read(cannot.event, cannot.lost, cannot.encoded),
    )


def option_as_written(
    pymux: "Pymux", option, args: argparse.Namespace, window: bool
) -> str:
    """
    What an option holds, as a person wrote it.

    The on/off options hold booleans and a person writes on and off;
    the rest hold what they were given. A window option reads the
    window that is active, or, with `-g`, the default every new
    window starts with -- which is recorded only when somebody set
    it, so one that was never set reads as not set. `-g` says nothing
    for a session option, on the read as on the write. An option that
    holds its state somewhere else than one attribute -- the prefix
    key lives in the binding manager -- reads as not set too.
    """
    if option.attribute_name is None:
        return "not set"
    if window and args.g:
        value = pymux.arrangement.window_defaults.get(option.attribute_name)
    else:
        holder = pymux.arrangement.get_active_window() if window else pymux
        value = getattr(holder, option.attribute_name, None)
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def answer(pymux: "Pymux", text: str) -> None:
    """
    Answer a question on the channel the asker reads.

    A command that arrives over the command line gets stdout, which
    is what `command_output` says. A person typing at the prompt of a
    pane gets the message line, because there is no stdout to write
    to. Lillecarl/pymux#289.
    """
    if pymux.command_output is not None:
        pymux.print_command_line(text)
    else:
        pymux.get_client_state().message = text


def show_listing(pymux: "Pymux", title: str, text: str) -> None:
    """
    A listing, to the person who asked for it.

    A person in a pane asked for a view and gets the popup. The
    command line has no view to show a listing in, and used to lose
    it there: the popup went to the client that ran the command, and
    there is no client to ask for when the command came over the
    command line -- `get_client_state` has no app to answer with,
    which is one reason the listing was silence and not a fault. That
    asker reads stdout, so the listing is printed there, which is
    what `command_output` says: the same question
    `print_command_line` asks. tmux prints a listing to stdout,
    always. Lillecarl/pymux#288.
    """
    if pymux.command_output is not None:
        pymux.print_command_line(text)
    else:
        pymux.get_client_state().layout_manager.display_popup(title, text)


def print_object_format(
    pymux: "Pymux",
    format_str: str | None,
    window: "Window",
    pane: "Pane",
) -> None:
    """
    Print the information of a newly created object. (Like `tmux
    new-window -P`.)
    """
    if format_str is None:
        format_str = "#{session_name}:#{window_index}.#{pane_index}"
    pymux.print_command_line(
        format_pymux_string(pymux, format_str, window=window, pane=pane)
    )
