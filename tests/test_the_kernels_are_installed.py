"""
The server draws with the Rust kernels.

A build that lost `pyte_rs` or `prompt_toolkit_rs` still draws the same
cells, slower, and no other test would say so. Lillecarl/pymux#566.
"""

from __future__ import annotations

import os

import prompt_toolkit.layout.containers
import prompt_toolkit.renderer
import prompt_toolkit_rs
import pyte.runs
import pyte.screen
import pyte_rs
import pytest

import pymux.main  # noqa: F401  # installs the kernels


@pytest.mark.skipif(os.environ.get("PYTERM_PURE", "") not in ("", "0"), reason="the run asked for the Python")
def test_every_kernel_is_in_place():
    assert pyte.runs.runs_of is pyte_rs.runs_of
    assert pyte.screen._draw_on_row is pyte_rs.draw_on_row
    assert prompt_toolkit.layout.containers._copy_single_width is prompt_toolkit_rs.copy_single_width
    assert prompt_toolkit.renderer._changed_spans is prompt_toolkit_rs.changed_spans
