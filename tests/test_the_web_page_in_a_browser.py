"""
`pymux web`'s page in a real browser: what only a browser can say about
where a glyph lands. `test_the_web_server_serves.py` judges the stream
without one.

Runs in `checks.pymux-browsers`, which puts Chromium on the PATH.
"""

from __future__ import annotations

import json
import re
import socket
from contextlib import asynccontextmanager

import anyio
import pytest
from test_mcp_browsers import ARGUMENTS, CHROMIUM, call, server  # noqa: F401 -- `server` is a fixture

from pymux.web.server import a_token, serve

pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no chromium on the PATH")

#: How long one test of the page may take, from the session to the
#: browser's close. Each takes under five seconds; a hang fails here and
#: says where it waited.
PAGE_DEADLINE = 30

#: Where the border glyph stands in the row: ten narrow letters before it.
BORDER_COLUMN = 10

#: The column the border glyph stands at on the page, in cells of the
#: screen's own font. A Range and not an element: a row that is one text
#: node has no element around the glyph to measure.
WHERE_THE_BORDER_IS = """
(() => {
  const host = document.querySelector("pymux-pane");
  const screen = host.shadowRoot.querySelector(".pyte-screen");
  const walker = document.createTreeWalker(screen, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const at = node.data.indexOf("\\u2502");
    if (at < 0) continue;
    const range = document.createRange();
    range.setStart(node, at);
    range.setEnd(node, at + 1);
    const probe = document.createElement("span");
    probe.textContent = "0".repeat(10);
    probe.style.position = "absolute";
    screen.append(probe);
    const cell = probe.getBoundingClientRect().width / 10;
    probe.remove();
    return (range.getBoundingClientRect().left - screen.getBoundingClientRect().left) / cell;
  }
  return null;
})()
"""


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@asynccontextmanager
async def a_page(server, command: str, writable: bool = False, session: bool = False, query: str = ""):
    """
    A new session running `command`, and the demo page showing its pane
    in Chromium as the browser named "page". Answers the pane's id.
    `session` shows the session as a client draws it instead. `query`
    goes on the page's address, after an `&`.
    """
    with anyio.fail_after(PAGE_DEADLINE):
        async with _a_page(server, command, writable, session, query) as pane:
            yield pane


@asynccontextmanager
async def _a_page(server, command: str, writable: bool, session: bool, query: str):
    await server.cmd(["new-session", "-d", command])
    # The session this made is the one whose pane shows the row; a server
    # may start with one of its own.
    sessions = await call(server, "tree")
    pane = sessions[-1]["windows"][0]["panes"][0]["id"]
    port = a_free_port()
    token = a_token()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(lambda: serve(server.connection.socket_path, "127.0.0.1", port, token, writable))
        if session:
            url = "http://127.0.0.1:%d/?session&t=%s" % (port, token)
        else:
            url = "http://127.0.0.1:%d/?pane=%s&t=%s" % (port, pane.replace("%", "%25"), token)
        if query:
            url += "&" + query
        await call(server, "browser.open", name="page", kind="chromium", seat="headless", url=url, arguments=ARGUMENTS)
        try:
            yield pane
        finally:
            await call(server, "browser.close", name="page")
            tasks.cancel_scope.cancel()


async def test_a_glyph_of_another_width_moves_nothing_after_it(server):
    """
    A glyph whose advance is not a cell's -- a fallback font's, here every
    glyph of a proportional one -- left the border between two panes
    leaning. Each run is now its cells wide. Lillecarl/pyterm#950.
    """
    async with a_page(server, "printf 'iiiiiiiiii\\342\\224\\202x'; sleep 60"):
        with anyio.fail_after(20):
            while await call(server, "browser.evaluate", name="page", expression=WHERE_THE_BORDER_IS) is None:
                await anyio.sleep(0.1)
        # Every glyph of a proportional font has its own width, so the
        # letters before the border are narrower than the cells they
        # stand in.
        await call(
            server,
            "browser.evaluate",
            name="page",
            expression="document.querySelector('pymux-pane').shadowRoot"
            ".querySelector('.pyte-screen').style.fontFamily = 'serif'",
        )
        column = await call(server, "browser.evaluate", name="page", expression=WHERE_THE_BORDER_IS)
        assert column == pytest.approx(BORDER_COLUMN, abs=0.05), json.dumps(column)


