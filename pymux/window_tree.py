"""
One window: a tree of splits whose leaves are panes, the layouts that
reshape it, and the strip of columns a window can scroll through.
`arrangement.py` holds a session's windows; this holds what is inside
one.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Iterable
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar
from weakref import ref

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Size
from ptterm import Terminal
from pyte.keep import Keep

from .downloads import DownloadReader
from .ids import PaneId, PaneIndex, WindowId, WindowIndex
from .osc import NO_PROGRESS
from .window_options import WindowOptions

if TYPE_CHECKING:
    from .recording import Recording
    from .tags import Tags
    from .window_pane import WindowWidget

__all__ = [
    "DEFAULT_COLUMN_WIDTH",
    "PRESET_COLUMN_WIDTHS",
    "HSplit",
    "LayoutTypes",
    "Node",
    "NodeKey",
    "Pane",
    "VSplit",
    "Window",
    "node_key",
    "panes_of",
]


#: How wide a column of a strip can be, as a fraction of the window.
#: The first three are niri's own presets, and a key cycles between
#: them.
#:
#: **The whole window is the fourth**, because a person working in one
#: pane wants it wide and `resize-pane -Z` is the wrong answer: zoom
#: leaves the row, and a strip is for staying in it with the
#: neighbours one scroll away. It is not a special case either -- the
#: view already shows one column and part of the next when a column is
#: wider than it fits. Lillecarl/pymux#215.
PRESET_COLUMN_WIDTHS = (1 / 3, 1 / 2, 2 / 3, 1.0)


#: What a column takes until somebody says otherwise. niri's default
#: as well, and the reason a strip of two columns exactly fills the
#: screen while a third one pushes past the edge.
DEFAULT_COLUMN_WIDTH = 1 / 2


class LayoutTypes(Enum):
    # The values are in lowercase with dashes, because that is what users can
    # use at the command line.
    EVEN_HORIZONTAL = "even-horizontal"
    EVEN_VERTICAL = "even-vertical"
    MAIN_HORIZONTAL = "main-horizontal"
    MAIN_VERTICAL = "main-vertical"
    TILED = "tiled"


class Pane:
    """
    One pane, containing one process and a search buffer for going into copy
    mode or displaying the help.
    """

    _pane_counter = 1000  # Start at 1000, to be sure to not confuse this with pane indexes.

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    #: The counters of panes, windows and splits are class attributes,
    #: so no instance walk sees them: a snapshot saves them too, or a
    #: new id collides with one that survived. Lillecarl/pymux#399.
    KEEP: ClassVar[dict[str, Keep]] = {
        "pane_id": Keep.SAVED,
        "terminal": Keep.SAVED,
        "chosen_name": Keep.SAVED,
        "clock_mode": Keep.SAVED,
        "user_vars": Keep.SAVED,
        "tags": Keep.SAVED,
        "current_directory": Keep.SAVED,
        "current_host": Keep.SAVED,
        "current_user": Keep.SAVED,
        "shell": Keep.SAVED,
        "shell_integration_version": Keep.SAVED,
        # A program says its progress again as it moves.
        "progress": Keep.DROPPED,
        "command_zone": Keep.SAVED,
        "last_exit_status": Keep.SAVED,
        "bell": Keep.SAVED,
        # `#{pane_revision}` only grows: an agent waiting on a change
        # holds a number from before the upgrade.
        "revision": Keep.SAVED,
        # An upgrade writes it out first: `upgrade.upgrade`.
        "recording": Keep.DROPPED,
        # A file half sent is lost; its sender sends it again.
        "downloads": Keep.DROPPED,
    }

    def __init__(self, terminal: Terminal | WindowWidget) -> None:
        self.terminal = terminal
        self.chosen_name: str | None = None

        # Displayed the clock instead of this pane content.
        self.clock_mode = False

        # Give unique ID.
        Pane._pane_counter += 1
        self.pane_id: PaneId = PaneId(Pane._pane_counter)

        #: How many times anything this pane shows may have changed. It
        #: only goes up, and `#{pane_revision}` is it.
        #:
        #: **`PaneMaker.create_pane` is what makes it move**, because it is
        #: what builds the terminal that says so. A pane built around
        #: anything else -- a dozen tests build one around a stub -- has
        #: the field and never counts, which is what lets those tests go
        #: on knowing nothing about a widget.
        #:
        #: **Compare it, never order it.** Two revisions that differ say
        #: something may have changed; a difference of one says nothing
        #: about how much. The count moves on output that draws nothing
        #: at all, because `ptyhost` calls `invalidate` after every read
        #: whatever the bytes were, and that is the safe direction: a
        #: reader that redraws for nothing loses a frame, and one that
        #: misses a change shows the wrong screen until the next one.
        #:
        #: **It is not `screen.writes`.** That counts writes to rows, so
        #: it stands still through a DECSCNM, an "OSC 4" and the death of
        #: the program -- each of which changes what a reader draws. A
        #: reader inside the server compares those itself, the way
        #: `ptterm` compares reverse video; a reader that has only this
        #: number cannot. Lillecarl/pymux#387.
        self.revision = 0

        #: Where the program says it is, from "OSC 7" or "OSC 1337 ;
        #: CurrentDir". A shell reports it on every prompt, and nothing
        #: else sets it. `None` is a pane nobody asked about yet.
        self.current_directory: str | None = None

        #: The machine the directory sits on, from the host of the
        #: "OSC 7" file URL. `None` is the machine here or a report
        #: that named no host. "OSC 1337 ; RemoteHost" sets it too.
        self.current_host: str | None = None

        #: The user the shell runs as there, from "OSC 1337 ; RemoteHost".
        self.current_user: str | None = None

        #: The shell and the version of its iTerm2 integration, from
        #: "OSC 1337 ; ShellIntegrationVersion". Lillecarl/pymux#784.
        self.shell: str | None = None
        self.shell_integration_version: str | None = None

        #: What the program says of its progress, from "OSC 9 ; 4".
        #: Lillecarl/pymux#788.
        self.progress = NO_PROGRESS

        #: What the shell published about itself, from "OSC 1337 ;
        #: SetUserVar". iTerm2 reads these for badges and triggers and
        #: WezTerm for titles; here they wait for a reader, the way the
        #: notification hub waited for its chooser.
        self.user_vars: dict[str, str] = {}

        #: What a caller said about this pane, with `set-tag`.
        self.tags: Tags = {}

        #: The shell integration zone the program stands in, from
        #: "OSC 133": "A" a prompt, "B" a command, "C" its output.
        #: `None` is outside every zone or after the command ended.
        self.command_zone: str | None = None

        #: The exit status the last command finished with, from
        #: "OSC 133 ; D ; status". `None` is no finished command yet,
        #: or one that was aborted.
        self.last_exit_status: int | None = None

        #: The program rang the bell where nobody saw it: in a pane that
        #: was not the active one of a window a client looks at. Making
        #: the pane active clears it. `#{pane_bell_flag}` is it.
        #: Lillecarl/pymux#501.
        self.bell = False

        #: What `record-pane` is recording of this pane, or None.
        self.recording: Recording | None = None

        #: The files this pane sends to the person, as their parts
        #: arrive. Lillecarl/pymux#743.
        self.downloads = DownloadReader()

    def content_changed(self) -> None:
        "Say that what this pane shows may have changed."
        self.revision += 1

    @property
    def is_copying(self) -> bool:
        """
        Is the client that runs now reading the history of this pane
        rather than the program in it?

        **The widget knows, and a pane asks it.** A flag of the pane's
        own went out of step with the widget once, and five readers of
        it all answered "no". Lillecarl/pymux#133.

        **Copy mode is a client's, not the pane's.** Each client that
        shows the pane is an application of its own, and the program
        runs on under all of them. Lillecarl/pymux#470.
        """
        return self.terminal.is_copying

    def is_copying_for(self, app: Application[Any] | None) -> bool:
        "Is the client of `app` reading the history of this pane?"
        return self.terminal.copy_views.of(app) is not None

    @property
    def process(self):
        return self.terminal.process

    @property
    def screen(self):
        "What the program in this pane has drawn."
        return self.terminal.screen

    @property
    def name(self) -> str:
        """
        The name for the window as displayed in the title bar and status bar.
        """
        # Name, explicitely set for the pane.
        if self.chosen_name:
            return self.chosen_name
        # Name from the process running inside the pane.
        name = self.process.get_name()
        if name:
            return os.path.basename(name)

        return ""

    def current_path(self) -> str:
        """
        Where the pane says it is, else where its process is.

        The same order a new pane starts in (`_directory_to_start_in`), so
        `split-window -c "#{pane_current_path}"` and a bare `split-window`
        agree. Over ssh the process table sees only the local ssh.
        """
        if self.current_directory:
            return self.current_directory
        try:
            return self.process.get_cwd() or ""
        except Exception:
            return ""

    def enter_copy_mode(self) -> None:
        "Open copy mode on this pane, for the client that runs now."
        self.terminal.enter_copy_mode()

    def focus(self) -> None:
        """
        Focus this pane.
        """
        get_app().layout.focus(self.terminal)


#: What names one node of a window's layout tree: its kind and its id.
#: A pane and a split count their ids apart, so the kind keeps them
#: apart. Keys are values, not objects, so a snapshot can write them
#: and a new process can read them back. Lillecarl/pymux#399.
NodeKey = tuple[str, int]


def node_key(item: Node) -> NodeKey:
    "The key of a pane or a split."
    if isinstance(item, Pane):
        return ("pane", item.pane_id)
    return ("split", item.split_id)


class _Split(list["Node"]):
    """
    Base class for horizontal and vertical splits. (This is a higher level
    split than prompt_toolkit.layout.HSplit.)
    """

    _split_counter = 0

    KEEP: ClassVar[dict[str, Keep]] = {
        "split_id": Keep.SAVED,
        "weights": Keep.SAVED,
    }

    def __init__(self, children: Iterable[Node] = ()) -> None:
        list.__init__(self, children)

        _Split._split_counter += 1
        self.split_id = _Split._split_counter

        #: Each child's share of this split, by `node_key`. A child with
        #: no entry weighs 1. An entry outlives a child that leaves, so
        #: a pane that comes back finds the weight it had.
        self.weights: dict[NodeKey, int] = {}

    def weight_of(self, child: Node) -> int:
        "This child's share of the split."
        return self.weights.get(node_key(child), 1)

    def set_weight(self, child: Node, weight: int) -> None:
        self.weights[node_key(child)] = weight

    def __repr__(self) -> str:
        return "%s(%s)" % (self.__class__.__name__, list.__repr__(self))


class HSplit(_Split):
    """Panes stacked one above another."""


class VSplit(_Split):
    """Panes side by side, so this is the split that has a left and a right."""


#: What a split holds: a pane, or a split of the other way.
type Node = Pane | _Split


def _shape_of(split: _Split) -> tuple[bool, tuple[object, ...]]:
    "The kind of `split` and what it holds: pane ids, and the shape of each split inside."
    return (
        type(split) is HSplit,
        tuple(_shape_of(item) if isinstance(item, _Split) else item.pane_id for item in split),
    )


def _place_of(split: _Split, item: object) -> int:
    """
    Where this child sits in the split, by identity.

    `list.index` asks whether two children are equal, and a split *is*
    a list: two stacks holding the same panes would compare equal and
    the first of them would answer for both.
    """
    for place, child in enumerate(split):
        if child is item:
            return place
    raise ValueError("%r is not in %r" % (item, split))


def panes_of(item: Node) -> list[Pane]:
    "Every pane under this item, in the order they are drawn."
    if isinstance(item, Pane):
        return [item]

    result: list[Pane] = []
    for child in item:
        result.extend(panes_of(child))
    return result


class Window:
    """
    Pymux window.
    """

    #: What a hot upgrade does with each attribute; `pyte.keep` says.
    KEEP: ClassVar[dict[str, Keep]] = {
        "window_id": Keep.SAVED,
        "index": Keep.SAVED,
        "root": Keep.SAVED,
        "_active_pane": Keep.SAVED,
        "_pane_history": Keep.SAVED,  # weak references; its newest is saved, by pane id
        "chosen_name": Keep.SAVED,
        "previous_selected_layout": Keep.SAVED,
        "options": Keep.SAVED,
        "column_widths": Keep.SAVED,
        "manual_size": Keep.SAVED,
        "size_when_made": Keep.SAVED,
        "zoom": Keep.SAVED,
        "frame_rate_set": Keep.SAVED,
        "bell": Keep.SAVED,
        "activity": Keep.SAVED,
        "silence": Keep.SAVED,
        "notified": Keep.SAVED,
        "_revisions_seen": Keep.DROPPED,  # the next image's panes count from zero
        "_quiet_since": Keep.DROPPED,
        "_silence_armed": Keep.DROPPED,
        "seen_at": Keep.DROPPED,  # the marks it qualifies are saved
        "tags": Keep.SAVED,
    }

    _window_counter = 1000  # Start here, to avoid confusion with window index.

    def __init__(self, index: WindowIndex = WindowIndex(0)) -> None:
        self.index = index
        self.root: VSplit | HSplit = HSplit()
        self._active_pane: Pane | None = None
        #: The panes this window left, the newest last: closing the
        #: active one returns to the newest still here. Lillecarl/pymux#658.
        self._pane_history: list[ref[Pane]] = []
        self.chosen_name: str | None = None
        self.previous_selected_layout: LayoutTypes | None = None
        #: What a caller said about this window, with `set-tag -w`.
        self.tags: Tags = {}

        self.options = WindowOptions()
        self.options.owner = self

        #: How wide each column of the strip is, as a fraction of the
        #: window, by the `node_key` of the column. A column with no
        #: entry takes `DEFAULT_COLUMN_WIDTH`. Read it through
        #: `column_width`: an entry outlives its column, so walking
        #: this dictionary finds columns that are gone.
        self.column_widths: dict[NodeKey, float] = {}

        #: How big a person said this window is, in cells, or `None`.
        #: `resize-window` writes it and turns `window_size` to
        #: `MANUAL`; nothing reads it under any other policy. It is the
        #: window's own size, so no status row comes off it.
        self.manual_size: Size | None = None

        #: How big this window is while nobody watches it, when its
        #: session named no size: what the clients gave it when it was
        #: made. Lillecarl/pymux#817.
        self.size_when_made: Size | None = None

        #: When true, the current pane is zoomed in.
        self.zoom = False

        #: Whether a person set `frame-rate` on this window. Only then
        #: does it cap the Wayland windows in it, which otherwise draw
        #: as fast as the link to each client takes their frames.
        #: Lillecarl/pymux#636.
        self.frame_rate_set = False

        #: A pane in it rang the bell while no client looked at it.
        #: A client that makes it its window clears it, and
        #: `#{window_bell_flag}` and the `!` of `#F` are it.
        #: Lillecarl/pymux#501.
        self.bell = False

        #: The marks the `monitor-*` options raise in a window no
        #: client looks at. Lillecarl/pymux#767.
        self.activity = False
        self.silence = False
        self.notified = False
        #: The sum of the panes' revisions at the last check, or None
        #: before the first: that check takes the count as it is, so a
        #: load that replays every screen marks nothing.
        self._revisions_seen: int | None = None
        self._quiet_since = 0.0
        #: Output came after the last silence mark, so the next quiet
        #: spell is news.
        self._silence_armed = False
        #: When a client last looked, in `time.time()` like a
        #: notification's `at`: the hub reads a newer one as unread.
        #: Lillecarl/pymux#768.
        self.seen_at = 0.0

        # Give unique ID.
        Window._window_counter += 1
        self.window_id: WindowId = WindowId(Window._window_counter)

    def shape_for_strip(self) -> None:
        """
        Keep the root in the shape the `strip` option needs, after any
        write of it.

        The columns of a strip are the children of `root`, so `root`
        has to be the row itself. A window that was laid out any other
        way becomes the first column of the strip, so nothing on screen
        moves except the way it is laid out.
        """
        if self.options.strip and not isinstance(self.root, VSplit):
            self.root = VSplit([self.root]) if len(self.root) else VSplit()

    def column_width(self, column: Node) -> float:
        "How wide one column of the strip is, as a fraction of the window."
        return self.column_widths.get(node_key(column), DEFAULT_COLUMN_WIDTH)

    def set_column_width(self, column: Node, width: float) -> None:
        self.column_widths[node_key(column)] = width

    def switch_column_width(self, pane: Pane, back: bool = False) -> None:
        """
        Give the column that holds this pane the next preset width.

        The presets are niri's, and so is cycling rather than resizing
        by a step: a person picks between a few widths that fit
        together, instead of nudging a border until it looks right. The
        step is what a divided layout offers and `resize-pane` still
        does. Lillecarl/pymux#198.

        A width nobody chose is the default, and the cycle starts from
        wherever that sits in the list.
        """
        column = self._column_of(pane)
        widths = PRESET_COLUMN_WIDTHS
        now = self.column_width(column)

        try:
            where = widths.index(now)
        except ValueError:
            # A width that is not one of the presets, which `fixed`
            # would give. Step onto the list rather than staying off
            # it, from the end a person asked for.
            where = len(widths) - 1 if back else -1

        self.set_column_width(column, widths[(where - 1 if back else where + 1) % len(widths)])

    def move_column(self, pane: Pane, step: int) -> bool:
        """
        Move the column that holds this pane along the row.

        `step` is -1 for one place to the left and +1 for one to the
        right. Returns whether it moved: a column at the end of the row
        has nowhere to go, and that is not an error.

        **It is a list reorder and nothing more.** The columns are the
        children of the root, `column_widths` is keyed by the column's
        `node_key` so each column keeps the width it was given, and
        `invalidation_hash` names every pane in the order they sit in,
        so the layout is rebuilt. Lillecarl/pymux#202.
        """
        column = self._column_of(pane)
        where = _place_of(self.root, column)
        there = where + step

        if not 0 <= there < len(self.root):
            return False

        self.root[where], self.root[there] = self.root[there], self.root[where]
        return True

    def consume_or_expel(self, pane: Pane, step: int) -> bool:
        """
        Move one pane between the columns of a strip.

        `step` is -1 for the left and +1 for the right. Returns whether
        anything moved.

        **One key does two jobs.** A pane that shares its column
        leaves it, into a new column of its own on that side. A pane
        that is alone in its column joins the next column on that
        side, at the bottom. That is niri's
        `consume-or-expel-window-left`, and the "or" is the point: a
        person holds the key and the pane walks in and out of the
        columns, without ever choosing which of the two moves they
        meant. Lillecarl/pymux#213.

        The two are each other's opposite, so walking one way and back
        again puts the pane where it started.

        A pane that is alone in the column at the end of the row has
        nowhere to go, and stays. That is not an error, the way a key
        held down at the edge of the row is not an error in niri.
        """
        column = self._column_of(pane)

        if len(panes_of(column)) > 1:
            return self._expel(pane, column, step)
        return self._consume(pane, column, step)

    def _expel(self, pane: Pane, column: Node, step: int) -> bool:
        "Take a pane out of the column it shares, into one of its own."
        where = _place_of(self.root, column)

        self._take_out(pane)

        # To the left of the column it left, or to the right of it.
        # Taking the pane out cannot move the column: the column still
        # holds a pane, so it is still there.
        self.root.insert(where if step < 0 else where + 1, pane)
        return True

    def _consume(self, pane: Pane, column: Node, step: int) -> bool:
        "Put a pane that is alone in its column into the next column."
        there = _place_of(self.root, column) + step

        if not 0 <= there < len(self.root):
            return False

        joined = self.root[there]
        self._take_out(column)

        if isinstance(joined, Pane):
            # A column of one pane becomes a stack of two, and keeps
            # the width the column had.
            stack = HSplit([joined, pane])
            self.root.set_weight(stack, self.root.weight_of(joined))
            self.set_column_width(stack, self.column_width(joined))
            self.root[_place_of(self.root, joined)] = stack
        else:
            joined.append(pane)

        return True

    def invalidation_hash(self) -> tuple[object, ...]:
        """
        A value that changes when the layout has to be rebuilt.
        """
        # `strip` is in it because turning the mode on changes how the
        # same panes are laid out and nothing else, so without it the
        # layout would not be rebuilt and the mode would take hold at
        # the next unrelated change.
        return (self.window_id, self.zoom, self.options.strip, _shape_of(self.root))

    @property
    def active_pane(self) -> Pane | None:
        """
        The current active :class:`.Pane`.
        """
        return self._active_pane

    @active_pane.setter
    def active_pane(self, value: Pane | None) -> None:
        # Never the pane itself: removing it would then make the removed
        # pane active again.
        old = self._active_pane
        self._pane_history = [entry for entry in self._pane_history if entry() not in (None, old, value)]
        if old is not None and old is not value:
            self._pane_history.append(ref(old))

        self.zoom = False
        self._active_pane = value
        if value is not None:
            value.bell = False

    def seen(self) -> None:
        "A client looks at this window now, so its marks and its active pane's bell are heard."
        self.bell = self.activity = self.silence = self.notified = False
        self.seen_at = time.time()
        if self._active_pane is not None:
            self._active_pane.bell = False

    def check_output(self, watched: bool, now: float) -> bool:
        """
        Raise the activity or silence mark from what the panes wrote
        since the last check. True when a mark went up.

        Polled, and not raised from the read of each pane: a read is
        the hottest path pymux has, and a revision count costs nothing
        there. Lillecarl/pymux#767.
        """
        total = sum(pane.revision for pane in self.panes)
        first = self._revisions_seen is None
        wrote = not first and total != self._revisions_seen
        self._revisions_seen = total
        if first or wrote:
            self._quiet_since = now
        if wrote:
            self._silence_armed = True
            if self.options.monitor_activity and not watched and not self.activity:
                self.activity = True
                return True
            return False
        quiet = self.options.monitor_silence
        if self._silence_armed and quiet and now - self._quiet_since >= quiet:
            self._silence_armed = False
            if not watched and not self.silence:
                self.silence = True
                return True
        return False

    @property
    def previous_active_pane(self) -> Pane | None:
        "The newest pane this window left that is still in it, or None."
        panes = self.panes
        for entry in reversed(self._pane_history):
            pane = entry()
            if pane is not None and pane in panes and pane is not self._active_pane:
                return pane
        return None

    @property
    def name(self) -> str:
        """
        The name for this window as it should be displayed in the status bar.
        """
        # Name, explicitely set for the window.
        if self.chosen_name:
            return self.chosen_name
        pane = self.active_pane
        if pane:
            return pane.name

        return ""

    def add_pane(self, pane: Pane, vsplit: bool = False, select: bool = True) -> None:
        """
        Add another pane to this Window.

        In a strip a horizontal split opens a new column beside the one
        a person is on, rather than dividing the pane they are on. That
        is the whole of what the mode is for: the panes already open
        keep their widths and the row grows past the edge of the
        screen. A vertical split still stacks inside the column, which
        is what a niri column holds. Lillecarl/pymux#198.

        `select` makes the new pane the active one. Without it the
        active pane and the pane history stay as they were, which is
        `split-window -d`.
        """
        split_cls = VSplit if vsplit else HSplit
        active = self.active_pane

        if active is None:
            self.root.append(pane)
        elif self.options.strip and vsplit:
            column = self._column_of(active)
            self.root.insert(self.root.index(column) + 1, pane)
            if select:
                self.active_pane = pane
            self.zoom = False
            return
        else:
            parent = self._get_parent(active)
            assert parent is not None
            same_direction = isinstance(parent, split_cls)

            index = parent.index(active)

            if same_direction:
                parent.insert(index + 1, pane)
            else:
                new_split = split_cls([active, pane])
                parent[index] = new_split

                # Give the newly created split the same weight as the original
                # pane that was at this position.
                parent.set_weight(new_split, parent.weight_of(active))

                # And, in a strip, the width of the column it became.
                # A pane that is stacked into a column should not make
                # that column change width under a person.
                if self.options.strip and parent is self.root:
                    self.set_column_width(new_split, self.column_width(active))

        if select or active is None:
            self.active_pane = pane
        self.zoom = False

    def remove_pane(self, pane: Pane) -> None:
        """
        Remove pane from this Window.
        """
        if pane in self.panes:
            # When this pane was focused, switch to previous active or next in order.
            if pane == self.active_pane:
                if self.previous_active_pane:
                    self.active_pane = self.previous_active_pane
                else:
                    self.focus_next()

            self._take_out(pane)

    def _take_out(self, item: Node) -> None:
        """
        Take one item out of the tree, and tidy up what it leaves.

        A split with nothing left in it goes as well, and a split with
        one child left becomes that child. The width and the weight of
        a split that collapses move to the child that takes its place,
        so a column does not change size because a pane left it.

        It says nothing about the focus. `remove_pane` moves that
        first, and a pane that is only changing column keeps it.
        """
        parent = self._get_parent(item)
        assert parent is not None
        parent.remove(item)

        # An empty split is not a column of nothing: it is gone.
        while len(parent) == 0 and parent is not self.root:
            above = self._get_parent(parent)
            assert above is not None
            above.remove(parent)
            parent = above

        # A split of one is that one.
        while len(parent) == 1 and parent is not self.root:
            above = self._get_parent(parent)
            assert above is not None
            above.set_weight(parent[0], above.weight_of(parent))

            if node_key(parent) in self.column_widths:
                self.set_column_width(parent[0], self.column_width(parent))

            above[_place_of(above, parent)] = parent[0]
            parent = above

    @property
    def panes(self) -> list[Pane]:
        """
        Every pane of this window, in the order they are drawn.

        **The order is what a person sees, and it used to be the order
        of a walk.** This took each split's own panes before it went
        into the splits inside it, so every pane that sat directly in
        the root was numbered before every pane in a nested split,
        whatever their place on the screen. A strip met that at once:
        turning `strip` on wraps the window it is turning into a row, so
        the first column is a nested split and its panes came last. A
        picture of three columns showed the first one holding pane
        number 2. Lillecarl/pymux#210.

        `get_pane_index` reads this, and that number is what
        `select-pane -t`, `display-panes` and every title bar say. So
        it walks the tree in place: top-left first, the way tmux
        numbers panes and the way an eye reads them.
        """
        return panes_of(self.root)

    @property
    def splits(self) -> list[_Split]:
        "Return a list with all HSplit/VSplit instances."
        result: list[_Split] = []

        def collect(split: _Split) -> None:
            result.append(split)

            for item in split:
                if isinstance(item, (HSplit, VSplit)):
                    collect(item)

        collect(self.root)
        return result

    def _get_parent(self, item: Node) -> _Split | None:
        "The HSplit/VSplit that contains the active pane."
        for s in self.splits:
            if item in s:
                return s

    def _column_of(self, pane: Pane):
        """
        Which column of a strip holds this pane.

        The columns are the children of the root, so this walks up from
        the pane until the thing above it is the root. A pane that sits
        in the root is its own column.
        """
        item = pane
        while True:
            parent = self._get_parent(item)
            if parent is None or parent is self.root:
                return item
            item = parent

    def has_stack(self) -> bool:
        """
        Whether any pane of this window has one above or below it.

        The bar under a pane names those two, so a window with no stack
        in it has nothing to put there and does not reserve the row.
        Lillecarl/pymux#211.
        """
        return any(isinstance(split, HSplit) and len(split) > 1 for split in self.splits)

    @property
    def has_panes(self) -> bool:
        "True when this window contains at least one pane."
        return len(self.panes) > 0

    @property
    def active_process(self):
        "Return `Process` that should receive user input."
        p = self.active_pane

        if p is not None:
            return p.process

    def focus_next(self, count: int = 1) -> None:
        "Focus the next pane."
        panes = self.panes
        if panes:
            self.active_pane = panes[(panes.index(self.active_pane or panes[0]) + count) % len(panes)]
        else:
            self.active_pane = None  # No panes left.

    def focus_previous(self) -> None:
        "Focus the previous pane."
        self.focus_next(count=-1)

    def rotate(
        self,
        count: int = 1,
        with_pane_before_only: bool = False,
        with_pane_after_only: bool = False,
    ) -> None:
        """
        Rotate panes.
        When `with_pane_before_only` or `with_pane_after_only` is True, only rotate
        with the pane before/after the active pane.
        """
        # Create (split, index, pane, weight) tuples.
        items = []
        current_pane_index: int | None = None

        for s in self.splits:
            for index, item in enumerate(s):
                if isinstance(item, Pane):
                    items.append((s, index, item, s.weight_of(item)))
                    if item == self.active_pane:
                        current_pane_index = len(items) - 1

        # Only before after? Reduce list of panes.
        if current_pane_index is not None:
            if with_pane_before_only:
                items = items[current_pane_index - 1 : current_pane_index + 1]

            elif with_pane_after_only:
                items = items[current_pane_index : current_pane_index + 2]

        # Rotate positions.
        for i, triple in enumerate(items):
            split, index, pane, weight = triple

            new_item = items[(i + count) % len(items)][2]

            split[index] = new_item
            split.set_weight(new_item, weight)

    def select_layout(self, layout_type: LayoutTypes) -> None:
        """
        Select one of the predefined layouts.

        This turns a strip off. Each of these five divides the window
        between the panes, which is the one thing a strip does not do,
        so asking for one is asking to leave. Lillecarl/pymux#198.
        """
        self.options.strip = False

        # When there is only one pane, always choose EVEN_HORIZONTAL,
        # Otherwise, we create VSplit/HSplit instances with an empty list of
        # children. With no active pane there is no main pane either.
        active = self.active_pane
        if len(self.panes) == 1 or active is None:
            layout_type = LayoutTypes.EVEN_HORIZONTAL

        # even-horizontal.
        if layout_type == LayoutTypes.EVEN_HORIZONTAL:
            self.root = HSplit(self.panes)

        # even-vertical.
        elif layout_type == LayoutTypes.EVEN_VERTICAL:
            self.root = VSplit(self.panes)

        # main-horizontal.
        elif layout_type == LayoutTypes.MAIN_HORIZONTAL and active is not None:
            self.root = HSplit(
                [
                    active,
                    VSplit([p for p in self.panes if p != active]),
                ]
            )

        # main-vertical.
        elif layout_type == LayoutTypes.MAIN_VERTICAL and active is not None:
            self.root = VSplit(
                [
                    active,
                    HSplit([p for p in self.panes if p != active]),
                ]
            )

        # tiled.
        elif layout_type == LayoutTypes.TILED:
            panes = self.panes
            column_count = math.ceil(len(panes) ** 0.5)

            rows = HSplit()
            current_row = VSplit()

            for p in panes:
                current_row.append(p)

                if len(current_row) >= column_count:
                    rows.append(current_row)
                    current_row = VSplit()
            if current_row:
                rows.append(current_row)

            self.root = rows

        self.previous_selected_layout = layout_type

    def select_next_layout(self, count: int = 1) -> None:
        """
        Select next layout. (Cycle through predefined layouts.)
        """
        # List of all layouts. (When we have just two panes, only toggle
        # between horizontal/vertical.)
        if len(self.panes) == 2:
            all_layouts = [LayoutTypes.EVEN_HORIZONTAL, LayoutTypes.EVEN_VERTICAL]
        else:
            all_layouts = list(LayoutTypes)

        # Get index of current layout.
        layout = self.previous_selected_layout or list(LayoutTypes)[-1]
        try:
            index = all_layouts.index(layout)
        except ValueError:
            index = 0

        # Switch to new layout.
        new_layout = all_layouts[(index + count) % len(all_layouts)]
        self.select_layout(new_layout)

    def change_size_for_pane(self, pane: Pane, up: int = 0, right: int = 0, down: int = 0, left: int = 0) -> None:
        """
        Increase the size of the current pane in any of the four directions.
        Positive values indicate an increase, negative values a decrease.
        """

        def find_split_and_child(split_cls: type[_Split], is_before: bool) -> tuple[_Split | None, Node]:
            "Find the split for which we will have to update the weights."
            child = pane
            split = self._get_parent(child)

            def found() -> bool:
                # None past the root never matches.
                if split is None:
                    return False
                return (
                    isinstance(split, split_cls)
                    and (not is_before or split.index(child) > 0)
                    and (is_before or split.index(child) < len(split) - 1)
                )

            while split and not found():
                child = split
                split = self._get_parent(child)

            return split, child  # split can be None!

        def handle_side(split_cls: type[_Split], is_before: bool, amount: int, trying_other_side: bool = False) -> None:
            "Increase weights on one side. (top/left/right/bottom)."
            if amount:
                split, child = find_split_and_child(split_cls, is_before)

                if split:
                    # Find neighbour.
                    neighbour_index = split.index(child) + (-1 if is_before else 1)
                    neighbour_child = split[neighbour_index]

                    # Increase/decrease weights.
                    split.set_weight(child, split.weight_of(child) + amount)
                    split.set_weight(neighbour_child, split.weight_of(neighbour_child) - amount)

                    # Ensure that all weights are at least one.
                    for one in split:
                        if split.weight_of(one) < 1:
                            split.set_weight(one, 1)

                else:
                    # When no split has been found where we can move in this
                    # direction, try to move the other side instead using a
                    # negative amount. This happens when we run "resize-pane -R 4"
                    # inside the pane that is completely on the right. In that
                    # case it's logical to move the left border to the right
                    # instead.
                    if not trying_other_side:
                        handle_side(split_cls, not is_before, -amount, trying_other_side=True)

        handle_side(VSplit, True, left)
        handle_side(VSplit, False, right)
        handle_side(HSplit, True, up)
        handle_side(HSplit, False, down)

    def get_pane_index(self, pane: Pane) -> PaneIndex:
        "Return the index of the given pane. ValueError if not found."
        return PaneIndex(self.panes.index(pane))
