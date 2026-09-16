"""
Key bindings.
"""

import logging
from typing import TYPE_CHECKING, Callable, Dict, Tuple

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import (
    Condition,
    Filter,
    emacs_mode,
    has_focus,
    vi_navigation_mode,
)
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.bindings.auto_suggest import (
    load_auto_suggest_bindings,
)
from prompt_toolkit.key_binding.key_processor import KeyPressEvent as E
from prompt_toolkit.keys import Keys

from .commands import call_command_handler
from .commands.utils import wrap_argument
from .enums import COMMAND, PROMPT
from .filters import HasPrefix, KeyTableIs, ModeActive, WaitsForConfirmation
from .key_spelling import key_however_it_is_written

if TYPE_CHECKING:
    from pymux.main import Pymux

logger = logging.getLogger(__name__)

__all__ = ["PymuxKeyBindings"]

#: The two tables a client is always between: the one its keys are
#: read from while the prefix is up, and the one for the rest of the
#: time. A mode adds its own named table on top.
ROOT_TABLE = "root"
PREFIX_TABLE = "prefix"

#: One binding: the table its key is read from, and the keys
#: themselves. The keys are prompt_toolkit's, so every spelling of one
#: key gives the same one.
_ABinding = Tuple[str, Tuple[str, ...]]


class KeyTable:
    """
    A named table of keys, and what entering it does.

    **A table is a mode when a person can be in it.** The name is what
    `enter-mode` takes, what `bind-key -T` binds into, and what
    `#{client_key_table}` reports while it is on top. `on_enter` and
    `on_leave` are the mode's own side effects, and nothing more: the
    keys themselves are the bindings the table holds.

    A key the table holds no binding for falls through to the pane.
    That is what makes a mode transparent, and strictness is a
    binding of its own: `bind-key -T <table> Any noop` swallows
    everything the table does not name. Lillecarl/pymux#394.
    """

    def __init__(self, name: str, on_enter=None, on_leave=None) -> None:
        self.name = name
        #: The mode's own side effects. Each takes the pymux.
        self.on_enter = on_enter
        self.on_leave = on_leave


def _hold_pane_numbers_up(pymux: "Pymux") -> None:
    "The numbers are how a person reads a mode that moves the focus."
    pymux.display_pane_numbers = True


def _put_pane_numbers_back(pymux: "Pymux") -> None:
    pymux.display_pane_numbers = False


def _register_builtin_mode_tables(manager: "PymuxKeyBindings") -> None:
    """
    The modes pymux ships with, with the side effects their keys
    cannot say.

    The keys themselves are the `bind-key -T` lines of the initial
    configuration, so a person who rebinds one rebinds it; the table
    is here so that entering it means something even then.
    Lillecarl/pymux#395.
    """
    manager.mode_tables["pane-management"] = KeyTable(
        "pane-management",
        on_enter=_hold_pane_numbers_up,
        on_leave=_put_pane_numbers_back,
    )