#: Two streams, one after the other, that give style 1 two meanings, and
#: the colour the second stream's run draws in.
TWO_STREAMS = """
(() => {
  const pane = document.createElement("pymux-pane");
  document.body.append(pane);
  const welcome = { type: "welcome", revision: 0, writable: false, css: "", size: { rows: 1, columns: 4 } };
  const frame = (colour, text) => ({
    type: "frame", revision: 1, styles: { "1": { s: "color: " + colour } }, rows: { "0": [[1, text, 1]] },
  });
  pane.receive(welcome);
  pane.receive(frame("rgb(255, 0, 0)", "a"));
  pane.receive(welcome);
  pane.receive(frame("rgb(0, 0, 255)", "b"));
  const run = pane.shadowRoot.querySelector(".row > span");
  return [run.textContent, getComputedStyle(run).color];
})()
"""


#: The served families that finished loading.
LOADED_FAMILIES = """
[...document.fonts].filter((face) => face.status === "loaded").map((face) => face.family.replace(/"/g, ""))
"""


async def test_a_symbol_the_text_font_lacks_loads_a_served_fallback(server):
    """
    Claude Code's ⏵⏵ drew as boxes on Android: the page shipped no font,
    and the phone's monospace lacks the glyph. The text face lacks it too,
    so a fallback the server sends draws it. Lillecarl/pyterm#1070.
    """
    async with a_page(server, "printf '\\342\\217\\265\\342\\217\\265 accept edits'; sleep 60") as pane:
        with anyio.fail_after(20):
            while "accept edits" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        loaded: list = []
        with anyio.move_on_after(10):
            while not any(family != "pymux-0" for family in loaded):
                loaded = await call(server, "browser.evaluate", name="page", expression=LOADED_FAMILIES)
                await anyio.sleep(0.1)
        assert "pymux-0" in loaded, loaded
        assert any(family.startswith("pymux-") and family != "pymux-0" for family in loaded), loaded


async def test_a_new_stream_draws_with_its_own_style_table(server):
    """
    A reconnect, after an upgrade or a dropped network, numbers the
    styles from one again. Lillecarl/pyterm#955.
    """
    async with a_page(server, "sleep 60"):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="!!customElements.get('pymux-pane')"
            ):
                await anyio.sleep(0.1)
        drawn = await call(server, "browser.evaluate", name="page", expression=TWO_STREAMS)
        assert drawn == ["b", "rgb(0, 0, 255)"]


async def test_the_button_fills_the_screen_with_the_pane(server):
    "Lillecarl/pyterm#953."
    async with a_page(server, "sleep 60"):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="!!document.getElementById('fullscreen')"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="#fullscreen")
        with anyio.fail_after(10):
            while (
                await call(
                    server, "browser.evaluate", name="page", expression="document.fullscreenElement?.localName ?? ''"
                )
                != "pymux-pane"
            ):
                await anyio.sleep(0.1)
        assert "refused" not in await call(
            server, "browser.evaluate", name="page", expression="document.getElementById('about').textContent"
        )


async def test_escape_on_the_page_reaches_the_program(server):
    "Escape is pymux's abort; the page must not keep it."
    async with a_page(server, "printf ready; cat -v", writable=True) as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        # The click puts the keyboard on the element.
        await call(server, "browser.click", name="page", selector="pymux-pane")
        await call(server, "browser.press", name="page", key="Escape")
        await call(server, "browser.press", name="page", key="Enter")
        with anyio.fail_after(20):
            while "^[" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)


#: The prefix, which `browser.press` cannot hold Control for. The
#: element reads the event and not who sent it. A plain character goes
#: by `beforeinput`, which only a real key fires, so it is pressed.
PRESS_WITH_CONTROL = """
document.querySelector("pymux-pane").shadowRoot.querySelector(".keyboard")
  .dispatchEvent(new KeyboardEvent("keydown", { key: %s, ctrlKey: true, bubbles: true, cancelable: true }))
"""

#: A pointer that moves across the screen, a cell at a time.
POINTER_MOVES = """
(() => {
  const screen = document.querySelector("pymux-pane").shadowRoot.querySelector(".pyte-screen");
  const box = screen.getBoundingClientRect();
  for (let x = 5; x < 200; x += 9) {
    screen.dispatchEvent(new MouseEvent("mousemove", { clientX: box.left + x, clientY: box.top + 40, bubbles: true }));
  }
})()
"""


