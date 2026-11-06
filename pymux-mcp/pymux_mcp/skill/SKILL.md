---
name: pymux-mcp
description: Drive a running pymux server through its MCP tools — see what panes show, type into them, wait on output, run jobs, and open a Chromium a person can watch. Use when a task needs a terminal program driven interactively (a TUI, a REPL, a prompt), the screen of a pane or window read back, work shown to the person in their pymux, or a web page driven or inspected.
---

# pymux-mcp

Two MCP tools drive the pymux server this session runs in (`$PYMUX`), or
else the one pymux server running:

- `pymux(op, args)` runs one operation.
- `pymux_describe(op?)` lists the operations; with a name, answers its
  arguments as JSON Schema. Ask before guessing an argument.

The tools may be deferred: load them by name through tool search first.

## Ids

Start with `tree`. It answers every session (`$N`), window (`@N`) and pane
(`%N`), and the other operations take those ids. `tag.set` and `tag.find`
name a thing for later without keeping its id.

## A program in a pane

| step | op |
| --- | --- |
| make a place | `window.new`, or `window.split` beside the person's work |
| type | `pane.send`: text, then named keys, then Return when asked |
| wait | `pane.wait_until` a condition; `pane.wait` for any change |
| read | `pane.capture`; `start`/`end` reach the history |
| end | `pane.kill` |

Wait on what the screen shows, never on a fixed delay. Read a TUI with
`window.capture` (the layout with borders) or `client.capture` (what a
person attached sees, popups included).

## Jobs

`job.run` runs a shell command with no pane. `job.tail` follows it until it
ends or `timeout`, `job.output` reads without waiting, `job.view` shows it
to the person in a window. A job outlives the call that started it.

## Browsers

`browser.open` starts Chromium, or another `kind`: `qutebrowser`, `firefox`,
`glide`:
`seat: "pane"` in a window pane a person can watch (`window` splits theirs;
Linux only), `seat: "desktop"` in a window on the server machine's desktop,
`seat: "headless"` as a job. Then:

- read: `browser.snapshot` (accessibility tree with refs), `browser.find`
  (only elements reading some text), `browser.screenshot` with `marks`
  (refs drawn on the picture);
- act: `browser.click` and `browser.type` take a ref, a selector or a point;
  `browser.goto`, `browser.press`, `browser.evaluate`;
- with the person: `browser.point` shows them an element,
  `browser.selection` reads what they selected.

qutebrowser from inside: `qutebrowser.python` (Python on its main loop; names
`config`, `objreg`, `message`, `command()`, `window()`, `tab()`) and
`qutebrowser.command` (`set zoom.default 125%`). `name` picks a qutebrowser
`browser.open` started; without it, the person's own, after they type
`:pymux-bridge on`.

Firefox or Glide from inside: `gecko.chrome` runs JavaScript in the browser
window with chrome privilege (`gBrowser`, `Services`); in Glide, `glide` is
its config API (`glide.o.hint_size = "20px"`). Without `name`, the person's own
Glide, after they type `:pymux_bridge on`.

A browser closes after `idle` seconds unused (half an hour by default).
`browser.close` it when done. `profile` keeps logins between browsers.

## Everything else

`command` runs any pymux command line. `sql` answers a read-only question
about the server's state.

## Manners

Whatever the agent opens on a person's server, it closes when it has
served: `pane.kill`, `browser.close`. Never kill or send to a pane the
agent did not open unless the person asked.
