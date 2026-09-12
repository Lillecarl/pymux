"""
Arrangement of panes.

Don't confuse with the prompt_toolkit VSplit/HSplit classes. This is a higher
level abstraction of the Pymux window layout.

An arrangement consists of a list of windows. And a window has a list of panes,
arranged by ordering them in HSplit/VSplit instances.
"""

import math
import os
from enum import Enum
from typing import Dict, List
from weakref import WeakKeyDictionary, ref

from prompt_toolkit.application import Application, get_app, get_app_or_none, set_app
from prompt_toolkit.data_structures import Size
from ptterm import Terminal

from .enums import WindowSize

__all__ = [
    "LayoutTypes",
    "Pane",
    "HSplit",
    "VSplit",
    "Window",
    "Arrangement",
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

#: The most frames a second a client draws, until somebody says
#: otherwise. Zero means as many as it can.
#:
#: **Thirty, because a frame is expensive and an eye is not fast.** A
#: frame at 187x59 costs about 19 ms of CPU, nearly all of it building
#: cells that a diff then throws away (Lillecarl/pymux#254). A pane
#: that animates asks for one on every write, and three of them
#: together asked for seventeen a second. Thirty is above what a person
#: reads text at and below what a program writes at, so it costs
#: nothing anybody sees and refuses the frames nobody does.
#:
#: It is not a latency cap. A keystroke's frame is drawn at once
#: whenever the last one was more than a thirtieth of a second ago,
#: which at a human typing speed is always.
DEFAULT_FRAME_RATE = 30


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

    _pane_counter = (
        1000  # Start at 1000, to be sure to not confuse this with pane indexes.
    )

    def __init__(self, terminal: Terminal) -> None:
        self.terminal = terminal
        self.chosen_name: str | None = None

        # Displayed the clock instead of this pane content.
        self.clock_mode = False

        # Give unique ID.
        Pane._pane_counter += 1
        self.pane_id = Pane._pane_counter

    @property
    def is_copying(self) -> bool:
        """
        Is a person reading the history of this pane rather than the
        program in it?

        **The widget knows, and a pane asks it.** A pane held a buffer,
        a flag and a title of its own for this, from before copy mode
        moved into `ptterm`. Nothing had set the flag since, so five
        readers of it all answered "no": the mark in the title bar
        never showed, `#{pane_in_mode}` was always zero, and `send-keys`
        and `clear-history` never refused a pane whose program is
        suspended. Lillecarl/pymux#133.
        """
        return self.terminal.is_copying

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
        else:
            # Name from the process running inside the pane.
            name = self.process.get_name()
            if name:
                return os.path.basename(name)

        return ""

    def enter_copy_mode(self) -> None:
        """
        Suspend the process, and copy the screen content to the `scroll_buffer`.
        That way the user can search through the history and copy/paste.
        """
        self.terminal.enter_copy_mode()

    def focus(self) -> None:
        """
        Focus this pane.
        """
        get_app().layout.focus(self.terminal)


class _WeightsDictionary(WeakKeyDictionary):
    """
    Dictionary for the weights: weak keys, but defaults to 1.

    (Weights are used to represent the proportion of pane sizes in
    HSplit/VSplit lists.)

    This dictionary maps the child (another HSplit/VSplit or Pane), to the
    size. (Integer.)
    """

    def __getitem__(self, key):
        try:
            # (Don't use 'super' here. This is a classobj in Python2.)
            return WeakKeyDictionary.__getitem__(self, key)
        except KeyError:
            return 1


class _Split(list):
    """
    Base class for horizontal and vertical splits. (This is a higher level
    split than prompt_toolkit.layout.HSplit.)
    """

    def __init__(self, *a, **kw):
        list.__init__(self, *a, **kw)

        # Mapping children to its weight.
        self.weights = _WeightsDictionary()

    def __hash__(self):
        # Required in order to add HSplit/VSplit to the weights dict. "
        return id(self)

    def __repr__(self):
        return "%s(%s)" % (self.__class__.__name__, list.__repr__(self))


class HSplit(_Split):
    """Panes stacked one above another."""


class VSplit(_Split):
    """Panes side by side, so this is the split that has a left and a right."""


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


def panes_of(item) -> "List[Pane]":
    "Every pane under this item, in the order they are drawn."
    if isinstance(item, Pane):
        return [item]

    result: List[Pane] = []
    for child in item:
        result.extend(panes_of(child))
    return result


class Window:
    """
    Pymux window.
    """

    _window_counter = 1000  # Start here, to avoid confusion with window index.

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self.root: VSplit | HSplit = HSplit()
        self._active_pane: Pane | None = None
        self._prev_active_pane: "ref[Pane]" | None = None
        self.chosen_name: str | None = None
        self.previous_selected_layout: LayoutTypes | None = None

        #: Lay the panes out as a strip that may be wider than the
        #: screen, instead of dividing the screen between them. Read
        #: and written through the `strip` property below, which keeps
        #: the root in the shape a strip needs. Lillecarl/pymux#198.
        self._strip = False

        #: How wide each column of the strip is, as a fraction of the
        #: window. A column with no entry takes
        #: `DEFAULT_COLUMN_WIDTH`. The keys are the children of `root`,
        #: weakly held, so a column that closes takes its width with
        #: it.
        self.column_widths: "WeakKeyDictionary[object, float]" = WeakKeyDictionary()

        #: Which client's terminal decides how big this window's plane
        #: is, when more than one watches it. `set-window-option
        #: window-size` writes it. Decision 11 of
        #: `docs/layout-engine-plan.md`.
        self.window_size = WindowSize.SMALLEST

        #: How big a person said this window is, in cells, or `None`.
        #: `resize-window` writes it and turns `window_size` to
        #: `MANUAL`; nothing reads it under any other policy. It is the
        #: window's own size, so no status row comes off it.
        self.manual_size: Size | None = None

        #: When true, the current pane is zoomed in.
        self.zoom = False

        #: When True, send input to all panes simultaniously.
        self.synchronize_panes = False

        #: The most frames a second a client draws while it looks at
        #: this window. Zero means as many as it can.
        #:
        #: A window is where this belongs, because a window is what a
        #: client looks at and what holds the programs that animate.
        #: One with cmatrix in it can be told to draw ten times a
        #: second while the one a person is reading stays sharp.
        #: Lillecarl/pymux#254.
        self.frame_rate = DEFAULT_FRAME_RATE

        # Give unique ID.
        Window._window_counter += 1
        self.window_id = Window._window_counter

    @property
    def strip(self) -> bool:
        """
        Whether the panes are a strip that may run past the screen.

        Every other layout divides the window between the panes. A
        strip gives each column a width of its own, lets the row grow
        past the edge, and scrolls to the column a person is on, the
        way niri's scrollable tiling works. Lillecarl/pymux#198.
        """
        return self._strip

    @strip.setter
    def strip(self, value: bool) -> None:
        """
        Turn the mode on or off, and keep the root in the right shape.

        The columns of a strip are the children of `root`, so `root`
        has to be the row itself. A window that was laid out any other
        way becomes the first column of the strip, so nothing on screen
        moves except the way it is laid out.
        """
        self._strip = bool(value)

        if self._strip and not isinstance(self.root, VSplit):
            self.root = VSplit([self.root]) if len(self.root) else VSplit()

    def column_width(self, column) -> float:
        "How wide one column of the strip is, as a fraction of the window."
        return self.column_widths.get(column, DEFAULT_COLUMN_WIDTH)

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

        self.column_widths[column] = widths[
            (where - 1 if back else where + 1) % len(widths)
        ]

    def move_column(self, pane: Pane, step: int) -> bool:
        """
        Move the column that holds this pane along the row.

        `step` is -1 for one place to the left and +1 for one to the
        right. Returns whether it moved: a column at the end of the row
        has nowhere to go, and that is not an error.

        **It is a list reorder and nothing more.** The columns are the
        children of the root, `column_widths` is keyed by the column
        object so each column keeps the width it was given, and
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

    def _expel(self, pane: Pane, column, step: int) -> bool:
        "Take a pane out of the column it shares, into one of its own."
        where = _place_of(self.root, column)

        self._take_out(pane)

        # To the left of the column it left, or to the right of it.
        # Taking the pane out cannot move the column: the column still
        # holds a pane, so it is still there.
        self.root.insert(where if step < 0 else where + 1, pane)
        return True

    def _consume(self, pane: Pane, column, step: int) -> bool:
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
            self.root.weights[stack] = self.root.weights[joined]
            self.column_widths[stack] = self.column_width(joined)
            self.root[_place_of(self.root, joined)] = stack
        else:
            joined.append(pane)

        return True

    def invalidation_hash(self) -> str:
        """
        Return a hash (string) that can be used to determine when the layout
        has to be rebuild.
        """
        #        if not self.root:
        #            return '<empty-window>'

        def _hash_for_split(split: HSplit | VSplit) -> str:
            result = []
            for item in split:
                if isinstance(item, (VSplit, HSplit)):
                    result.append(_hash_for_split(item))
                elif isinstance(item, Pane):
                    result.append("p%s" % item.pane_id)

            if isinstance(split, HSplit):
                return "HSplit(%s)" % (",".join(result))
            else:
                return "VSplit(%s)" % (",".join(result))

        # `strip` is in it because turning the mode on changes how the
        # same panes are laid out and nothing else, so without it the
        # layout would not be rebuilt and the mode would take hold at
        # the next unrelated change.
        return "<window_id=%s,zoom=%s,strip=%s,children=%s>" % (
            self.window_id,
            self.zoom,
            self.strip,
            _hash_for_split(self.root),
        )

    @property
    def active_pane(self) -> Pane | None:
        """
        The current active :class:`.Pane`.
        """
        return self._active_pane

    @active_pane.setter
    def active_pane(self, value: Pane) -> None:
        # Remember previous active pane.
        if self._active_pane:
            self._prev_active_pane = ref(self._active_pane)

        self.zoom = False
        self._active_pane = value

    @property
    def previous_active_pane(self) -> Pane | None:
        """
        The previous active :class:`.Pane` or `None` if unknown.
        """
        p = self._prev_active_pane and self._prev_active_pane()

        # Only return when this pane actually still exists in the current
        # window.
        if p and p in self.panes:
            return p
        return None

    @property
    def name(self) -> str:
        """
        The name for this window as it should be displayed in the status bar.
        """
        # Name, explicitely set for the window.
        if self.chosen_name:
            return self.chosen_name
        else:
            pane = self.active_pane
            if pane:
                return pane.name

        return ""

    def add_pane(self, pane: Pane, vsplit: bool = False) -> None:
        """
        Add another pane to this Window.

        In a strip a horizontal split opens a new column beside the one
        a person is on, rather than dividing the pane they are on. That
        is the whole of what the mode is for: the panes already open
        keep their widths and the row grows past the edge of the
        screen. A vertical split still stacks inside the column, which
        is what a niri column holds. Lillecarl/pymux#198.
        """
        split_cls = VSplit if vsplit else HSplit

        if self.active_pane is None:
            self.root.append(pane)
        elif self._strip and vsplit:
            column = self._column_of(self.active_pane)
            self.root.insert(self.root.index(column) + 1, pane)
            self.active_pane = pane
            self.zoom = False
            return
        else:
            parent = self._get_parent(self.active_pane)
            same_direction = isinstance(parent, split_cls)

            index = parent.index(self.active_pane)

            if same_direction:
                parent.insert(index + 1, pane)
            else:
                new_split = split_cls([self.active_pane, pane])
                parent[index] = new_split

                # Give the newly created split the same weight as the original
                # pane that was at this position.
                parent.weights[new_split] = parent.weights[self.active_pane]

                # And, in a strip, the width of the column it became.
                # A pane that is stacked into a column should not make
                # that column change width under a person.
                if self._strip and parent is self.root:
                    self.column_widths[new_split] = self.column_width(self.active_pane)

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

    def _take_out(self, item) -> None:
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
        parent.remove(item)

        # An empty split is not a column of nothing: it is gone.
        while len(parent) == 0 and parent is not self.root:
            above = self._get_parent(parent)
            above.remove(parent)
            parent = above

        # A split of one is that one.
        while len(parent) == 1 and parent is not self.root:
            above = self._get_parent(parent)
            above.weights[parent[0]] = above.weights[parent]

            if parent in self.column_widths:
                self.column_widths[parent[0]] = self.column_widths[parent]

            above[_place_of(above, parent)] = parent[0]
            parent = above

    @property
    def panes(self) -> List[Pane]:
        """
        Every pane of this window, in the order they are drawn.

        **The order is what a person sees, and it used to be the order
        of a walk.** This took each split's own panes before it went
        into the splits inside it, so every pane that sat directly in
        the root was numbered before every pane in a nested split,
        whatever their place on the screen. A strip met that at once:
        `Window.strip` wraps the window it is turning into a row, so
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
    def splits(self) -> List[HSplit | VSplit]:
        "Return a list with all HSplit/VSplit instances."
        result = []

        def collect(split):
            result.append(split)

            for item in split:
                if isinstance(item, (HSplit, VSplit)):
                    collect(item)

        collect(self.root)
        return result

    def _get_parent(self, item):
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

    def has_a_stack(self) -> bool:
        """
        Whether any pane of this window has one above or below it.

        The bar under a pane names those two, so a window with no stack
        in it has nothing to put there and does not reserve the row.
        Lillecarl/pymux#211.
        """
        return any(
            isinstance(split, HSplit) and len(split) > 1 for split in self.splits
        )

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

    def focus_next(self, count=1) -> None:
        "Focus the next pane."
        panes = self.panes
        if panes:
            self.active_pane = panes[
                (panes.index(self.active_pane or panes[0]) + count) % len(panes)
            ]
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
                    items.append((s, index, item, s.weights[item]))
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
            split.weights[new_item] = weight

    def select_layout(self, layout_type: LayoutTypes) -> None:
        """
        Select one of the predefined layouts.

        This turns a strip off. Each of these five divides the window
        between the panes, which is the one thing a strip does not do,
        so asking for one is asking to leave. Lillecarl/pymux#198.
        """
        self.strip = False

        # When there is only one pane, always choose EVEN_HORIZONTAL,
        # Otherwise, we create VSplit/HSplit instances with an empty list of
        # children.
        if len(self.panes) == 1:
            layout_type = LayoutTypes.EVEN_HORIZONTAL

        # even-horizontal.
        if layout_type == LayoutTypes.EVEN_HORIZONTAL:
            self.root = HSplit(self.panes)

        # even-vertical.
        elif layout_type == LayoutTypes.EVEN_VERTICAL:
            self.root = VSplit(self.panes)

        # main-horizontal.
        elif layout_type == LayoutTypes.MAIN_HORIZONTAL:
            self.root = HSplit(
                [
                    self.active_pane,
                    VSplit([p for p in self.panes if p != self.active_pane]),
                ]
            )

        # main-vertical.
        elif layout_type == LayoutTypes.MAIN_VERTICAL:
            self.root = VSplit(
                [
                    self.active_pane,
                    HSplit([p for p in self.panes if p != self.active_pane]),
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

    def select_previous_layout(self) -> None:
        self.select_next_layout(count=-1)

    def change_size_for_active_pane(
        self, up: int = 0, right: int = 0, down: int = 0, left: int = 0
    ) -> None:
        """
        Increase the size of the current pane in any of the four directions.
        """
        child = self.active_pane
        if child is not None:
            self.change_size_for_pane(child, up=up, right=right, down=down, left=left)

    def change_size_for_pane(self, pane: Pane, up=0, right=0, down=0, left=0):
        """
        Increase the size of the current pane in any of the four directions.
        Positive values indicate an increase, negative values a decrease.
        """

        def find_split_and_child(split_cls, is_before):
            "Find the split for which we will have to update the weights."
            child = pane
            split = self._get_parent(child)

            def found():
                return (
                    isinstance(split, split_cls)
                    and (not is_before or split.index(child) > 0)
                    and (is_before or split.index(child) < len(split) - 1)
                )

            while split and not found():
                child = split
                split = self._get_parent(child)

            return split, child  # split can be None!

        def handle_side(split_cls, is_before, amount, trying_other_side=False):
            "Increase weights on one side. (top/left/right/bottom)."
            if amount:
                split, child = find_split_and_child(split_cls, is_before)

                if split:
                    # Find neighbour.
                    neighbour_index = split.index(child) + (-1 if is_before else 1)
                    neighbour_child = split[neighbour_index]

                    # Increase/decrease weights.
                    split.weights[child] += amount
                    split.weights[neighbour_child] -= amount

                    # Ensure that all weights are at least one.
                    for k, value in split.weights.items():
                        if value < 1:
                            split.weights[k] = 1

                else:
                    # When no split has been found where we can move in this
                    # direction, try to move the other side instead using a
                    # negative amount. This happens when we run "resize-pane -R 4"
                    # inside the pane that is completely on the right. In that
                    # case it's logical to move the left border to the right
                    # instead.
                    if not trying_other_side:
                        handle_side(
                            split_cls, not is_before, -amount, trying_other_side=True
                        )

        handle_side(VSplit, True, left)
        handle_side(VSplit, False, right)
        handle_side(HSplit, True, up)
        handle_side(HSplit, False, down)

    def get_pane_index(self, pane: Pane):
        "Return the index of the given pane. ValueError if not found."
        return self.panes.index(pane)


class Arrangement:
    """
    Arrangement class for one Pymux session.
    This contains the list of windows and the layout of the panes for each
    window. All the clients share the same Arrangement instance, but they can
    have different windows active.
    """

    def __init__(self) -> None:
        self.windows: List[Window] = []

        # The number of the first window. tmux starts at zero, but a
        # keyboard starts at one: the "1" key is easier to reach than
        # the "0" key, and it sits where the first window belongs.
        # `set-option base-index 0` brings the tmux default back.
        self.base_index = 1

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
        self.window_defaults: Dict[str, object] = {}

        self._active_window_for_cli: "WeakKeyDictionary[Application, Window]" = (
            WeakKeyDictionary()
        )
        self._prev_active_window_for_cli: "WeakKeyDictionary[Application, Window]" = (
            WeakKeyDictionary()
        )

        # The active window of the last CLI. Used as default when a new session
        # is attached.
        self._last_active_window: Window | None = None

    def invalidation_hash(self) -> str:
        """
        When this changes, the layout needs to be rebuild.
        """
        if not self.windows:
            return "<no-windows>"

        w = self.get_active_window()
        return w.invalidation_hash()

    def get_active_window(self) -> Window:
        """
        The current active :class:`.Window`.
        """
        return self.get_active_window_for(get_app())

    def get_active_window_for(self, app) -> Window:
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
        """
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
            return window

    def set_active_window(self, window: Window) -> None:
        app = get_app()

        previous = self.get_active_window()
        self._prev_active_window_for_cli[app] = previous
        self._active_window_for_cli[app] = window
        self._last_active_window = window

    def set_active_window_from_pane_id(self, pane_id: int) -> None:
        """
        Make the window with this pane ID the active Window.
        """
        for w in self.windows:
            for p in w.panes:
                if p.pane_id == pane_id:
                    self.set_active_window(w)

    def get_previous_active_window(self) -> Window | None:
        "The previous active Window or None if unknown."
        app = get_app()

        try:
            return self._prev_active_window_for_cli[app]
        except KeyError:
            return None

    def get_window_by_index(self, index):
        "Return the Window with this index or None if not found."
        for w in self.windows:
            if w.index == index:
                return w

    def lowest_free_index(self) -> int:
        "The first index from `base_index` up that no window has."
        taken = {w.index for w in self.windows}
        index = self.base_index
        while index in taken:
            index += 1
        return index

    def make_room_at(self, index: int) -> None:
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

        in_the_way = []
        while index in by_index:
            in_the_way.append(by_index[index])
            index += 1

        # From the top down, so no window lands on one that has not
        # moved yet.
        for window in reversed(in_the_way):
            window.index += 1

    def create_window(
        self,
        pane: Pane,
        name: str | None = None,
        set_active: bool = True,
        index: int | None = None,
    ) -> None:
        """
        Create a new window that contains just this pane.

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
            setattr(w, attribute, value)

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

    def move_window(self, window: Window, new_index: int) -> None:
        """
        Move window to a new index.
        """
        window.index = new_index

        # Sort windows by index.
        self.windows = sorted(self.windows, key=lambda w: w.index)

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
                    break

            for held, width in list(window.column_widths.items()):
                if held is old:
                    window.column_widths[new] = width
                    del window.column_widths[held]

            if window._active_pane is old:
                window._active_pane = new

    def swap_window(self, first: Window, second: Window) -> None:
        """
        Trade the indexes of two windows. Lillecarl/pymux#296.
        """
        first.index, second.index = second.index, first.index

        # Sort windows by index.
        self.windows = sorted(self.windows, key=lambda w: w.index)

    def get_active_pane(self) -> Pane | None:
        """
        The current :class:`.Pane` from the current window.
        """
        return self.get_active_pane_for(get_app())

    def get_active_pane_for(self, app) -> Pane | None:
        "The active :class:`.Pane` of one client. See `get_active_window_for`."
        w = self.get_active_window_for(app)
        if w is not None:
            return w.active_pane
        return None

    def remove_pane(self, pane: Pane) -> None:
        """
        Remove a :class:`.Pane`. (Look in all windows.)
        """
        for w in self.windows:
            w.remove_pane(pane)

            # No panes left in this window?
            if not w.has_panes:
                # Focus next.
                for app, active_w in self._active_window_for_cli.items():
                    if w == active_w:
                        with set_app(app):
                            self.focus_next_window()

                self.windows.remove(w)

    def focus_previous_window(self) -> None:
        w = self.get_active_window()

        self.set_active_window(
            self.windows[(self.windows.index(w) - 1) % len(self.windows)]
        )

    def focus_next_window(self) -> None:
        w = self.get_active_window()

        self.set_active_window(
            self.windows[(self.windows.index(w) + 1) % len(self.windows)]
        )

    def break_pane(self, set_active: bool = True) -> None:
        """
        When the current window has multiple panes, remove the pane from this
        window and put it in a new window.

        :param set_active: When True, focus the new window.
        """
        w = self.get_active_window()

        if len(w.panes) > 1:
            pane = w.active_pane
            if pane is not None:
                self.get_active_window().remove_pane(pane)
                self.create_window(pane, set_active=set_active)

    def rotate_window(self, count: int = 1) -> None:
        "Rotate the panes in the active window."
        w = self.get_active_window()
        w.rotate(count=count)

    @property
    def has_panes(self) -> bool:
        "True when any of the windows has a :class:`.Pane`."
        for w in self.windows:
            if w.has_panes:
                return True
        return False