@pytest.mark.parametrize(
    ("escape", "moving"),
    [("Escape", False), ("Escape", True), ("C-[", False)],
)
async def test_escape_in_the_session_view_leaves_copy_mode(server, escape, moving):
    """
    Escape is pymux's abort. A pointer that moves after it sends mouse
    reports right behind the escape byte. Ctrl-[ is Escape for a browser
    that keeps Escape for itself in full screen: Firefox.
    """
    async with a_page(server, "seq 100; sleep 60", writable=True, session=True):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")

        # Whichever session the client attached to: copy mode on any pane.
        async def mode() -> str:
            tree = await call(server, "tree")
            return "".join(each["mode"] for one in tree for window in one["windows"] for each in window["panes"])

        await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("b"))
        await call(server, "browser.press", name="page", key="[")
        with anyio.fail_after(20):
            while not await mode():
                await anyio.sleep(0.1)

        if escape == "C-[":
            await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("["))
        else:
            await call(server, "browser.press", name="page", key="Escape")
        if moving:
            await call(server, "browser.evaluate", name="page", expression=POINTER_MOVES)
        with anyio.fail_after(10):
            while await mode():
                await anyio.sleep(0.1)


#: Whether the element's keyboard still has the focus.
THE_KEYBOARD_HAS_THE_FOCUS = """
document.activeElement === document.querySelector("pymux-pane")
  && document.querySelector("pymux-pane").shadowRoot.activeElement?.className === "keyboard"
"""


#: Counts what the page asks of the VirtualKeyboard API, where a headless
#: browser has no keyboard to show.
COUNT_KEYBOARD_SHOWS = """
(() => {
  window.keyboardShows = 0;
  Object.defineProperty(navigator, "virtualKeyboard", {
    value: { show: () => { window.keyboardShows += 1; }, hide: () => {} },
    configurable: true,
  });
})()
"""


async def test_a_tap_on_the_terminal_leaves_the_phone_keyboard_closed(server):
    """
    A tap is a click: it gives the textarea the focus, for a hardware
    keyboard, and asks for no on-screen keyboard. The ⌨ of the key row
    does. Lillecarl/pyterm#1077.
    """
    async with a_page(server, "printf ready; sleep 60", writable=True, session=True, query="keys") as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        await call(server, "browser.evaluate", name="page", expression=COUNT_KEYBOARD_SHOWS)
        policy = await call(
            server,
            "browser.evaluate",
            name="page",
            expression="document.querySelector('pymux-pane').shadowRoot.querySelector('.keyboard')"
            ".getAttribute('virtualkeyboardpolicy')",
        )
        assert policy == "manual"

        await call(server, "browser.click", name="page", selector="pymux-pane")
        assert await call(server, "browser.evaluate", name="page", expression=THE_KEYBOARD_HAS_THE_FOCUS)
        assert await call(server, "browser.evaluate", name="page", expression="window.keyboardShows") == 0

        await call(server, "browser.click", name="page", selector="#show-keyboard")
        assert await call(server, "browser.evaluate", name="page", expression="window.keyboardShows") == 1
        assert await call(server, "browser.evaluate", name="page", expression=THE_KEYBOARD_HAS_THE_FOCUS)


async def test_the_escape_button_aborts_and_leaves_the_keys_on_the_terminal(server):
    """
    For a browser that keeps Escape in full screen. Chromium has the
    Keyboard Lock, so the page hides the button here; the test shows it.
    """
    async with a_page(server, "seq 100; sleep 60", writable=True, session=True):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        assert await call(
            server, "browser.evaluate", name="page", expression="document.getElementById('escape').hidden"
        )
        await call(
            server, "browser.evaluate", name="page", expression="document.getElementById('escape').hidden = false"
        )
        await call(server, "browser.click", name="page", selector="pymux-pane")

        async def mode() -> str:
            tree = await call(server, "tree")
            return "".join(each["mode"] for one in tree for window in one["windows"] for each in window["panes"])

        await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("b"))
        await call(server, "browser.press", name="page", key="[")
        with anyio.fail_after(20):
            while not await mode():
                await anyio.sleep(0.1)

        await call(server, "browser.click", name="page", selector="#escape")
        with anyio.fail_after(10):
            while await mode():
                await anyio.sleep(0.1)
        assert await call(server, "browser.evaluate", name="page", expression=THE_KEYBOARD_HAS_THE_FOCUS)


