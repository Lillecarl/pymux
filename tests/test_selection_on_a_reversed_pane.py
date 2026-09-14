"""
A selection has to be visible on a pane that reversed its screen.

Lillecarl/pymux#96 gave the copy window the reverse video of the pane,
which revealed this. prompt_toolkit marks a selection by reversing it
(`styles/defaults.py` carries `("selected", "reverse")`), and
`reverse` **sets** rather than toggles (`styles/style.py:231`). So on
a pane with DECSCNM on, a selected cell resolved to reverse and an
unselected cell resolved to reverse, and the selection was there with
nobody able to see it.

The answer is the one a terminal gives by itself. foot documents
`selection-foreground, selection-background` as "Default: inverse
foreground/background", and an inverse of an already inverted screen
is the screen: selected text reads as normal against a reversed pane.
`Terminal._copy_style` puts `class:reversed-pane` on the copy window
and `pymux/style.py` turns the reverse back off for a selected cell.

Lillecarl/pymux#99.
"""

from prompt_toolkit.styles import default_ui_style, merge_styles

from pymux.style import THEMES, theme


def resolved(style, style_str: str):
    return style.get_attrs_for_style_str(style_str)


def the_style(name: str = "default"):
    """
    What a client draws with: prompt_toolkit's own rules, then the
    theme's over them.

    That is the order `Application` merges them in, and the order is
    the whole mechanism -- `selected` says reverse and
    `reversed-pane selected`, read later, says noreverse.
    """
    return merge_styles([default_ui_style(), theme(name)])


def test_an_unselected_cell_of_a_reversed_pane_stays_reversed():
    attrs = resolved(the_style(), "reverse class:reversed-pane")
    assert attrs.reverse is True


def test_a_selected_cell_of_a_reversed_pane_is_not_reversed():
    "The fault: this used to come back reversed, like its neighbours."
    attrs = resolved(the_style(), "reverse class:reversed-pane class:selected")
    assert attrs.reverse is False, "the selection cannot be told from the pane"


def test_a_selected_cell_of_an_ordinary_pane_is_still_reversed():
    "Nothing changes for a pane that never reversed its screen."
    attrs = resolved(the_style(), "class:selected")
    assert attrs.reverse is True


def test_the_search_match_follows_the_same_rule():
    reversed_pane = resolved(
        the_style(), "reverse class:reversed-pane class:incsearch.current"
    )
    ordinary = resolved(the_style(), "class:incsearch.current")

    assert reversed_pane.reverse is False
    assert ordinary.reverse is True


def test_every_theme_carries_the_rule():
    "A theme that dropped it would lose the selection again."
    for name in THEMES:
        attrs = resolved(
            the_style(name), "reverse class:reversed-pane class:selected"
        )
        assert attrs.reverse is False, "%s loses the selection" % (name,)


def test_the_class_only_arrives_when_the_pane_is_reversed():
    "`Terminal._copy_style` is what puts it on, and only then."
    from ptterm.terminal import Terminal

    reversed_style = Terminal._copy_style(
        type("T", (), {"copy_reverse_video": True})()
    )
    plain = Terminal._copy_style(type("T", (), {"copy_reverse_video": False})())

    assert "class:reversed-pane" in reversed_style
    assert "reverse" in reversed_style
    assert plain == ""
