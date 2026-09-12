"""
`which-key`: while the prefix waits, the keys it leads to draw in a
box on the view.

The rules this judges, Lillecarl/pymux#29: the popup opens as soon as
the prefix lands; it draws in the top right, the corner that holds the
least of what a person has on the screen, and steps aside to the
corner diagonally opposite only when the cursor is in its own way; and
the fast typist pays nothing, because the box takes no focus and the
key after the prefix reaches the bindings as it always did.
"""

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point, Size
from prompt_toolkit.layout.containers import ConditionalContainer, Float
from prompt_toolkit.layout.layout import walk
from prompt_toolkit.layout.screen import Screen

from session import create_session, in_a_loop
from pymux.options import ALL_OPTIONS


def which_key_floats(state) -> list[Float]:
    """
    The four floats the which-key box can draw in, in draw order.

    Like the palette's box, each is a `DynamicContainer` over the
    method that builds it, and the method's name tells them apart
    from everything else the layout floats.
    """
    found = []
    for one in state.layout_manager.layout.floats:
        inner = getattr(one.content, "content", None)
        builder = getattr(inner, "get_container", None)
        if builder is not None and builder.__name__ == "_which_key_box":
            found.append(one)
    assert found, "the layout draws no which-key box at all"
    return found


def drawn_which_key_float(state) -> Float | None:
    "The one float whose filter is true now, or None."
    with set_app(state.app):
        drawn = [one for one in which_key_floats(state) if one.content.filter()]
    assert len(drawn) <= 1, "two corners claim the cursor at once"
    return drawn[0] if drawn else None


def put_the_cursor(state, x: int, y: int) -> None:
    """
    Place the cursor where the last frame would have recorded it.

    The screen holds one cursor position per window, so the position
    goes on the real `Screen` for the window the layout has focused --
    the same key `_cursor_on_the_view` reads the position under.
    """
    with set_app(state.app):
        window = state.app.layout.current_window
    screen = Screen(Size(rows=24, columns=80))
    screen.set_cursor_position(window, Point(x=x, y=y))
    state.app.renderer._last_screen = screen


@in_a_loop
async def test_which_key_is_off_to_begin_with():
    "A person who knows the keys does not want a popup in the way."
    async with create_session() as (pymux, state):
        assert not pymux.which_key

        state.has_prefix = True

        assert drawn_which_key_float(state) is None


@in_a_loop
async def test_the_option_and_the_prefix_draw_the_box():
    "The popup is about the prefix, so both have to hold."
    async with create_session() as (pymux, state):
        ALL_OPTIONS["which-key"].set_value(pymux, "on")

        assert drawn_which_key_float(state) is None

        state.has_prefix = True

        drawn = drawn_which_key_float(state)
        assert drawn is not None


@in_a_loop
async def test_the_prefix_alone_asks_for_a_frame():
    """
    The popup draws only when something asks for a frame, and nothing
    else changes when the prefix lands. With the option off, nothing
    wants one.
    """
    async with create_session() as (pymux, state):
        ALL_OPTIONS["which-key"].set_value(pymux, "on")
        asked = []
        real = state.app.invalidate
        state.app.invalidate = lambda: (asked.append(True), real())[1]

        with set_app(state.app):
            pymux.key_bindings_manager._prefix_binding(None)

        assert state.has_prefix
        assert asked

        asked.clear()
        pymux.which_key = False
        with set_app(state.app):
            pymux.key_bindings_manager._prefix_binding(None)

        assert not asked


@in_a_loop
async def test_the_box_prefers_the_top_right_and_steps_aside():
    """
    The top right holds the least of what a person has on the screen,
    so the box draws there by default. Only a cursor that is in its
    own way sends it to the corner diagonally opposite.
    """
    async with create_session() as (pymux, state):
        ALL_OPTIONS["which-key"].set_value(pymux, "on")
        state.has_prefix = True

        for x, y, at_the_top_right in [
            (0, 0, True),  # upper left
            (79, 0, False),  # upper right: in the way
            (0, 23, True),  # lower left
            (79, 23, True),  # lower right
        ]:
            put_the_cursor(state, x, y)

            drawn = drawn_which_key_float(state)

            assert drawn is not None, "no place drew for (%i, %i)" % (x, y)
            assert (drawn.right is not None) == at_the_top_right
            assert (drawn.bottom is not None) != at_the_top_right


@in_a_loop
async def test_the_listing_is_the_prefix_bindings():
    """
    The keys the box lists are the bindings the prefix reaches, with
    the command each one runs. A binding that answers without the
    prefix is somebody's always-on key and shows in no list.
    """
    async with create_session() as (pymux, state):
        manager = pymux.key_bindings_manager
        manager.add_custom_binding("C-z", "send-prefix", [])
        manager.add_custom_binding("x", "swap-pane", ["-U"], needs_prefix=True)
        manager.add_custom_binding("e", "show-clipboard", [], needs_prefix=True)

        rows = manager.keys_a_prefix_leads_to()

        assert ("e", "show-clipboard") in rows
        assert ("x", "swap-pane -U") in rows
        # A binding that answers without the prefix is somebody's
        # always-on key, and shows in no list.
        assert ("C-z", "send-prefix") not in rows
        # The defaults are in the table too, which is the point: the
        # popup teaches the keys nobody has memorised yet.
        assert ("c", "new-window") in rows


@in_a_loop
async def test_the_box_takes_no_focus():
    """
    The key after the prefix must reach the bindings as it always
    did. A focusable control in the box would eat it, and the popup
    would be the fast typist's toll. Lillecarl/pymux#29.
    """
    async with create_session() as (pymux, state):
        ALL_OPTIONS["which-key"].set_value(pymux, "on")
        state.has_prefix = True

        box = state.layout_manager._which_key_box()

        for container in walk(box):
            control = getattr(container, "content", None)
            if control is not None and hasattr(control, "focusable"):
                # `focusable` is a filter, not a flag.
                assert not control.focusable()