#: Whether each modifier button of the key row shows itself held.
HELD_BUTTONS = """
Object.fromEntries([...document.querySelectorAll("#keys [data-hold]")]
  .map((button) => [button.dataset.hold, button.getAttribute("aria-pressed")]))
"""


async def test_the_key_row_holds_ctrl_for_a_typed_letter(server):
    """
    A phone's on-screen keyboard has no Ctrl and no Esc. The row's Ctrl
    holds for the next letter, which arrives as text the way an
    on-screen keyboard sends it, and its Esc leaves copy mode with the
    keyboard still on the terminal. Lillecarl/pyterm#1051.
    """
    async with a_page(server, "seq 100; sleep 60", writable=True, session=True, query="keys"):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")

        async def mode() -> str:
            tree = await call(server, "tree")
            return "".join(each["mode"] for one in tree for window in one["windows"] for each in window["panes"])

        # The row asks for notifications while nobody has answered:
        # Chrome asks only on a tap. Lillecarl/pyterm#1075.
        permission, hidden = await call(
            server,
            "browser.evaluate",
            name="page",
            expression="[Notification.permission, document.getElementById('notify').hidden]",
        )
        assert hidden == (permission != "default"), permission

        await call(server, "browser.click", name="page", selector="#keys [data-hold=ctrl]")
        assert await call(server, "browser.evaluate", name="page", expression=HELD_BUTTONS) == {
            "ctrl": "true",
            "alt": "false",
        }
        assert await call(server, "browser.evaluate", name="page", expression=THE_KEYBOARD_HAS_THE_FOCUS)
        await call(server, "browser.press", name="page", key="b")
        await call(server, "browser.press", name="page", key="[")
        with anyio.fail_after(20):
            while not await mode():
                await anyio.sleep(0.1)
        # The letter used the hold up.
        assert await call(server, "browser.evaluate", name="page", expression=HELD_BUTTONS) == {
            "ctrl": "false",
            "alt": "false",
        }

        await call(server, "browser.click", name="page", selector="#keys [data-key=Escape]")
        with anyio.fail_after(10):
            while await mode():
                await anyio.sleep(0.1)
        assert await call(server, "browser.evaluate", name="page", expression=THE_KEYBOARD_HAS_THE_FOCUS)


#: Gboard's events, as the keyboard textarea gets them: a composition
#: that holds a word, and the letter that starts one.
GBOARD = """
(() => {
  const keyboard = document.querySelector("pymux-pane").shadowRoot.querySelector(".keyboard");
  const fire = (event) => keyboard.dispatchEvent(event);
  return {
    start: () => fire(new CompositionEvent("compositionstart", { data: "" })),
    letter: (data) => fire(new InputEvent("beforeinput", {
      inputType: "insertCompositionText", data, isComposing: true, bubbles: true, cancelable: false, composed: true,
    })),
    holding: (word) => { keyboard.value += word; },
    end: (data) => fire(new CompositionEvent("compositionend", { data })),
  };
})()
"""


def on_gboard(step: str) -> str:
    return "(%s).%s" % (GBOARD.strip(), step)


async def test_a_word_half_composed_goes_before_a_key_of_the_row(server):
    """
    Gboard holds a word until a space. A key from the row sends the word
    first, and the composition's own end sends it no second time. A held
    Ctrl takes the next composed letter at once. Lillecarl/pyterm#1052.
    """
    async with a_page(server, "stty raw -echo; printf ready; cat -vT", writable=True, query="keys") as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")

        await call(server, "browser.click", name="page", selector="#keys [data-hold=ctrl]")
        await call(server, "browser.evaluate", name="page", expression=on_gboard("start()"))
        await call(server, "browser.evaluate", name="page", expression=on_gboard("letter('x')"))
        await call(server, "browser.evaluate", name="page", expression=on_gboard("end('x')"))

        await call(server, "browser.evaluate", name="page", expression=on_gboard("start()"))
        await call(server, "browser.evaluate", name="page", expression=on_gboard("holding('sta')"))
        await call(server, "browser.click", name="page", selector="#keys [data-key=Tab]")
        await call(server, "browser.evaluate", name="page", expression=on_gboard("end('sta')"))
        await call(server, "browser.press", name="page", key="z")

        shown = ""
        with anyio.move_on_after(10):
            while "z" not in (shown := await call(server, "pane.capture", pane=pane)):
                await anyio.sleep(0.1)
        assert "ready^Xsta^Iz" in shown


