"""
Key bindings.
"""

import logging
from typing import TYPE_CHECKING, Callable, Dict, Tuple

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition, Filter, emacs_mode, has_focus
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.bindings.auto_suggest import (
    load_auto_suggest_bindings,
)
from prompt_toolkit.key_binding.key_processor import KeyPressEvent as E
from prompt_toolkit.keys import Keys

from .commands.commands import call_command_handler
from .commands.utils import wrap_argument
from .enums import COMMAND, PROMPT
from .filters import HasPrefix, WaitsForConfirmation
from .key_spelling import key_however_it_is_written

if TYPE_CHECKING:
    from pymux.main import Pymux

logger = logging.getLogger(__name__)

__all__ = ["PymuxKeyBindings"]

#: One binding: the keys it answers to, and whether the prefix comes
#: first. The keys are prompt_toolkit's, so every spelling of one key
#: gives the same one.
_ABinding = Tuple[bool, Tuple[str, ...]]


class PymuxKeyBindings:
    """
    Pymux key binding manager.
    """

    def __init__(self, pymux: "Pymux") -> None:
        self.pymux = pymux

        self.custom_key_bindings = KeyBindings()

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

        @kb.add(Keys.Any, filter=has_prefix)
        def _(event: E) -> None:
            "Ignore unknown Ctrl-B prefixed key sequences."
            pymux.get_client_state().has_prefix = False

        @kb.add("escape", filter=prompt_or_command_focus & ~has_prefix & emacs_mode)
        @kb.add("c-c", filter=prompt_or_command_focus & ~has_prefix)
        @kb.add("c-g", filter=prompt_or_command_focus & ~has_prefix)
        #        @kb.add('backspace', filter=has_focus(COMMAND) & ~has_prefix &
        #                              Condition(lambda: cli.buffers[COMMAND].text == ''))
        def _leave_command_mode(event: E) -> None:
            """
            Leave command mode.

            **Escape only outside vi mode.** `status-keys vi` gives
            Escape to vi, where it leaves insert mode and the command
            line stays open. tmux draws the same line: Escape cancels
            with emacs status keys and goes to normal mode with vi
            ones. Lillecarl/pymux#157.

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

            command = client_state.confirm_command
            client_state.confirm_command = None
            client_state.confirm_text = None

            pymux.handle_command(command)

        @kb.add("n", filter=waits_for_confirmation)
        @kb.add("N", filter=waits_for_confirmation)
        @kb.add("c-c", filter=waits_for_confirmation)
        def _cancel(event: E) -> None:
            """
            Cancel command.
            """
            client_state = pymux.get_client_state()
            client_state.confirm_command = None
            client_state.confirm_text = None

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

        # The chooser of windows. It has the focus while it shows,
        # which is what keeps the pane from taking these keys; the
        # same reason the pop-up's `q` above answers. tmux's tree
        # answers the mode keys, and so does this: j and k with the
        # arrows, Enter to switch, q and Escape to leave. The search
        # of `/` takes the focus; while it has it, everything types
        # into it, the list narrows as it does, and Escape brings the
        # keys back. Lillecarl/pymux#295.
        @Condition
        def chooser_displayed() -> bool:
            state = self.pymux.get_client_state()
            return state.choose_window or state.choose_buffer

        @Condition
        def chooser_search_focused() -> bool:
            state = self.pymux.get_client_state()
            return (state.choose_window or state.choose_buffer) and has_focus(
                state.choose_window_filter
            )()

        @kb.add("/", filter=chooser_displayed & ~chooser_search_focused)
        def _chooser_search(event: E) -> None:
            "Search the names; what is typed narrows the list."
            state = self.pymux.get_client_state()
            manager = state.layout_manager
            if state.choose_buffer:
                get_app().layout.focus(manager._choose_buffer_search)
            else:
                get_app().layout.focus(manager._choose_window_search)

        @kb.add("up", filter=chooser_displayed)
        @kb.add("k", filter=chooser_displayed)
        @kb.add("c-p", filter=chooser_displayed)
        def _chooser_up(event: E) -> None:
            "The row above, staying at the first."
            state = self.pymux.get_client_state()
            state.choose_window_index = max(0, state.choose_window_index - 1)

        @kb.add("down", filter=chooser_displayed)
        @kb.add("j", filter=chooser_displayed)
        @kb.add("c-n", filter=chooser_displayed)
        def _chooser_down(event: E) -> None:
            "The row below, staying at the last."
            state = self.pymux.get_client_state()
            matches = state.layout_manager.chooser_matches()
            state.choose_window_index = min(
                len(matches) - 1,
                state.choose_window_index + 1,
            )

        @kb.add("enter", filter=chooser_displayed & ~chooser_search_focused)
        def _chooser_choose(event: E) -> None:
            "Take the row the chooser points at."
            state = self.pymux.get_client_state()
            if state.choose_buffer:
                state.layout_manager.choose_the_pointed_buffer()
            else:
                state.layout_manager.choose_the_pointed_window()

        @kb.add("q", filter=chooser_displayed & ~chooser_search_focused, eager=True)
        @kb.add(
            "escape", filter=chooser_displayed & ~chooser_search_focused, eager=True
        )
        @kb.add("c-c", filter=chooser_displayed & ~chooser_search_focused, eager=True)
        def _quit_chooser(event: E) -> None:
            "Leave the chooser without taking anything."
            state = self.pymux.get_client_state()
            state.choose_window = False
            state.choose_buffer = False

        @kb.add("escape", filter=chooser_search_focused, eager=True)
        def _quit_chooser_search(event: E) -> None:
            "Leave the search, keeping the chooser."
            state = self.pymux.get_client_state()
            state.choose_window_filter.reset()
            manager = state.layout_manager
            if state.choose_buffer:
                get_app().layout.focus(manager._choose_buffer_rows)
            else:
                get_app().layout.focus(manager._choose_window_rows)

        @kb.add(Keys.KeyRelease, eager=True)
        def _forward_a_key_release(event: E) -> None:
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

        @kb.add(Keys.Any, eager=True, filter=display_pane_numbers)
        def _hide_numbers(event: E) -> None:
            "When the pane numbers are shown. Any key press should hide them."
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
        self, key_name: str, command: str, arguments: list, needs_prefix=False
    ) -> None:
        """
        Add custom binding (for the "bind-key" command.)
        Raises ValueError if the give `key_name` is an invalid name.

        :param key_name: Pymux key name, for instance "C-a", "M-x" or
            "ctrl+home".
        """
        # Translate the name into a prompt_toolkit key sequence, in
        # either spelling. (Can raise ValueError.)
        keys_sequence = key_however_it_is_written(key_name)

        # Unbind the key, under whichever name it was bound.
        self.remove_custom_binding(key_name, needs_prefix=needs_prefix)

        # Create handler and add to Registry.
        filter: Filter
        if needs_prefix:
            filter = HasPrefix(self.pymux)
        else:
            filter = ~HasPrefix(self.pymux)

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
            call_command_handler(command, self.pymux, arguments)
            client_state.has_prefix = False

        self.custom_key_bindings.add(*keys_sequence, filter=filter)(key_handler)

        self.custom_bindings[needs_prefix, keys_sequence] = CustomBinding(
            key_handler, command, arguments, key_name
        )

    def keys_a_prefix_leads_to(self) -> "list[tuple[str, str]]":
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
        for (needs_prefix, _keys), binding in self.custom_bindings.items():
            if not needs_prefix:
                continue
            meaning = binding.command
            if binding.arguments:
                meaning += " " + " ".join(map(wrap_argument, binding.arguments))
            rows.append((binding.written, meaning))
        return sorted(rows)

    def binding_on(
        self, key_name: str, needs_prefix: bool = False
    ) -> "CustomBinding | None":
        """
        What a key runs, under any name for that key, or None.

        Raises `ValueError` when the name reads as no key at all.
        """
        return self.custom_bindings.get(
            (needs_prefix, key_however_it_is_written(key_name))
        )

    def remove_custom_binding(self, key_name: str, needs_prefix: bool = False) -> None:
        """
        Remove the binding on a key, under any name for that key.

        Raises `ValueError` when the name reads as no key at all.

        :param key_name: Pymux key name, for instance "C-A".
        """
        k = (needs_prefix, key_however_it_is_written(key_name))

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
