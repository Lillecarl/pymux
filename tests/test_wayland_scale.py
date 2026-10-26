"""
`wayland-scale`: how dense a Wayland window in a pane draws.
Lillecarl/pymux#637.
"""

from __future__ import annotations

import pytest

from pymux.main import Pymux
from pymux.options import ALL_WINDOW_OPTIONS, SetOptionError
from pymux.wayland_scale import NOMINAL_CELL_HEIGHT, parse_scale, resolve_scale


@pytest.mark.parametrize(
    ("value", "cell_height", "scale"),
    [
        ("auto", NOMINAL_CELL_HEIGHT, 1.0),
        ("auto", 2 * NOMINAL_CELL_HEIGHT, 2.0),
        ("auto", 24, 1.5),
        # A small font is not a reason to shrink a window below 1.
        ("auto", 12, 1.0),
        ("auto+0.25", 32, 2.25),
        ("auto-0.5", 32, 1.5),
        ("auto*1.5", 32, 3.0),
        ("2", 12, 2.0),
        ("1.25", 40, 1.25),
        # In 120ths, as wp_fractional_scale_v1 counts.
        ("auto", 17, 128 / 120),
        # Never below a quarter, however far an offset goes.
        ("auto-5", 16, 0.25),
    ],
)
def test_resolve(value, cell_height, scale):
    assert resolve_scale(value, cell_height) == pytest.approx(scale)


@pytest.mark.parametrize("value", ["", "big", "0", "-1", "auto+", "auto/2", "auto+x", "2x"])
def test_what_is_not_a_scale_is_refused(value):
    with pytest.raises(ValueError):
        parse_scale(value)


def test_spelling_is_kept_tidy():
    assert parse_scale(" Auto + 0.5 ") == "auto+0.5"


async def test_the_option_writes_the_active_window():
    mux = Pymux()
    async with mux.running():
        await mux.create_window("true")
        window = mux.arrangement.windows[0]
        assert window.options.wayland_scale == "auto"
        ALL_WINDOW_OPTIONS["wayland-scale"].set_value(mux, "auto*1.5")
        assert window.options.wayland_scale == "auto*1.5"
        with pytest.raises(SetOptionError):
            ALL_WINDOW_OPTIONS["wayland-scale"].set_value(mux, "huge")
        assert window.options.wayland_scale == "auto*1.5"


async def test_global_form_says_what_new_window_starts_with():
    mux = Pymux()
    async with mux.running():
        await mux.create_window("true")
        mux.handle_command("set-window-option -g wayland-scale 2")
        assert mux.arrangement.windows[0].options.wayland_scale == "auto"
        await mux.create_window("true")
        assert mux.arrangement.windows[-1].options.wayland_scale == "2"