#: SwiftKey in Firefox with Ctrl held from the key row: the letter comes
#: as an `insertText` the element cancels, then the keyboard composes it
#: again from its own copy. From a probe, Lillecarl/pyterm#1078.
FIREFOX_HELD_LETTER = """
((letter) => {
  const keyboard = document.querySelector("pymux-pane").shadowRoot.querySelector(".keyboard");
  const fire = (event) => keyboard.dispatchEvent(event);
  const code = letter.toUpperCase().charCodeAt(0);
  fire(new KeyboardEvent("keydown", { key: letter, keyCode: code, bubbles: true, composed: true }));
  const go = fire(new InputEvent("beforeinput", {
    inputType: "insertText", data: letter, isComposing: false, bubbles: true, cancelable: true, composed: true,
  }));
  if (go) keyboard.value += letter;
  fire(new KeyboardEvent("keydown", { key: "Process", keyCode: 229, bubbles: true, composed: true }));
  fire(new CompositionEvent("compositionstart", { data: "" }));
  fire(new InputEvent("beforeinput", {
    inputType: "insertCompositionText", data: letter, isComposing: true, bubbles: true, composed: true,
  }));
  keyboard.value += letter;
  fire(new InputEvent("input", { inputType: "insertCompositionText", data: letter, isComposing: true, bubbles: true, composed: true }));
  fire(new CompositionEvent("compositionend", { data: letter }));
  fire(new InputEvent("input", { inputType: "insertCompositionText", data: letter, isComposing: false, bubbles: true, composed: true }));
})
"""


async def test_a_held_letter_the_keyboard_composes_again_goes_out_once(server):
    "Ctrl-b went out with a b after it, which is send-prefix. Lillecarl/pyterm#1078."
    async with a_page(server, "stty raw -echo; printf ready; cat -vT", writable=True, query="keys") as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")
        await call(server, "browser.click", name="page", selector="#keys [data-hold=ctrl]")
        await call(server, "browser.evaluate", name="page", expression="(%s)('x')" % FIREFOX_HELD_LETTER.strip())
        # A letter typed on purpose after it still goes out.
        await call(server, "browser.press", name="page", key="x")
        await call(server, "browser.press", name="page", key="z")
        shown = ""
        with anyio.move_on_after(10):
            while "z" not in (shown := await call(server, "pane.capture", pane=pane)):
                await anyio.sleep(0.1)
        assert "ready^Xxz" in shown


#: What a browser does with an on-screen keyboard's edits, which a
#: dispatched event does not: `beforeinput`, then, unless the element
#: cancelled it, the change to the textarea and `input`.
KEYBOARD = """
(() => {
  const keyboard = document.querySelector("pymux-pane").shadowRoot.querySelector(".keyboard");
  const input = (inputType, data, isComposing) => keyboard.dispatchEvent(
    new InputEvent("input", { inputType, data, isComposing, bubbles: true, composed: true }));
  return {
    edit: (inputType, data = null) => {
      // Chrome on Android fires nothing for a delete over an empty field.
      if (inputType.startsWith("delete") && !keyboard.value) return;
      const go = keyboard.dispatchEvent(new InputEvent("beforeinput", {
        inputType, data, isComposing: false, bubbles: true, cancelable: true, composed: true,
      }));
      if (!go) return;
      if (inputType === "insertText") keyboard.value += data;
      else if (inputType === "deleteContentBackward") keyboard.value = [...keyboard.value].slice(0, -1).join("");
      else if (inputType === "deleteWordBackward") keyboard.value = keyboard.value.replace(/(\\S+|\\s+)$/, "");
      input(inputType, data, false);
    },
    // A correction: the keyboard composes `word` over the last word of
    // the field, from its own copy of it.
    recompose: (word) => {
      keyboard.dispatchEvent(new CompositionEvent("compositionstart", { data: "" }));
      keyboard.dispatchEvent(new InputEvent("beforeinput", {
        inputType: "insertCompositionText", data: word, isComposing: true, bubbles: true, composed: true,
      }));
      keyboard.value = keyboard.value.replace(/\\S*$/, word);
      input("insertCompositionText", word, true);
      keyboard.dispatchEvent(new CompositionEvent("compositionend", { data: word }));
      input("insertCompositionText", word, false);
    },
  };
})()
"""


