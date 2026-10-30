"""
What draws over what: the z-index of each kind of float pymux draws.
"""

from __future__ import annotations

__all__ = ["Z_INDEX"]


class Z_INDEX:
    #: The shade at a cut edge: after the pane's cells, which it reads,
    #: and under the bars and popups, which it must not shade.
    #: Lillecarl/pymux#616.
    CUT_SHADE = 1
    HIGHLIGHTED_BORDER = 2
    STATUS_BAR = 5
    COMMAND_LINE = 6
    MESSAGE_TOOLBAR = 7
    #: A question sits where a message sits, so the two can meet: a
    #: command that said something and then asked. The question wins,
    #: because it is the one waiting on the person.
    #: Lillecarl/pymux#339.
    CONFIRMATION = 8
    WINDOW_TITLE_BAR = 9
    #: The mouse handlers of the lines between panes, over the bars a
    #: stacked pane draws on its line and under every popup. They draw
    #: nothing. Lillecarl/pymux#772.
    BORDER_LINE = 10
    #: Under every popup a person opened: a notification waits, and
    #: what the person is doing does not. Lillecarl/pymux#826.
    NOTIFICATION_POPUP = 11
    POPUP = 12
    OVERLAY = 13
    #: Over everything a person works in, because it is about all of
    #: it, and gone in seconds. Lillecarl/pymux#241.
    ERROR_NOTICE = 14

    #: A clock and a pane number are drawn inside the pane they belong
    #: to, over the content of that pane. Nothing else draws in those
    #: cells, so the only thing this height decides is what happens
    #: when one of them meets the chrome above: a popup or an overlay
    #: covers the panes, and a clock drawn at a hundred paints over
    #: one. They are left where they were found, well clear of the
    #: list, because no case has been seen where the two meet.
    CLOCK = 100
    PANE_NUMBER = 100
    #: Over the pane and over the clock and the numbers: a label is
    #: what the person is reading while the mode is up.
    URL_LABEL = 101
    #: Last, so its mouse handler is the one every cell keeps while a
    #: line is dragged. It draws nothing. Lillecarl/pymux#772.
    DRAG_CAPTURE = 1000
