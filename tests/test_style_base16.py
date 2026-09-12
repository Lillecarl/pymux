"""
The base16 collection, as pymux themes (`pymux.style_base16`).

A scheme is sixteen colours and a documented meaning per colour: the
roles derive from those, and the palette a pane answers with is the
sixteen verbatim.
"""

import json

import pytest

from pymux import style_base16
from pymux.style import roles_of_palette


A_DARK_SCHEME = {
    "base00": "#1e1e2e", "base01": "#181825", "base02": "#313244",
    "base03": "#45475a", "base04": "#585b70", "base05": "#cdd6f4",
    "base06": "#f5e0dc", "base07": "#b4befe", "base08": "#f38ba8",
    "base09": "#fab387", "base0A": "#f9e2af", "base0B": "#a6e3a1",
    "base0C": "#94e2d5", "base0D": "#89b4fa", "base0E": "#cba6f7",
    "base0F": "#f2cdcd",
}


@pytest.fixture
def schemes(tmp_path, monkeypatch):
    "A collection of two, named by the environment."
    file = tmp_path / "base16-schemes.json"
    file.write_text(
        json.dumps(
            {
                "test-dark": A_DARK_SCHEME,
                "test-light": {letter: "#eeeeee" for letter in A_DARK_SCHEME},
            }
        )
    )
    monkeypatch.setenv("PYMUX_BASE16_SCHEMES", str(file))
    style_base16._schemes.cache_clear()
    yield file
    style_base16._schemes.cache_clear()


def test_the_names_are_the_schemes(schemes):
    assert style_base16.names() == ["test-dark", "test-light"]


def test_a_name_nobody_holds_raises_keyerror(schemes):
    with pytest.raises(KeyError):
        style_base16.base16_roles("no-such-scheme")


def test_a_collection_nobody_carries_holds_no_names(monkeypatch):
    monkeypatch.delenv("PYMUX_BASE16_SCHEMES", raising=False)
    style_base16._schemes.cache_clear()

    assert style_base16.names() == []


def test_the_roles_come_from_the_spec(schemes):
    roles = style_base16.base16_roles("test-dark")

    assert roles["pane"] == "#1e1e2e"  # base00, the background.
    assert roles["text"] == "#cdd6f4"  # base05, the foreground.
    assert roles["text-bright"] == "#b4befe"  # base07.
    assert roles["text-muted"] == "#45475a"  # base03, the comment.
    assert roles["alarm"] == "#f38ba8"  # base08, the red.
    assert roles["signal"] == "#a6e3a1"  # base0B, the green.
    assert roles["warn"] == "#f9e2af"  # base0A, the yellow.
    assert roles["accent"] == "#89b4fa"  # base0D, the blue.


def test_the_palette_is_the_scheme_verbatim(schemes):
    "In the order the spec's own template numbers a terminal's."
    roles = style_base16.base16_roles("test-dark")

    assert roles["color-0"] == "#1e1e2e"  # base00.
    assert roles["color-1"] == "#f38ba8"  # base08, the red.
    assert roles["color-2"] == "#a6e3a1"  # base0B, the green.
    assert roles["color-7"] == "#cdd6f4"  # base05, the text.
    assert roles["color-8"] == "#45475a"  # base03, the muted one.
    assert roles["color-15"] == "#b4befe"  # base07, the bright one.


def test_roles_of_palette_reads_the_terminal_s_order():
    "The sixteen arrive in the order a terminal numbers them."
    sixteen = [
        "#000000", "#ff0000", "#00ff00", "#ffff00",
        "#0000ff", "#ff00ff", "#00ffff", "#ffffff",
        "#808080", "#800000", "#008000", "#808000",
        "#000080", "#800080", "#008080", "#c0c0c0",
    ]

    roles = roles_of_palette(sixteen)

    assert roles["pane"] == "#000000"
    assert roles["alarm"] == "#ff0000"
    assert roles["signal"] == "#00ff00"
    assert roles["text-muted"] == "#808080"
    assert roles["color-3"] == "#ffff00"
    assert roles["color-9"] == "#800000"