def on_the_keyboard(step: str) -> str:
    return "(%s).%s" % (KEYBOARD.strip(), step)


#: An on-screen keyboard's Backspace, as it arrives when its keydown
#: carries no key: only the `beforeinput`.
DELETE_BACKWARD = on_the_keyboard("edit('deleteContentBackward')")


async def test_a_backspace_with_no_key_still_deletes(server):
    "Lillecarl/pyterm#1052."
    async with a_page(server, "printf 'ready\\n'; cat", writable=True, query="probe") as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")
        await call(server, "browser.press", name="page", key="a")
        await call(server, "browser.press", name="page", key="b")
        await call(server, "browser.evaluate", name="page", expression=DELETE_BACKWARD)
        await call(server, "browser.press", name="page", key="Enter")
        # The tty echoes "a", then cat writes back the line it read.
        shown = ""
        with anyio.move_on_after(10):
            while (shown := (await call(server, "pane.capture", pane=pane)).rstrip()) != "ready\na\na":
                await anyio.sleep(0.1)
        assert shown == "ready\na\na"
        # And the probe listed what the keyboard got.
        probe = await call(
            server, "browser.evaluate", name="page", expression="document.getElementById('probe').textContent"
        )
        assert '"inputType":"deleteContentBackward"' in probe, probe


async def test_a_backspace_with_nothing_typed_reaches_the_program(server):
    "Chrome on Android sends no event for a delete over an empty field. Lillecarl/pyterm#1080."
    async with a_page(server, "stty raw -echo; printf ready; cat -vT", writable=True) as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")
        replay = "(() => { const keyboard = %s; %s; })()" % (
            KEYBOARD.strip(),
            "; ".join(
                "keyboard.%s" % step
                for step in [
                    "edit('deleteContentBackward')",
                    "edit('deleteContentBackward')",
                    "edit('insertText', 'a')",
                    "edit('deleteContentBackward')",
                    "edit('deleteWordBackward')",
                ]
            ),
        )
        await call(server, "browser.evaluate", name="page", expression=replay)
        shown = ""
        with anyio.move_on_after(10):
            while "^W" not in (shown := await call(server, "pane.capture", pane=pane)):
                await anyio.sleep(0.1)
        assert "ready^?^?a^?^W" in shown


async def test_a_word_the_keyboard_corrects_goes_out_once(server):
    """
    SwiftKey on Android, typing "Can't we": it typed C, a, n and t as
    plain inserts, deleted the t, and composed "Can" again from its own
    copy of the word before the apostrophe. The element had cancelled each
    letter, so the field was empty and "Can" went out twice. The probe's
    log of it is in Lillecarl/pyterm#1069.
    """
    async with a_page(server, "printf 'ready\\n'; cat", writable=True) as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        await call(server, "browser.click", name="page", selector="pymux-pane")
        # One call, as the keyboard sends them: a call each took a dozen
        # round trips, past the page's deadline on a loaded machine.
        steps = (
            ["edit('insertText', %s)" % json.dumps(letter) for letter in "Cant"]
            + ["edit('deleteContentBackward')", "recompose('Can')"]
            + ["edit('insertText', %s)" % json.dumps(letter) for letter in "'t we"]
        )
        replay = "(() => { const keyboard = %s; %s; })()" % (
            KEYBOARD.strip(),
            "; ".join("keyboard.%s" % step for step in steps),
        )
        await call(server, "browser.evaluate", name="page", expression=replay)
        await call(server, "browser.press", name="page", key="Enter")
        # The tty echoes the line, then cat writes it back.
        shown = ""
        with anyio.move_on_after(10):
            while (shown := (await call(server, "pane.capture", pane=pane)).rstrip()) != "ready\nCan't we\nCan't we":
                await anyio.sleep(0.1)
        assert shown == "ready\nCan't we\nCan't we"


