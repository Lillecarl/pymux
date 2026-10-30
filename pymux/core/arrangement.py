"""
Arrangement of panes.

Don't confuse with the prompt_toolkit VSplit/HSplit classes. This is a higher
level abstraction of the Pymux window layout.

An arrangement consists of a list of windows. And a window has a list of panes,
arranged by ordering them in HSplit/VSplit instances.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, ClassVar
from weakref import WeakKeyDictionary

from prompt_toolkit.application import Application, DummyApplication, get_app, get_app_or_none, set_app
from pyte.keep import Keep

from ..ids import PaneId, WindowIndex
from ..options.session_options import SessionOptions
from .window_tree import Pane, Window, node_key

if TYPE_CHECKING:
    from .session import OwnOptions

__all__ = ["Arrangement"]


class Arrangement:
    """
    Arrangement class for one Pymux session.
    This contains the list of windows and the layout of the panes for each
    window. All the clients share the same Arrangement instance, but they can
    have different windows active.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: The two per-client maps are keyed by `Application`, which no
    #: snapshot can hold: they are saved by client and window id, and
    #: a client that reattaches gets its windows back.
    KEEP: ClassVar[dict[str, Keep]] = {
        "windows": Keep.SAVED,
        "_unlinked_windows": Keep.SAVED,
        # The session's options, which the session saves.
        "options": Keep.REBUILT,
        "window_defaults": Keep.SAVED,
        "_last_active_window": Keep.SAVED,
        "_active_window_for_cli": Keep.SAVED,
        # Its newest window, which is what `last-window` reaches.
        "_window_history_for_cli": Keep.SAVED,
    }

    @property
    def base_index(self) -> int:
        return self.options.base_index

    @base_index.setter
    def base_index(self, value: int) -> None:
        self.options.base_index = value

    @property
    def renumber_windows(self) -> bool:
        return self.options.renumber_windows

    @renumber_windows.setter
    def renumber_windows(self, value: bool) -> None:
        self.options.renumber_windows = value

    def __init__(self, options: SessionOptions | OwnOptions | None = None) -> None:
        self.windows: list[Window] = []

        #: Where `base_index` and `renumber_windows` live: the session's
        #: options, so that `set-option -g` reaches a session that has
        #: no value of its own. Lillecarl/pymux#756.
        # An arrangement no session holds reads the declared defaults.
        self.options = options if options is not None else SessionOptions()

        # The windows that link-window can put back. An unlinked
        # window keeps its panes running; it is only out of the order
        # nobody sees. Lillecarl/pymux#297.
        self._unlinked_windows: list[Window] = []

        # What a new window starts with, by the attribute the option
        # writes. tmux calls these the global window options and
        # `set-window-option -g` is how they are set.
        #
        # A window option belongs to one window, and a configuration
        # file is read before there is a window, so without this there
        # was no way for a configuration file to say what a window
        # should be. Lillecarl/pymux#199.
        #
        # Like tmux, setting one changes no window that is already
        # open. It says what the next one starts as.
        self.window_defaults: dict[str, object] = {}

        self._active_window_for_cli: WeakKeyDictionary[Application[Any], Window] = WeakKeyDictionary()
        # The windows each client left, the newest last. Closing its
        # window returns it to the newest one still open, as tmux's
        # `session_last` does, and the next index only when there is
        # none. Lillecarl/pymux#658.
        self._window_history_for_cli: WeakKeyDictionary[Application[Any], list[Window]] = WeakKeyDictionary()

        # The active window of the last client. Used as default when a new session
        # is attached.
        self._last_active_window: Window | None = None

    def invalidation_hash(self) -> tuple[object, ...]:
        """
        When this changes, the layout needs to be rebuild.
        """
        w = self.get_active_window()
        if w is None:
            return ()

        return w.invalidation_hash()

    def get_active_window(self) -> Window | None:
        """
        The current active :class:`.Window`, or nothing when this
        session holds none.
        """
        return self.get_active_window_for(get_app())

    def get_active_window_for(self, app: Application[Any]) -> Window | None:
        """
        The active :class:`.Window` of one client.

        Every client looks at a window of its own. `get_app()` names the
        client that asks, which is right for a key binding and wrong
        during a render: the renderer of one client may run while
        another client is the current one. A caller that already holds
        the application passes it here instead.

        A client that has not looked yet lands on the window the session
        was last on. It cached that window and returned `windows[0]`, so
        the first answer and every answer after it differed as soon as
        anything had made a window active. Lillecarl/pymux#193.

        **A session that holds no window has no answer, and says so.**
        It answered `windows[0]` of an empty list, which is an
        `IndexError` out of whatever asked -- and an `IndexError` is
        not a `CommandException`, so a bare `resize-window -x 200` over
        the socket reached nobody at all. `get_active_pane_for` already
        reads this as optional. Lillecarl/pymux#473.
        """
        if not self.windows:
            return None

        try:
            return self._active_window_for_cli[app]
        except KeyError:
            # The last active window can be gone. `remove_pane` moves a
            # client off a window it empties, and a window with no
            # client on it is emptied with nobody to move.
            window = self._last_active_window
            if window is None or window not in self.windows:
                window = self.windows[0]

            self._active_window_for_cli[app] = window
            # `get_app()` with no client current answers prompt_toolkit's
            # stand-in, and nobody looks through that: an invalidate on
            # a pane's callback cleared the marks it had just raised.
            # Lillecarl/pymux#767.
            if not isinstance(app, DummyApplication):
                window.seen()
            return window

    def set_active_window(self, window: Window) -> None:
        app = get_app()

        previous = self.get_active_window()
        history = self._window_history_for_cli.setdefault(app, [])
        if previous is not None and previous is not window:
            if previous in history:
                history.remove(previous)
            history.append(previous)
        if window in history:
            history.remove(window)
        self._active_window_for_cli[app] = window
        self._last_active_window = window
        window.seen()

    def ring(self, window: Window, pane: Pane, apps: Iterable[Application[Any]]) -> bool:
        """
        Raise the bell flags for a bell of `pane` that nobody saw ring.
        True when a flag went up, so a status line has something new.

        A window is seen when one of `apps`, the clients attached now,
        has it as its window, and a pane when it is also the active pane
        there. A detached client keeps its window for when it comes back,
        and sees nothing meanwhile. tmux keeps the window's flag alone;
        the pane's says which pane of a split it was.
        """
        if not window.options.monitor_bell:
            return False
        watched = any(self._active_window_for_cli.get(app) is window for app in apps)
        raised = False
        if not watched and not window.bell:
            window.bell = raised = True
        if (not watched or pane is not window.active_pane) and not pane.bell:
            pane.bell = raised = True
        return raised

    def watched(self, window: Window, apps: Iterable[Application[Any]]) -> bool:
        return any(self._active_window_for_cli.get(app) is window for app in apps)

    def notify(self, window: Window, apps: Iterable[Application[Any]]) -> bool:
        "Mark a window a notification came from, unless a client looks at it. True when the mark went up."
        if not window.options.monitor_notification or window.notified or self.watched(window, apps):
            return False
        window.notified = True
        return True

    def check_output(self, apps: Iterable[Application[Any]], now: float) -> bool:
        "`Window.check_output` for every window. True when a mark went up."
        raised = False
        for window in self.windows:
            raised |= window.check_output(self.watched(window, apps), now)
        return raised

    def set_active_window_from_pane_id(self, pane_id: PaneId) -> None:
        """
        Make the window with this pane ID the active Window.
        """
        for w in self.windows:
            for p in w.panes:
                if p.pane_id == pane_id:
                    self.set_active_window(w)

    def get_previous_active_window(self) -> Window | None:
        "The previous active Window or None if unknown."
        return self.previous_window_for(get_app())

    def previous_window_for(self, app: Application[Any]) -> Window | None:
        "The newest window that `app` left and that is still in the order."
        for window in reversed(self._window_history_for_cli.get(app, ())):
            if window in self.windows:
                return window
        return None

    def get_window_by_index(self, index: WindowIndex):
        "Return the Window with this index or None if not found."
        for w in self.windows:
            if w.index == index:
                return w

    def lowest_free_index(self) -> WindowIndex:
        "The first index from `base_index` up that no window has."
        taken = {w.index for w in self.windows}
        index = self.base_index
        while index in taken:
            index += 1
        return WindowIndex(index)

    def renumber(self) -> None:
        """
        Close the gaps in the window order: the windows keep their
        order and are relabelled from `base_index` up.

        **Nothing moves and nobody's focus changes.** A client holds a
        `Window` object and not an index (`_active_window_for_cli`),
        so a relabel cannot unsettle anybody. What changes is what
        `select-window -t 3` reaches and what the status line prints.

        tmux does the same walk in index order
        (`session_renumber_windows`, `session.c:740`).

        **The unlinked windows keep their indices.** One that
        `unlink-window` parked is out of the order nobody sees, and
        `link-window` puts it back where it was; packing an order that
        is not shown would move a window a person never closed.
        Lillecarl/pymux#297, Lillecarl/pymux#342.
        """
        for number, window in enumerate(sorted(self.windows, key=lambda one: one.index), start=self.base_index):
            window.index = WindowIndex(number)

    def make_room_at(self, index: WindowIndex) -> None:
        """
        Free one index by moving the windows at and above it up.

        Only the run that is in the way moves. The windows are walked
        from `index` up while each one is there, and the first gap
        stops the walk: a session numbered 1, 2, 3, 7 makes room at 2
        by moving 2 and 3, and 7 stays where a person left it.

        tmux says the same about its own "-a": "the new window is
        inserted at the next index up from the specified target
        window, moving windows up if necessary".
        Lillecarl/pymux#191.
        """
        by_index = {w.index: w for w in self.windows}

        displaced = []
        while index in by_index:
            displaced.append(by_index[index])
            index += 1

        # From the top down, so no window lands on one that has not
        # moved yet.
        for window in reversed(displaced):
            window.index += 1

    def create_window(
        self,
        pane: Pane,
        name: str | None = None,
        set_active: bool = True,
        index: WindowIndex | None = None,
    ) -> Window:
        """
        Create a new window that contains just this pane, and answer it.

        :param pane: The :class:`.Pane` instance to put in the new window.
        :param name: If given, name for the new window.
        :param set_active: When True, focus the new window.
        :param index: Where to put it. Without one it takes the lowest
            free index, which is what a session with no gaps in it
            gives anyway.
        """
        if index is None:
            index = self.lowest_free_index()
        else:
            self.make_room_at(index)

        # Create new window and add it.
        w = Window(index)
        w.add_pane(pane)

        # What a `set-window-option -g` asked every new window to be.
        # After the pane, so that a default which reshapes the window
        # around what is in it sees the pane. `strip` is that.
        for attribute, value in self.window_defaults.items():
            setattr(w.options, attribute, value)

        self.windows.append(w)

        # Sort windows by index.
        self.windows = sorted(self.windows, key=lambda w: w.index)

        app = get_app_or_none()

        if app is not None and set_active:
            self.set_active_window(w)

        if name is not None:
            w.chosen_name = name

        assert w.active_pane == pane
        assert w._get_parent(pane)
        return w

    def move_window(self, window: Window, new_index: WindowIndex) -> None:
        """
        Move window to a new index.
        """
        window.index = new_index

        # Sort windows by index.
        self.windows = sorted(self.windows, key=lambda w: w.index)

    def move_window_to(self, window: Window, dst: Arrangement, index: WindowIndex | None = None) -> None:
        """
        Move a window of this order into another arrangement's.

        The window leaves this order the way `unlink_window` takes
        one out -- the clients looking at it are focused away first
        -- and lands in the other's the way `link_window` puts one
        in, at the index or after the last window without one. The
        pen in between never escapes this method: the window is out
        of one order and in the other when it returns.
        Lillecarl/pymux#533.

        A window that leaves closes its gap under `renumber-windows`,
        as one that closes does: to this order it is gone. tmux
        renumbers the source after a move too (`cmd-move-window.c:116`).
        """
        self.unlink_window(window)
        self._unlinked_windows.remove(window)
        dst.link_window(window, index)
        if self.renumber_windows:
            self.renumber()

    def replace_pane(self, old: Pane, new: Pane) -> None:
        """
        Put a new pane where an old one sat, in the tree and in the
        focus. The place and the weights stay; the id and the screen
        belong to the program, so they go with it.
        Lillecarl/pymux#306.
        """
        for window in self.windows:
            for split in window.splits:
                if old in split:
                    split[split.index(old)] = new
                    if node_key(old) in split.weights:
                        split.set_weight(new, split.weights.pop(node_key(old)))
                    break

            if node_key(old) in window.column_widths:
                window.set_column_width(new, window.column_widths.pop(node_key(old)))

            if window._active_pane is old:
                window._active_pane = new

    def swap_window(self, first: Window, second: Window) -> None:
        """
        Trade the indexes of two windows. Lillecarl/pymux#296.
        """
        first.index, second.index = second.index, first.index

        # Sort windows by index.
        self.windows = sorted(self.windows, key=lambda w: w.index)

    def move_pane_to_window(self, pane: Pane, window: Window, vsplit: bool = False) -> None:
        """
        Take a pane out of the window that holds it, and add it to
        another one, beside or above the pane that is focused there.
        A window the pane leaves empty is gone, the way a pane whose
        program ends leaves it. Lillecarl/pymux#297.
        """
        self.remove_pane(pane)
        window.add_pane(pane, vsplit=vsplit)

    def unlink_window(self, window: Window) -> None:
        """
        Take a window out of the order, with its panes still running.
        link_window puts it back. Lillecarl/pymux#297.
        """
        # Focus away first, while the window is still in the order to
        # be counted from.
        for app, active_w in self._active_window_for_cli.items():
            if window == active_w and self.windows:
                with set_app(app):
                    self.focus_next_window()

        self.windows.remove(window)
        self._unlinked_windows.append(window)

    def link_window(self, window: Window, index: WindowIndex | None = None) -> None:
        """
        Put a window in the order at an index: one that unlink_window
        took out, one that is in the order already, which is a move,
        or one that belongs to no order, which `move_window_to`
        unlinked from another arrangement. Lillecarl/pymux#297,
        Lillecarl/pymux#533.
        """
        if window in self._unlinked_windows:
            self._unlinked_windows.remove(window)
            take = True
        elif window not in self.windows:
            # A window that belongs to no order: `move_window_to`
            # unlinked it from another arrangement, and it arrives to
            # be put in this one.
            take = True
        else:
            take = False

        if take:
            if index is None:
                index = max((w.index for w in self.windows), default=WindowIndex(self.base_index - 1)) + 1
            window.index = index
            self.windows.append(window)

        if index is not None:
            self.move_window(window, index)

        self.set_active_window(window)

    def get_active_pane(self) -> Pane | None:
        """
        The current :class:`.Pane` from the current window.
        """
        return self.get_active_pane_for(get_app())

    def get_active_pane_for(self, app: Application[Any]) -> Pane | None:
        "The active :class:`.Pane` of one client. See `get_active_window_for`."
        w = self.get_active_window_for(app)
        if w is not None:
            return w.active_pane
        return None

    def remove_pane(self, pane: Pane) -> None:
        """
        Remove a :class:`.Pane`. (Look in all windows.)
        """
        closed = False

        # **A copy, because the loop takes windows out of the list.**
        # Walking the list itself skipped the window after each one it
        # removed, and that window is the one this sweep is for: an
        # empty window right behind a closing one stayed in the order
        # for ever. Lillecarl/pymux#351.
        for w in list(self.windows):
            w.remove_pane(pane)

            # No panes left in this window?
            if not w.has_panes:
                for app, active_w in list(self._active_window_for_cli.items()):
                    if w == active_w:
                        with set_app(app):
                            back = self.previous_window_for(app)
                            if back is not None and back is not w:
                                self.set_active_window(back)
                            else:
                                self.focus_next_window()

                self.windows.remove(w)
                for history in self._window_history_for_cli.values():
                    if w in history:
                        history.remove(w)
                closed = True

        # A window closing is what `renumber-windows` is about, or one
        # leaving for another session (`move_window_to`). A `move-window`
        # inside the session that opens a gap leaves it. Lillecarl/pymux#342.
        if closed and self.renumber_windows:
            self.renumber()

    def focus_previous_window(self) -> None:
        w = self.get_active_window()
        assert w is not None

        self.set_active_window(self.windows[(self.windows.index(w) - 1) % len(self.windows)])

    def focus_next_window(self) -> None:
        w = self.get_active_window()
        assert w is not None

        self.set_active_window(self.windows[(self.windows.index(w) + 1) % len(self.windows)])

    def break_pane(self, set_active: bool = True) -> None:
        """
        When the current window has multiple panes, remove the pane from this
        window and put it in a new window.

        :param set_active: When True, focus the new window.
        """
        w = self.get_active_window()
        assert w is not None

        if len(w.panes) > 1:
            pane = w.active_pane
            if pane is not None:
                w.remove_pane(pane)
                self.create_window(pane, set_active=set_active)

    def rotate_window(self, count: int = 1) -> None:
        "Rotate the panes in the active window."
        w = self.get_active_window()
        assert w is not None
        w.rotate(count=count)

    @property
    def has_panes(self) -> bool:
        "True when any of the windows has a :class:`.Pane`."
        for w in self.windows:
            if w.has_panes:
                return True
        return False
