"""
`pymux web`: what it serves, and what it refuses.

The adapter runs beside a pymux server rather than inside one -- one
`libpymux.PaneStream` per viewer over the socket -- so the server process
never imports a web library. That is the whole of what "optional" means
here, and it is why these tests can reach the serving without a server of
their own.

**Two things here are about packaging and not about behaviour**, and they
are the two that failed silently. The front end is data in a python
package, so it ships only if something puts it there -- a declaration for
the files a person wrote, and a compiler for the files it emits; and the
websocket library is an extra, so the failure to have it must say which
extra rather than raising `ImportError` at somebody.
Lillecarl/pymux#461.
"""

from pathlib import Path

import pytest

from pymux.web import server as web

# ----------------------------------------------------------------------
# What ships.


def test_the_element_is_in_the_installed_package():
    """
    **Measured, and it did not.** With `packages` alone the wheel held
    every `.py` of `pymux/web/` and nothing of `pymux/web/static/`, so the
    serving answered nothing and said nothing about why.

    Two routes fill this directory and both can fail on their own. The
    files a person wrote arrive because `artifacts` in `pyproject.toml`
    names them and `mkProject` reads the same list. The three the compiler
    writes arrive from the element derivation, which installs them into
    the package and into the tree the suites run against.

    `web.STATIC` and not a path built here: the test asks the question the
    program asks.
    """
    assert (web.STATIC / "pymux-pane.js").is_file()
    assert (web.STATIC / "pymux-pane.d.ts").is_file()
    assert (web.STATIC / "index.html").is_file()
    assert (web.STATIC / "page.js").is_file()
    assert (web.STATIC / "page.css").is_file()


def test_the_source_is_not_beside_what_it_compiles_to():
    """
    The TypeScript lives in `pymux/web/client/` and never here.

    **A check rests on this.** `checks.pymux-element` compiles a
    consumer's file against `pymux-pane.d.ts`, and a `pymux-pane.ts` in
    this directory would win that resolution: the check would read the
    source twice and say nothing about what was emitted.
    """
    beside = [
        found.name
        for found in web.STATIC.iterdir()
        if found.suffix == ".ts" and not found.name.endswith(".d.ts")
    ]
    assert beside == [], beside


@pytest.mark.parametrize("path", sorted(web.SERVED))
def test_everything_named_as_served_is_there(path):
    "A name in the table with no file behind it is a 404 nobody meant."
    assert web._served(path) is not None, path


def test_nothing_outside_the_table_is_served():
    """
    A list and not a directory walk, so no path can climb out of it. The
    spellings below are what a walk would have to refuse one at a time.
    """
    for asked in [
        "/../pyproject.toml",
        "/../../etc/passwd",
        "/static/../../../etc/passwd",
        "/pymux-pane.js/../../server.py",
        "",
        "/pane",
    ]:
        assert web._served(asked) is None, asked


# ----------------------------------------------------------------------
# The token.


def test_a_token_is_not_guessable():
    first, second = web.a_token(), web.a_token()
    assert first != second
    # 32 bytes of urlsafe base64.
    assert len(first) >= 40


def test_a_query_is_read_without_a_url_parser():
    assert web._one_of("t=abc", "t") == "abc"
    assert web._one_of("pane=%1&t=abc", "t") == "abc"
    assert web._one_of("t=abc&pane=%1", "t") == "abc"
    assert web._one_of("pane=%1", "t") is None
    assert web._one_of("", "t") is None
    # A name that only appears as part of another is not that name.
    assert web._one_of("token=abc", "t") is None


# ----------------------------------------------------------------------
# The extra.


def test_the_missing_extra_says_which_extra_and_the_way_around_it():
    """
    A person who did not install it gets a sentence, not a traceback --
    and the sentence names the route that needs nothing, because a caller
    with a front end of its own does not want this at all.
    """
    assert "pymux[web]" in web.MISSING
    assert "stream-pane" in web.MISSING


