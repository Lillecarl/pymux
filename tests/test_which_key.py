"""
`which-key`: while the prefix waits, the keys it leads to draw in a
box, placed on the view diagonally opposite the cursor.

Lillecarl/pymux#29 records the two rules this judges: the popup opens
as soon as the prefix lands, and it is placed on the view, not the
pane, as far from the cursor as the view allows. The fast typist pays
nothing: the box takes no focus, so the key after the prefix reaches
the bindings as it always did.
"""

from types import SimpleNamespace

from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.layout.containers import ConditionalContainer, Float
from prompt_toolkit.layout.layout import walk

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

    `renderer._last_screen` is what `_cursor_on_the_view` reads; a
    stand-in with a cursor position is what the placement needs, and
    a `SimpleNamespace` gives it without drawing a frame.
    """
    state.app.renderer._last_screen = SimpleNamespace(
        cursor_position=Point(x=x, y=y)
    )


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
async def test_the_box_draws_opposite_the_cursor():
    """
    The rule the issue records: as far from the cursor as possible.
    A cursor in the upper left puts the box at the bottom right, and
    each corner round the clock answers the same way.
    """
    async with create_session() as (pymux, state):
        ALL_OPTIONS["which-key"].set_value(pymux, "on")
        state.has_prefix = True

        for x, y, anchored_right, anchored_bottom in [
            (0, 0, True, True),  # upper left
            (79, 0, False, True),  # upper right
            (0, 23, True, False),  # lower left
            (79, 23, False, False),  # lower right
        ]:
            put_the_cursor(state, x, y)

            drawn = drawn_which_key_float(state)

            assert drawn is not None, "no corner drew for (%i, %i)" % (x, y)
            assert (drawn.right is not None) == anchored_right
            assert (drawn.bottom is not None) == anchored_bottom


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