async def test_a_session_view_keeps_its_client_options_over_a_reload(server):
    "Each stream is a new client; the page names what was set on the last. Lillecarl/pyterm#959."

    async def names() -> list[str]:
        listed = await server.cmd(["list-clients", "-F", "#{client_name}"])
        return listed.stdout.split()

    async with a_page(server, "sleep 60", writable=True, session=True):
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)
        # Typed on the page, as a person sets one on the client they look through.
        await call(server, "browser.click", name="page", selector="pymux-pane")
        await call(server, "browser.evaluate", name="page", expression=PRESS_WITH_CONTROL % json.dumps("b"))
        await call(server, "browser.press", name="page", key=":")
        await call(
            server,
            "browser.evaluate",
            name="page",
            expression="document.querySelector('pymux-pane').send({type: 'text', text: 'set-client-option name phone'})",
        )
        await call(server, "browser.press", name="page", key="Enter")
        with anyio.fail_after(10):
            while await call(
                server, "browser.evaluate", name="page", expression="localStorage.getItem('pymux-client-options')"
            ) != json.dumps([["name", "phone"]], separators=(",", ":")):
                await anyio.sleep(0.1)

        await call(server, "browser.evaluate", name="page", expression="location.reload()")
        with anyio.fail_after(20):
            while await names() != ["phone"]:
                await anyio.sleep(0.1)


async def test_a_click_on_the_page_reaches_a_program_that_asked_for_the_mouse(server):
    """
    The whole way: Chromium's click, the element's cell, the server's
    report. The tty echoes the report, so the pane shows it.
    Lillecarl/pyterm#952.
    """
    async with a_page(server, "printf '\\033[?1000h\\033[?1006hready'; cat", writable=True) as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        # The welcome says whether the stream takes input; a click before
        # it is dropped.
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)

        await call(server, "browser.click", name="page", selector="pymux-pane")

        # The press and the release arrive one after the other, so a
        # capture can hold the press alone: wait for both.
        with anyio.fail_after(20):
            while not re.search(
                r"\[<0;\d+;\d+M.*\[<0;\d+;\d+m", await call(server, "pane.capture", pane=pane), re.DOTALL
            ):
                await anyio.sleep(0.1)


#: A finger on the screen that moves up three and a half rows. After the
#: served fonts, which change the row: measured before them, three and a
#: half rows were two of the element's.
SWIPE_UP = """
(async () => {
  await document.fonts.ready;
  const screen = document.querySelector("pymux-pane").shadowRoot.querySelector(".pyte-screen");
  const box = screen.getBoundingClientRect();
  const probe = document.createElement("span");
  probe.textContent = "0".repeat(10);
  screen.append(probe);
  const row = probe.getBoundingClientRect().height;
  probe.remove();
  const x = box.left + 20;
  const y = box.top + row * 8;
  const at = (clientY) => new Touch({ identifier: 1, target: screen, clientX: x, clientY });
  const fire = (kind, touch) => screen.dispatchEvent(new TouchEvent(kind, {
    touches: [touch], targetTouches: [touch], changedTouches: [touch], bubbles: true, cancelable: true, composed: true,
  }));
  fire("touchstart", at(y));
  fire("touchmove", at(y - row * 3.5));
})()
"""


async def test_a_swipe_scrolls_as_a_wheel_does(server):
    "A phone sends no wheel events, and Claude Code scrolled only with one. Lillecarl/pyterm#1072."
    async with a_page(server, "printf '\\033[?1000h\\033[?1006hready'; cat", writable=True) as pane:
        with anyio.fail_after(20):
            while "ready" not in await call(server, "pane.capture", pane=pane):
                await anyio.sleep(0.1)
        with anyio.fail_after(20):
            while not await call(
                server, "browser.evaluate", name="page", expression="document.querySelector('pymux-pane').writable"
            ):
                await anyio.sleep(0.1)

        # The browser pans nothing over a screen that takes input: a pan it
        # started stopped the moves after a few lines. Lillecarl/pyterm#1074.
        assert (
            await call(
                server,
                "browser.evaluate",
                name="page",
                expression="getComputedStyle(document.querySelector('pymux-pane').shadowRoot"
                ".querySelector('.pyte-screen')).touchAction",
            )
            == "none"
        )
        await call(server, "browser.evaluate", name="page", expression=SWIPE_UP)

        # Three rows up shows what is below: three wheel-down reports.
        shown = ""
        with anyio.move_on_after(10):
            while len(re.findall(r"\[<65;\d+;\d+M", shown := await call(server, "pane.capture", pane=pane))) < 3:
                await anyio.sleep(0.1)
        assert len(re.findall(r"\[<65;\d+;\d+M", shown)) == 3, shown
        assert "[<64;" not in shown, shown
