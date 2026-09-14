"""Helpers shared by the command modules."""

import argparse
from typing import NamedTuple, Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pymux.arrangement import Pane
    from pymux.main import Pymux
    from pymux.arrangement import Window
    from pymux.session import Session


from prompt_toolkit.application.current import get_app
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding.vi_state import InputMode
from pymux.commands import CommandException
from pymux.format import Language, format_pymux_string
from pymux.key_spelling import why_pane_cannot_read
from pymux.options import Scope, SetOptionError
from pyte.keys import Unhearable


def session_part(pymux: "Pymux", target: str) -> "tuple[Session | None, str]":
    """
    Split a `session:rest` target into the session and the rest.

    No colon, or nothing before it, means the session of the client
    that asks. A name before the colon that no session has gives
    `None`, which every caller answers as "not found": a target that
    names a session that is not there must not quietly answer with a
    window of another one. Lillecarl/pymux#323.
    """
    if ":" not in target:
        return pymux.current_session, target

    name, _, rest = target.rpartition(":")
    if not name:
        return pymux.current_session, rest

    return pymux.get_session(name), rest


def find_window(pymux: "Pymux", target: str | None) -> Optional["Window"]:
    """
    Find a window for a tmux-style target.

    Supported targets: `@<window-id>`, `%<pane-id>` (the window that owns
    this pane), `<window-index>`, `:<window-index>`, and the window part of
    `session:window.pane`.

    A window id and a pane id name one thing on the whole server, so
    they are looked up across every session and never take a session
    part.
    """
    if target is None or target == "":
        return pymux.arrangement.get_active_window()

    # A pane ID target: `%<id>`. (Find the window that owns this pane.)
    if target.startswith("%"):
        pane = find_pane(pymux, target)
        return pymux._window_holding(pane) if pane is not None else None

    if target.startswith("@"):
        window_id = target[1:]
        if window_id.isdigit():
            for session in pymux.sessions:
                for w in session.arrangement.windows:
                    if w.window_id == int(window_id):
                        return w
        return None

    session, target = session_part(pymux, target)
    if session is None:
        return None

    return window_in(session, target)


def window_in(session: "Session", target: str) -> Optional["Window"]:
    "The window a target names inside one session."
    if target == "":
        return session.arrangement.get_active_window()

    if target.isdigit():
        return session.arrangement.get_window_by_index(int(target))

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

    # A pane ID target: `%<id>`. The server knows every pane by id, so
    # this reaches across the sessions.
    if target.startswith("%"):
        pane_id = target[1:]
        if pane_id.isdigit():
            return pymux.panes_by_id.get(int(pane_id))
        return None

    session, target = session_part(pymux, target)
    if session is None:
        return None

    window = session.arrangement.get_active_window()

    # Split off the pane part.
    pane_part: str | None = None
    if "." in target:
        target, _, pane_part = target.partition(".")

    if target:
        # Inside the session the target named, and not the one the
        # client is on: `work:2.1` is the second pane of window 2 of
        # `work`.
        window = (
            find_window(pymux, target)
            if target.startswith("@")
            else window_in(session, target)
        )
        if window is None:
            return None

    if pane_part is None or pane_part == "":
        return window.active_pane

    if pane_part.isdigit():
        index = int(pane_part)
        if 0 <= index < len(window.panes):
            return window.panes[index]

    return None


def ask_person(
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


def send_key(pane, event, written: str) -> None:
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
        why_pane_cannot_read(cannot.event, cannot.lost, cannot.encoded),
    )


def clients_named(pymux: "Pymux", wanted: str) -> list:
    """
    Every client of that name, as `list-clients` prints it first on a
    line.

    **Either name selects a client**: the one a person chose, and the
    one pymux derived from the machine and the terminal. So a script
    that learned `dynhetz:/dev/pts/7` keeps working after somebody
    calls that terminal `desk`, and a person who named it can say what
    they named it. tmux matches several spellings of one client the
    same way (`cmd-find.c:1322`).

    **A list and not one.** A derived name is not unique either -- two
    machines can each have a `/dev/pts/3` -- and a chosen one is not
    checked, so `detach-client -t desk` detaches every terminal called
    `desk`, which is what the words say. A command that can only act
    on one takes the first. Lillecarl/pymux#335, Lillecarl/pymux#340.
    """
    found = [
        client_state
        for client_state in pymux.clients
        if wanted
        in (
            getattr(client_state, "name", ""),
            getattr(client_state.connection, "name", ""),
        )
    ]
    if not found:
        raise CommandException("can't find client: %s" % (wanted,))
    return found


def option_as_written(
    pymux: "Pymux", option, args: argparse.Namespace, target=None
) -> str:
    """
    What an option holds, as a person wrote it.

    The on/off options hold booleans and a person writes on and off;
    the rest hold what they were given. The option's own scope says
    what holds it: the session, the active window, or the client this
    command means. `-g` on a window option reads the default every new
    window starts with -- which is recorded only when somebody set it,
    so one that was never set reads as not set -- and says nothing for
    the other two scopes, on the read as on the write.

    Nothing holding the value reads as not set: a window option with
    no window yet, a client option with nobody attached, and an option
    that keeps its state somewhere else than one attribute, the way
    the prefix key lives in the binding manager.
    """
    if option.attribute_name is None:
        return "not set"

    if option.scope is Scope.WINDOW and getattr(args, "g", False):
        value = pymux.arrangement.window_defaults.get(option.attribute_name)
    else:
        try:
            holder = option.held_by(pymux, target)
        except SetOptionError:
            return "not set"
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


class ChosenFormat(NamedTuple):
    "What a command prints, and the language it is written in."

    string: str
    language: Language

    #: Whether the caller asked for a format at all. A listing draws a
    #: line for each object when they did, and its own overview when
    #: they did not.
    asked: bool


def add_format_arguments(parser, help_text: str) -> None:
    """
    Add `-F` and `-J` to a command that prints a format.

    Neither is sniffed. `-F` is tmux format, which is what libtmux and
    libpymux read; `-J` is a jinja2 template, for a person who wants a
    condition or a filter in what they print. Lillecarl/pymux#333.

    **`J` means something else on `capture-pane`**, where it is tmux's
    own flag for joining the pieces a wrapped line was cut into. The
    two never meet, because `capture-pane` prints no format and so
    never calls this. A command that is given a format later has to be
    read for a `-J` of its own first: tmux spells one on
    `show-messages`, for jobs.
    """
    parser.add_argument("-F", dest="format", metavar="<format>", help=help_text)
    parser.add_argument(
        "-J",
        dest="template",
        metavar="<template>",
        help="%s Written as a jinja2 template." % (help_text,),
    )


def chosen_format(args: argparse.Namespace, default: str) -> ChosenFormat:
    "The format this command prints, by which flag asked for it."
    template = getattr(args, "template", None)
    if template:
        return ChosenFormat(template, Language.JINJA, True)
    return ChosenFormat(args.format or default, Language.TMUX, bool(args.format))


#: What `-P` prints when nothing named a format. (tmux's own.)
NEW_OBJECT_FORMAT = "#{session_name}:#{window_index}.#{pane_index}"


def print_object_format(
    pymux: "Pymux",
    args: argparse.Namespace,
    window: "Window",
    pane: "Pane",
    session: "Session | None" = None,
) -> None:
    """
    Print the information of a newly created object. (Like `tmux
    new-window -P`.)
    """
    chosen = chosen_format(args, NEW_OBJECT_FORMAT)
    pymux.print_command_line(
        format_pymux_string(
            pymux,
            chosen.string,
            window=window,
            pane=pane,
            session=session,
            language=chosen.language,
        )
    )