class PymuxKeyBindings:
    """
    Pymux key binding manager.
    """

    def __init__(self, pymux: "Pymux") -> None:
        self.pymux = pymux

        self.custom_key_bindings = KeyBindings()

        #: The modes a person can be in, by the name `enter-mode`
        #: takes. `bind-key -T` creates a table without hooks here
        #: when it meets a name this does not hold. Lillecarl/pymux#394.
        self.mode_tables: Dict[str, KeyTable] = {}
        _register_builtin_mode_tables(self)

        self.key_bindings = merge_key_bindings(
            [
                self._load_builtins(),
                # The right arrow accepts what the command line is
                # suggesting. prompt_toolkit writes the binding and
                # loads it from `PromptSession` alone, so an
                # application that builds its own bindings draws the
                # grey text and gives a person no way to take it.
                #
                # **It comes after the rest**, which upstream asks for
                # in the docstring of that function: the vi bindings
                # have a right arrow of their own, and the suggestion
                # has to win while there is one. Lillecarl/pymux#163.
                load_auto_suggest_bindings(),
                self.custom_key_bindings,
            ]
        )

        self._prefix: Tuple[str, ...] = ("c-b",)
        self._prefix_binding: Callable[[E], None] | None = None

        # Load initial bindings.
        self._load_prefix_binding()

        #: The bindings a person made, by the key each one reaches.
        #:
        #: **Not by the name they wrote.** One key has many names:
        #: "ctrl+a", "C-a" and "c-a" all reach `Keys.ControlA`, so a
        #: dictionary keyed by the text held one key three times, and
        #: `unbind-key` under a second spelling removed nothing while
        #: `bind-key` under it left two handlers on one key.
        #: Lillecarl/pymux#235.
        self.custom_bindings: Dict[_ABinding, CustomBinding] = {}

    def _load_prefix_binding(self) -> None:
        """
        Load the prefix key binding.
        """
        pymux = self.pymux

        # Remove previous binding.
        if self._prefix_binding:
            self.custom_key_bindings.remove_binding(self._prefix_binding)

        # Create new Python binding.
        @self.custom_key_bindings.add(
            *self._prefix,
            filter=~(
                HasPrefix(pymux)
                | has_focus(COMMAND)
                | has_focus(PROMPT)
                | WaitsForConfirmation(pymux)
            ),
        )
        def enter_prefix_handler(event: E) -> None:
            "Enter prefix mode."
            client_state = pymux.get_client_state()
            client_state.has_prefix = True
            # The popup that lists what the prefix leads to draws only
            # when something asks for a frame, and the prefix alone
            # changes nothing else. `which-key` is the one thing that
            # wants one here. Lillecarl/pymux#29.
            if pymux.which_key:
                client_state.app.invalidate()

        self._prefix_binding = enter_prefix_handler

    @property
    def prefix(self) -> Tuple[str, ...]:
        "Get the prefix key."
        return self._prefix

    @prefix.setter
    def prefix(self, keys: Tuple[str, ...]) -> None:
        """
        Set a new prefix key.
        """
        self._prefix = keys
        self._load_prefix_binding()

    def _load_builtins(self) -> KeyBindings:
        """
        Fill the Registry with the hard coded key bindings.
        """
        pymux = self.pymux
        kb = KeyBindings()

        # Create filters.
        has_prefix = HasPrefix(pymux)
        waits_for_confirmation = WaitsForConfirmation(pymux)
        prompt_or_command_focus = has_focus(COMMAND) | has_focus(PROMPT)
        display_pane_numbers = Condition(lambda: pymux.display_pane_numbers)
        mode_active = ModeActive(pymux)

        @kb.add(Keys.Any, filter=has_prefix)
        def _(event: E) -> None:
            "Ignore unknown Ctrl-B prefixed key sequences."
            pymux.get_client_state().has_prefix = False

        @kb.add("escape", filter=prompt_or_command_focus & ~has_prefix & emacs_mode)
        @kb.add(
            "escape",
            filter=prompt_or_command_focus & ~has_prefix & vi_navigation_mode,
        )
        @kb.add("c-c", filter=prompt_or_command_focus & ~has_prefix)
        @kb.add("c-g", filter=prompt_or_command_focus & ~has_prefix)
        #        @kb.add('backspace', filter=has_focus(COMMAND) & ~has_prefix &
        #                              Condition(lambda: cli.buffers[COMMAND].text == ''))
        def _leave_command_mode(event: E) -> None:
            """
            Leave command mode.

            **With vi status keys it takes two Escapes.** The first one
            is vi's: it leaves insert mode and the line stays open,
            which is what tmux does and what Lillecarl/pymux#157
            decided. The second one closes, because a person in normal
            mode who presses Escape is asking to leave.

            That hole is what Lillecarl/pymux#255 was: Escape belonged
            to vi and nothing took the second press, so with vi status
            keys no number of Escapes closed the box at all. The mode
            the line is in is drawn on it, so a person can see which
            press they are about to make.

            A bare Escape is safe to bind. prompt_toolkit reports one
            only after its own timeout has ruled out a longer escape
            sequence.
            """
            pymux.leave_command_mode(append_to_history=False)

        @kb.add("y", filter=waits_for_confirmation)
        @kb.add("Y", filter=waits_for_confirmation)
        def _confirm(event: E) -> None:
            """
            Confirm command.
            """
            client_state = pymux.get_client_state()

            command = client_state.answer()
            if command:
                pymux.handle_command(command)

        @kb.add("n", filter=waits_for_confirmation)
        @kb.add("N", filter=waits_for_confirmation)
        @kb.add("c-c", filter=waits_for_confirmation)
        def _cancel(event: E) -> None:
            """
            Cancel command.
            """
            # `n` answers the question on screen and leaves the rest
            # waiting, the way `y` does. Lillecarl/pymux#266.
            client_state = pymux.get_client_state()
            client_state.answer()

        # Five bindings for copy mode stood here: leaving it, starting
        # a selection, copying one and swapping its type. Every one of
        # them was guarded by a filter that read a flag nothing had set
        # since copy mode moved into `ptterm`, so none of them ever
        # fired, and `pane.exit_scroll_buffer` does not exist.
        #
        # The keys of copy mode live with the copy buffer now, in
        # `ptterm.terminal.Terminal`. Lillecarl/pymux#133.

        @Condition
        def popup_displayed() -> bool:
            return self.pymux.get_client_state().display_popup

        @kb.add("q", filter=popup_displayed, eager=True)
        def _quit_popup(event: E) -> None:
            "Quit pop-up dialog."
            self.pymux.get_client_state().display_popup = False

        # The choosers. One has the focus while it shows, which is
        # what keeps the pane from taking these keys; the same reason
        # the pop-up's `q` above answers.
        #
        # h and l walk the entries, j and k the lines the bar wraps
        # them onto, and the arrows go with them. Enter takes the one
        # the point is on and Escape goes back. The search of `/`
        # takes the focus; while it has it, everything types into it,
        # the list narrows as it does, and Escape brings the keys
        # back. Lillecarl/pymux#295. Lillecarl/pymux#327.
        @Condition
        def chooser_displayed() -> bool:
            state = self.pymux.get_client_state()
            return (
                state.choose_window
                or state.choose_buffer
                or state.choose_options
            )

        @Condition
        def chooser_search_focused() -> bool:
            state = self.pymux.get_client_state()
            return (state.choose_window or state.choose_buffer) and has_focus(
                state.choose_window_filter
            )()

        @Condition
        def window_bar() -> bool:
            "The chooser that flows across a bar, and not the box."
            return self.pymux.get_client_state().choose_window

        @kb.add("/", filter=chooser_displayed & ~chooser_search_focused)
        def _chooser_search(event: E) -> None:
            "Search the names; what is typed narrows the list."
            get_app().layout.focus(
                self.pymux.get_client_state().layout_manager.chooser_search_control()
            )

        @kb.add("left", filter=chooser_displayed & ~chooser_search_focused)
        @kb.add("h", filter=chooser_displayed & ~chooser_search_focused)
        @kb.add("up", filter=chooser_displayed & ~window_bar)
        @kb.add("k", filter=chooser_displayed & ~window_bar)
        @kb.add("c-p", filter=chooser_displayed)
        def _chooser_back(event: E) -> None:
            "The entry before this one, staying at the first."
            state = self.pymux.get_client_state()
            state.layout_manager.point_at(state.choose_window_index - 1)

        @kb.add("right", filter=chooser_displayed & ~chooser_search_focused)
        @kb.add("l", filter=chooser_displayed & ~chooser_search_focused)
        @kb.add("down", filter=chooser_displayed & ~window_bar)
        @kb.add("j", filter=chooser_displayed & ~window_bar)
        @kb.add("c-n", filter=chooser_displayed)
        def _chooser_on(event: E) -> None:
            "The entry after this one, staying at the last."
            state = self.pymux.get_client_state()
            state.layout_manager.point_at(state.choose_window_index + 1)

        # The bar wraps, so up and down are a line and not an entry.
        # In the box a line holds one entry and the two agree, which
        # is why only the bar binds these. Lillecarl/pymux#327.
        @kb.add("up", filter=window_bar & ~chooser_search_focused)
        @kb.add("k", filter=window_bar & ~chooser_search_focused)
        def _bar_up(event: E) -> None:
            "The line above, keeping the place along it."
            self.pymux.get_client_state().layout_manager.step_line(-1)

        @kb.add("down", filter=window_bar & ~chooser_search_focused)
        @kb.add("j", filter=window_bar & ~chooser_search_focused)
        def _bar_down(event: E) -> None:
            "The line below, keeping the place along it."
            self.pymux.get_client_state().layout_manager.step_line(1)

        @kb.add("enter", filter=chooser_displayed & ~chooser_search_focused)
        def _chooser_choose(event: E) -> None:
            "Take the row the chooser points at."
            state = self.pymux.get_client_state()
            if state.choose_options:
                state.layout_manager.choose_pointed_option()
            elif state.choose_buffer:
                state.layout_manager.choose_pointed_buffer()
            else:
                state.layout_manager.choose_pointed_window()

        @kb.add("q", filter=chooser_displayed & ~chooser_search_focused, eager=True)
        @kb.add(
            "escape", filter=chooser_displayed & ~chooser_search_focused, eager=True
        )
        @kb.add("c-c", filter=chooser_displayed & ~chooser_search_focused, eager=True)
        def _quit_chooser(event: E) -> None:
            """
            Leave the chooser without taking anything.

            The window chooser switched this client while a person
            moved through it, so leaving puts the client back on the
            window it started from. Lillecarl/pymux#327.
            """
            state = self.pymux.get_client_state()
            state.layout_manager.leave_chooser(restore=True)

        @kb.add("escape", filter=chooser_search_focused, eager=True)
        def _quit_chooser_search(event: E) -> None:
            "Leave the search, keeping the chooser."
            state = self.pymux.get_client_state()
            state.choose_window_filter.reset()
            get_app().layout.focus(state.layout_manager.chooser_rows_control())

        # The menu that `display-menu` opened. It is modal: the keys
        # of its entries are the keys it takes, Escape and ctrl+c
        # leave it, and everything else stays in it. The named keys of
        # an entry -- Enter, Space, Tab -- come in as their own keys,
        # and a letter or a digit comes in as the character it is.
        # Lillecarl/pymux#297.
        @Condition
        def menu_displayed() -> bool:
            return bool(self.pymux.get_client_state().menu_entries)

        @kb.add("enter", filter=menu_displayed, eager=True)
        def _menu_enter(event: E) -> None:
            "The entry whose key is Enter, when there is one."
            self.pymux.get_client_state().layout_manager.menu_key_pressed(event.key)

        @kb.add("escape", filter=menu_displayed, eager=True)
        @kb.add("c-c", filter=menu_displayed, eager=True)
        def _quit_menu(event: E) -> None:
            "Leave the menu without taking anything."
            self.pymux.get_client_state().close_menu()

        @kb.add(Keys.Any, filter=menu_displayed, eager=True)
        def _menu_key(event: E) -> None:
            "The entry whose key this is, or nothing: the menu stays."
            self.pymux.get_client_state().layout_manager.menu_key_pressed(
                event.key, event.data
            )

        @kb.add(Keys.KeyRelease, eager=True)
        def _forward_key_release(event: E) -> None:
            """
            A key came back up. Give the sequence to the pane that the
            keyboard of this client reaches.

            A release is not a key press, so it drives nothing here: it
            does not leave the prefix, hide the pane numbers or stop
            the clock. The binding is eager for that reason, because an
            eager binding for any key would take it otherwise.

            The pane drops the release when it did not ask for the
            event types of a key. A press that this client kept, like
            the prefix key, still sends its release on, so a pane can
            see a release with no press. Holding the pressed keys of
            every pane to stop that costs more than it is worth.
            """
            pane = pymux.get_focused_pane()
            if pane is None or not event.data:
                return
            try:
                if not get_app().layout.has_focus(pane.terminal):
                    # The keyboard is on the command line or a prompt.
                    return
                pane.process.write_input(pane.screen.encode_key(event.data))
            except Exception:
                logger.exception("Forwarding a key release failed.")

        @kb.add(Keys.Any, eager=True, filter=display_pane_numbers & ~mode_active)
        def _hide_numbers(event: E) -> None:
            """
            When the pane numbers are shown. Any key press should hide
            them.

            **Not the keys of a mode.** Pane-management holds the
            numbers up for its whole stay: its keys move the focus and
            resize, and the numbers are how a person reads what they
            did. Lillecarl/pymux#395.
            """
            pymux.display_pane_numbers = False

        @Condition
        def clock_displayed() -> bool:
            " "
            pane = pymux.arrangement.get_active_pane()
            return pane.clock_mode

        @kb.add(Keys.Any, eager=True, filter=clock_displayed)
        def _hide_clock(event: E) -> None:
            "When the clock is displayed. Any key press should hide it."
            pane = pymux.arrangement.get_active_pane()
            pane.clock_mode = False

        return kb

    def add_custom_binding(
        self, key_name: str, command: str, arguments: list, table: str = PREFIX_TABLE
    ) -> None:
        """
        Add custom binding (for the "bind-key" command.)
        Raises ValueError if the give `key_name` is an invalid name.

        :param key_name: Pymux key name, for instance "C-a", "M-x" or
            "ctrl+home".
        :param table: The table the key is read from: `root` for a
            binding that answers without the prefix (`-n`), the name
            of a mode for `bind-key -T`. The default is the prefix
            table, what a bare `bind-key` has always meant.
        """
        # Translate the name into a prompt_toolkit key sequence, in
        # either spelling. (Can raise ValueError.)
        keys_sequence = key_however_it_is_written(key_name)

        # Unbind the key, under whichever name it was bound.
        self.remove_custom_binding(key_name, table=table)

        # A mode a person binds into exists from then on, the way it
        # does in tmux: the first `bind-key -T` is what names it.
        if table not in (ROOT_TABLE, PREFIX_TABLE) and table not in self.mode_tables:
            self.mode_tables[table] = KeyTable(table)

        # The binding answers only when this table is the one the
        # client's next key is read from. That is the whole of the
        # masking: while a mode is on top, the root table and the
        # prefix table hold nothing the mode does not.
        filter: Filter = KeyTableIs(self.pymux, table)
        filter = filter & ~(
            WaitsForConfirmation(self.pymux) | has_focus(COMMAND) | has_focus(PROMPT)
        )

        def key_handler(event: E) -> None:
            """
            Run the command, and take the prefix off this client.

            **The client is read before the command runs.** A command
            can take this client away, and `detach-client` does. Asking
            for it afterwards raised `ValueError` out of the key
            handler, on a terminal that the client had already put
            back. Lillecarl/pymux#109.

            The object outlives the client, so writing to it after the
            client has gone changes nothing that anybody reads.
            """
            client_state = self.pymux.get_client_state()
            self.pymux.spawn_command(
                call_command_handler(command, self.pymux, arguments)
            )
            client_state.has_prefix = False

        # An `Any` binding is eager, or it loses to an eager one below
        # it: the pane's own keys and `_hide_numbers` are any-key
        # bindings too, and waiting for a longer sequence that cannot
        # come hands the key to them. The same lesson
        # `_load_builtins` records on `_hide_numbers`.
        self.custom_key_bindings.add(
            *keys_sequence, filter=filter, eager=(keys_sequence == (Keys.Any,))
        )(key_handler)

        self.custom_bindings[table, keys_sequence] = CustomBinding(
            key_handler, command, arguments, key_name
        )

    def enter_mode(self, name: str) -> None:
        """
        Put a mode's table on top of this client's stack of them.

        Raises `KeyError` for a name no table holds, and `ValueError`
        when no client is here to enter it.
        """
        table = self.mode_tables[name]  # KeyError for a name nobody bound.
        client_state = self.pymux.get_client_state()
        client_state.key_tables.append(name)
        if table.on_enter:
            table.on_enter(self.pymux)
        client_state.app.invalidate()

    def leave_mode(self) -> None:
        """
        Take the top mode off this client's stack, and put back what
        its entering changed.

        Raises `ValueError` when no client is here, and
        `IndexError` when no mode is on the stack: `leave-mode` typed
        at the command line with nothing to leave is a mistake worth
        saying so, and not a silence.
        """
        client_state = self.pymux.get_client_state()
        name = client_state.key_tables.pop()
        table = self.mode_tables[name]
        if table.on_leave:
            table.on_leave(self.pymux)
        client_state.app.invalidate()

    def prefix_keys(self) -> "list[tuple[str, str]]":
        """
        The keys that follow the prefix, each with what it does.

        One row per binding the prefix reaches: the key as the person
        wrote it, and the command it runs with its arguments, spelled
        the way `list-keys` spells a binding. `which-key` reads this
        to draw its popup, and the view of the bindings that
        Lillecarl/pymux#30 asks for reads the same. Sorted by key, so
        the same table draws twice the same.
        """
        rows = []
        for (table, _keys), binding in self.custom_bindings.items():
            if table != PREFIX_TABLE:
                continue
            meaning = binding.command
            if binding.arguments:
                meaning += " " + " ".join(map(wrap_argument, binding.arguments))
            rows.append((binding.written, meaning))
        return sorted(rows)

    def binding_on(
        self, key_name: str, table: str = PREFIX_TABLE
    ) -> "CustomBinding | None":
        """
        What a key runs, under any name for that key, or None.

        Raises `ValueError` when the name reads as no key at all.
        """
        return self.custom_bindings.get(
            (table, key_however_it_is_written(key_name))
        )

    def remove_custom_binding(self, key_name: str, table: str = PREFIX_TABLE) -> None:
        """
        Remove the binding on a key, under any name for that key.

        Raises `ValueError` when the name reads as no key at all.

        :param key_name: Pymux key name, for instance "C-A".
        """
        k = (table, key_however_it_is_written(key_name))

        if k in self.custom_bindings:
            self.custom_key_bindings.remove(self.custom_bindings[k].handler)
            del self.custom_bindings[k]


class CustomBinding:
    """
    Record for storing a single custom key binding.
    """

    def __init__(
        self,
        handler: Callable[[E], None],
        command: str,
        arguments: list,
        written: str,
    ) -> None:
        self.handler = handler
        self.command = command
        self.arguments = arguments
        #: The name the person wrote. The binding is not held under it,
        #: so this is what `list-keys` shows and nothing more.
        self.written = written
