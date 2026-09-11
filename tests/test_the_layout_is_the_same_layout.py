"""
Walking the layout twice finds the same controls.

prompt_toolkit keys the key bindings of the whole application by the
set of controls it can reach: `_CombinedRegistry._key_bindings` builds
`(current_window, frozenset(other_controls))` and looks that up in a
cache of eight. A container that hands back a fresh object each walk is
a key that never matches, so every key press rebuilt every binding.

`checks.pymux-keystroke` holds the total, and a total says a stage grew
without saying why. This says which invariant the number rests on.
Lillecarl/pymux#233.
"""

from prompt_toolkit.application.current import set_app

from a_session import a_session, in_a_loop


def the_controls(app):
    "What `_CombinedRegistry` would key its cache by."
    return frozenset(app.layout.find_all_controls())


@in_a_loop
async def test_walking_the_layout_twice_finds_the_same_controls():
    async with a_session() as (pymux, state):
        with set_app(state.app):
            first = the_controls(state.app)
            second = the_controls(state.app)

    assert first == second


@in_a_loop
async def test_the_empty_overlay_is_one_window_and_not_a_new_one():
    """
    The container that held a fresh `Window()`, which makes a
    `DummyControl` of its own every time it is asked.
    """
    async with a_session() as (pymux, state):
        manager = state.layout_manager
        with set_app(state.app):
            assert pymux.overlay_pane is None
            assert manager._overlay_container() is manager._overlay_container()
