"""
Where a person types: the ":" command line and the answer to a
`command-prompt`, each drawn as a bar at the bottom or as a box in the
middle of the screen, and the mode tag in front of them. Each window is
built once and kept: the layout focuses a control, and a fresh one is
a control it never focused. Lillecarl/pymux#158, Lillecarl/pymux#220.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from prompt_toolkit.application import get_app
from prompt_toolkit.enums import EditingMode
from prompt_toolkit.filters import (
    has_completions,
    is_done,
)
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Container,
    HSplit,
    ScrollOffsets,
    Window,
    WindowAlign,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension as D
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenuControl
from prompt_toolkit.layout.processors import (
    AppendAutoSuggestion,
    BeforeInput,
    HighlightSelectionProcessor,
    ShowArg,
)

from ..enums import EditingKeys
from .z_index import Z_INDEX

if TYPE_CHECKING:
    from .layout import LayoutManager

__all__ = ["CommandLine"]


class CommandLine:
    """
    The command line and the prompt of one client's layout.
    """

    def __init__(self, manager: LayoutManager) -> None:
        self.manager = manager

        # The window a person types a command into, and the box that
        # holds it when the palette is on. Both are built once and kept.
        # A fresh `BufferControl` on every render is one that the layout
        # never focused, so the cursor is drawn nowhere and the person
        # cannot see where they are typing. Lillecarl/pymux#158.
        self._window: Window | None = None
        self._palette: Container | None = None
        # And the same two for the prompt: the line a person answers a
        # question on, and the box that holds it when the question
        # knows its answers. Lillecarl/pymux#220.
        self._prompt_window: Window | None = None
        self._key_box: Container | None = None

    def mode_tokens(self) -> StyleAndTextTuples:
        """
        Which vi mode the line a person types on is in.

        Nothing with emacs status keys: there is one mode there, and a
        tag that never changes says nothing.

        **With vi status keys the mode decides what a key does**, and
        Escape most of all: the first one leaves insert mode and the
        second closes the line. A person has to be able to see which
        press they are about to make. Lillecarl/pymux#255.

        The tag is one width whatever it says, so the line a person is
        typing on never moves under them.
        """
        if self.manager.client_state.session.options.status_keys is EditingKeys.HELIX:
            return self._helix_mode_tokens()
        if self.manager.client_state.session.options.status_keys is not EditingKeys.VI:
            return []

        app = get_app()
        if app.editing_mode is not EditingMode.VI:
            return []

        mode = app.vi_state.input_mode
        if mode in (InputMode.REPLACE, InputMode.REPLACE_SINGLE):
            name = "REPLACE"
        elif mode is InputMode.NAVIGATION:
            name = "NORMAL"
        else:
            name = "INSERT"

        return [("class:commandline.mode", " %-7s " % (name,))]

    def _helix_mode_tokens(self) -> StyleAndTextTuples:
        """
        Helix's mode, as Helix spells it, and what is half typed: a
        count, a menu key, or the regex `s` reads, which shows nowhere
        else. The mode is one width; the half-typed part grows only
        while it is typed. Lillecarl/pymux#749.
        """
        helix = self.manager.pymux.key_bindings_manager.helix
        buffer = get_app().current_buffer
        tokens: StyleAndTextTuples = [("class:commandline.mode", " %s " % (helix.mode_of(buffer).label,))]
        pending = helix.pending_of(buffer)
        if pending:
            tokens.append(("class:commandline.pending", " %s " % (pending,)))
        return tokens

    def before_command_tokens(self) -> StyleAndTextTuples:
        'What stands before what a person types after ":".'
        return [*self.mode_tokens(), ("class:commandline-prompt", ":")]

    def before_prompt_tokens(self) -> StyleAndTextTuples:
        if self.manager.client_state.prompt_completer is not None:
            # The box says what it is asking for, on the row above the
            # line. Saying it twice is one row wasted and one thing to
            # read. Lillecarl/pymux#220.
            return []
        return [*self.mode_tokens(), ("class:commandline.prompt", "%s " % (self.manager.client_state.prompt_text,))]

    def window(self) -> Window:
        """
        The window that holds what a person types after ":".

        The bar at the bottom and the palette in the middle draw the
        same window. Only where it is drawn differs, so the key
        bindings of command mode and everything that reads the buffer
        stay as they were. Lillecarl/pymux#158.

        **It is built once.** The layout focuses a control, and a fresh
        one on every render is one it never focused: the cursor lands
        nowhere and a person cannot see where they are typing. Only one
        of the two places draws it, because the filter of each reads
        the option.
        """
        if self._window is not None:
            return self._window

        self._window = Window(
            # Can be more if the command is multiline.
            height=D(min=1),
            style="class:commandline",
            dont_extend_height=True,
            content=BufferControl(
                buffer=self.manager.client_state.command_buffer,
                preview_search=True,
                input_processors=[
                    AppendAutoSuggestion(),
                    BeforeInput(self.before_command_tokens),
                    ShowArg(),
                    HighlightSelectionProcessor(),
                    # Paints nothing unless the status keys are Helix's.
                    self.manager.pymux.key_bindings_manager.helix.processor(),
                ],
            ),
            z_index=Z_INDEX.COMMAND_LINE,
        )
        return self._window

    def prompt_window(self) -> Container:
        """
        The window that holds the answer to a `command-prompt`.

        The bar at the bottom and the box in the middle draw the same
        window, for the same reason the ":" line does, and it is built
        once for the same reason as well.
        """
        if self._prompt_window is not None:
            return self._prompt_window

        self._prompt_window = Window(
            height=1,
            style="class:commandline",
            content=BufferControl(
                buffer=self.manager.client_state.prompt_buffer,
                input_processors=[
                    BeforeInput(self.before_prompt_tokens),
                    AppendAutoSuggestion(),
                    HighlightSelectionProcessor(),
                    self.manager.pymux.key_bindings_manager.helix.processor(),
                ],
            ),
            z_index=Z_INDEX.COMMAND_LINE,
        )
        return self._prompt_window

    def palette(self) -> Container:
        """
        The ":" command line as a box in the middle of the screen.

        **The completions are inside the box.** The menu that a bottom
        bar uses hangs off the cursor and stops at twelve rows, so a
        palette that only moved the text would be a nicer box around
        the same thin content. In here the menu takes the height of the
        box, which is what makes the room worth having.

        A title row says what the box is, the way the overlay pane says
        what it holds. tmux has no such thing; this is the room that
        Lillecarl/pymux#147, #148 and #48 are asking for.

        Built once, for the reason `CommandLine.window` gives.
        """
        if self._palette is not None:
            return self._palette

        self._palette = self._box(lambda: " Command ", self.window())
        return self._palette

    def _box(self, title: Callable[[], str], window: Container) -> Container:
        """
        A box in the middle of the screen: a title, a line to type in,
        and the completions filling what is left.

        Two things draw one. The ":" command line does, when a person
        asked for a palette; and a prompt that knows its answers does,
        which today is the box that composes a key.
        Lillecarl/pymux#220.

        `title` is asked each frame, so a box can say what it is asking
        for rather than what kind of box it is.
        """

        def get_title() -> StyleAndTextTuples:
            return [("class:commandpalette.title", title())]

        return HSplit(
            [
                Window(
                    height=1,
                    align=WindowAlign.CENTER,
                    content=FormattedTextControl(get_title),
                    style="class:commandpalette.titlebar",
                ),
                window,
                self._completions(),
            ],
            style="class:commandpalette",
        )

    def key_box(self) -> Container:
        """
        The prompt as a box, for a question that knows its answers.

        The line inside it is the same `prompt_buffer` the bottom
        toolbar uses. Only the room around it is different, and the
        room is what the completions need: a person composing
        "ctrl+shift+f5" is reading the list, not typing from memory.

        Built once, for the reason `CommandLine.window` gives.
        """
        if self._key_box is not None:
            return self._key_box

        self._key_box = self._box(
            lambda: " %s " % (self.manager.client_state.prompt_text or "Key",),
            self.prompt_window(),
        )
        return self._key_box

    def _completions(self) -> Container:
        """
        The completions of the palette, filling the box.

        This is `CompletionsMenu` with two of its numbers changed, so
        it is written out rather than wrapped. That widget draws for a
        menu that hangs under the cursor: as narrow as its longest
        completion, and twelve rows at the most. Both are wrong in a
        box. The box has a width to fill, which is where a completion
        and what it means will sit side by side, and it has to stop
        above the status line rather than run off the screen.
        """
        return ConditionalContainer(
            content=Window(
                content=CompletionsMenuControl(),
                height=lambda: D(min=1, max=self.manager.palette_rows()),
                scroll_offsets=ScrollOffsets(top=1, bottom=1),
                right_margins=[ScrollbarMargin(display_arrows=True)],
                style="class:completion-menu",
                z_index=Z_INDEX.POPUP,
            ),
            filter=has_completions & ~is_done,
        )
