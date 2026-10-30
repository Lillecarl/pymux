"""
`window-codec`: how the Wayland windows in a window reach a client.
Lillecarl/pymux#639.
"""

from __future__ import annotations

import pytest

from pymux.core.main import Pymux
from pymux.enums import WindowCodec
from pymux.options.options import ALL_WINDOW_OPTIONS, SetOptionError


async def test_the_option_writes_the_active_window():
    mux = Pymux()
    async with mux.running():
        await mux.create_window("true")
        window = mux.arrangement.windows[0]
        assert window.options.window_codec is WindowCodec.LOSSLESS
        ALL_WINDOW_OPTIONS["window-codec"].set_value(mux, "vp9")
        assert window.options.window_codec is WindowCodec.VP9
        with pytest.raises(SetOptionError):
            ALL_WINDOW_OPTIONS["window-codec"].set_value(mux, "h265")
        assert window.options.window_codec is WindowCodec.VP9


async def test_global_form_says_what_new_window_starts_with():
    mux = Pymux()
    async with mux.running():
        await mux.create_window("true")
        mux.handle_command("set-window-option -g window-codec vp9")
        assert mux.arrangement.windows[0].options.window_codec is WindowCodec.LOSSLESS
        await mux.create_window("true")
        assert mux.arrangement.windows[-1].options.window_codec is WindowCodec.VP9
        with pytest.raises(SetOptionError):
            ALL_WINDOW_OPTIONS["window-codec"].set_default(mux, "h265")
