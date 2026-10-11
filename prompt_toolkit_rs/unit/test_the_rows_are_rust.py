"""
The run `checks.prompt-toolkit-rs-unit` makes holds Rust rows, or it
says nothing about them.
"""

from __future__ import annotations

import prompt_toolkit_rs
from prompt_toolkit.layout.screen import Screen


def test_a_screen_holds_rust_rows():
    assert prompt_toolkit_rs.installed()
    assert type(Screen().data_buffer[0]) is prompt_toolkit_rs.Row