async def test_serving_without_the_library_raises_what_the_command_prints(
    monkeypatch,
):
    """
    The command turns this one message into an exit code and a line on
    stderr, and it compares against `MISSING` to do it. So the two have to
    be the same string, and this is what says so.
    """
    import builtins

    real = builtins.__import__

    def refuse(name, *arguments, **named):
        if name.startswith("websockets"):
            raise ImportError("no websockets here")
        return real(name, *arguments, **named)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(RuntimeError) as refused:
        await web.serve("/nowhere.sock", "127.0.0.1", 0, "token")

    assert str(refused.value) == web.MISSING


# ----------------------------------------------------------------------
# The element, read as text.
#
# **The compiled file and not the TypeScript**, because that is the one a
# browser runs and the one a page is served. A property that survived the
# source and not the emit is a property a viewer does not get.
#
# No browser here, so these say only what can be said without one: that
# the file holds the things a page needs from it. Whether it draws is what
# a browser answers, and nothing else can.


def source_of(name: str) -> str:
    return (web.STATIC / name).read_text()


def test_the_element_defines_the_tag_the_page_uses():
    element = source_of("pymux-pane.js")
    assert 'customElements.define("pymux-pane"' in element
    assert "<pymux-pane" in source_of("index.html")


def test_the_palette_goes_in_a_sheet_of_its_own():
    """
    A frame's `palette` may not replace the welcome's stylesheet.

    One sheet for both is what lost the screen's background, its font,
    `white-space: pre`, the link rule and the blink on the first frame. A
    browser found it; this says the element keeps them apart.
    """
    element = source_of("pymux-pane.js")
    assert "#paletteSheet" in element
    assert "#adopt(this.#paletteSheet, frame.palette)" in element
    assert "#adopt(this.#themeSheet, frame.css" in element


def test_holding_a_modifier_types_nothing():
    """
    Pressing Control alone sent `C-Control` to the program.

    A keydown of a modifier arrives with that modifier already set, so a
    handler that reads "something is held and there is no text" builds a
    key name out of the modifier's own name. A person found it by
    pressing Control in a browser.

    Read as text, which is all that can be read here: nothing in this
    collection runs the element's key handling. Lillecarl/pymux#468.
    """
    element = source_of("pymux-pane.js")
    assert "ONLY_A_MODIFIER" in element
    for held in ["Control", "Alt", "Meta", "AltGraph", "CapsLock"]:
        assert '"%s"' % held in element, held


def test_the_element_needs_no_unsafe_inline():
    """
    Every rule goes through the CSSOM into a constructed stylesheet, which
    a Content-Security-Policy does not govern. So a page embedding this
    needs no loosening at all -- which the first caller had budgeted for
    and can now drop.

    The two spellings that would need it are a `<style>` element and an
    assignment to `style` on an element, and neither is here.
    """
    element = source_of("pymux-pane.js")
    assert "new CSSStyleSheet()" in element
    assert "adoptedStyleSheets" in element
    assert 'createElement("style")' not in element


def test_nothing_a_program_writes_becomes_markup():
    """
    A program in the pane writes every character of the document, so
    building a string and assigning `innerHTML` is where a `<script>` it
    wrote would become one.
    """
    element = source_of("pymux-pane.js")
    # The assignment and not the word: the file explains in a comment why
    # it does not do this, and a test that forbade the word would forbid
    # saying so. `innerHTML` read is harmless; written is the fault.
    for dangerous in [".innerHTML =", ".innerHTML=", ".outerHTML ="]:
        assert dangerous not in element, dangerous
    assert "textContent" in element


def test_the_page_carries_no_inline_script():
    "The server sends `default-src 'self'`, which would block one."
    page = source_of("index.html")
    assert "<script" in page
    # Every script tag is a src, so none of them holds code.
    for piece in page.split("<script")[1:]:
        head = piece.split(">")[0]
        assert "src=" in head, head
