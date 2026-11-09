# pymux-mcp

Drive a pymux server from an agent, over MCP.

```sh
pymux-mcp                 # an MCP server on stdio, for the one running pymux server
pymux-mcp --socket PATH   # for the server on that socket
pymux-mcp --openapi       # the operations as an OpenAPI 3.1 document
```

## Two tools

A harness carries every tool's definition on every turn. So this server
has two tools, whatever the number of operations:

- `pymux(op, args)` runs one operation and answers its result. Text is
  answered as it is, and everything else as JSON.
- `pymux_describe(op?)` lists the operations, one line each. With a name
  it answers that operation's arguments as JSON Schema.

```json
{"op": "pane.capture", "args": {"pane": "%5", "start": -20}}
```

Start with `tree`: it answers every session, window and pane, with the
ids that the other operations take.

## The operations

`tree`, `sql`, `command`, `session.new`, `window.new`, `window.split`,
`window.capture`, `pane.capture`, `pane.send`, `pane.wait`, `pane.kill`,
`client.capture`, `job.run`, `job.tail`, `job.list`, `job.output` and
`job.kill`.

`command` runs any pymux command line, for what the rest do not cover.

## Browsers

`browser.open` starts a browser for an agent to drive over the DevTools
protocol: `headless` as a job, `pane` in a window pane that anybody
attached can watch, or `desktop` as a job in a window of its own on the
desktop of the server's machine. `pane` needs `wl`, so it is Linux only;
on macOS, `desktop` is the seat a person can watch. `browser.snapshot` answers the page as its
accessibility tree, with a ref on each element that `browser.click` and
`browser.type` take. `browser.goto`, `browser.press`, `browser.evaluate`,
`browser.screenshot`, `browser.console`, `browser.tabs`, `browser.list`
and `browser.close` do the rest.

A long page need not be read whole. `browser.screenshot` with `marks`
boxes each element a person acts on with its ref, and lists them after
the picture. `browser.at` names the element at a point of a picture,
and `browser.click` takes a point too. `browser.find` answers only the
elements that read some text, and `browser.snapshot` with `within`
only one element's part of the tree.

For the person watching, `browser.selection` reads what they selected,
and `browser.point` outlines an element with a label for a few
seconds.

A `pane` browser opens in a window of its own, or with `window` as a
split of a window somebody works in; closing it then takes only its own
panes away. Every browser prefers reduced motion, so a watched one
sends fewer frames.

`kind` is `chromium`, `qutebrowser`, `firefox` or `glide`. qutebrowser runs on QtWebEngine,
which is Chromium, so every operation that reads or acts on a page works
the same. It loads no extensions and has no `browser.setting`; both say
so. pymux picks its port and quits it with SIGTERM, because QtWebEngine
refuses port 0 and does not quit on `Browser.close`. A Chromium switch
reaches it as `--qt-flag=<switch>` in `arguments`.

### Inside qutebrowser

`qutebrowser.python` runs Python in a qutebrowser's own process, on its
main loop, and answers the last line's value, what it printed, and the
messages the browser showed meanwhile. `qutebrowser.command` runs a
qutebrowser command, such as `set zoom.default 125%`, and answers its
error. So an agent reads and changes a browser's live settings, which
the DevTools protocol cannot reach.

Both go through `qutebridge.py`, a socket that config.py starts inside
the browser. A qutebrowser `browser.open` starts serves it from the
first moment. With no `name`, both operations reach the person's own
qutebrowser, which serves it once its config.py loads the bridge and
the person types `:pymux-bridge on`; `:pymux-bridge off` shuts the agent
out again. `python -m pymux_mcp.qutebridge --off` prints the lines for
config.py. The socket is mode 0600: it runs any Python as the person,
in the browser that holds their logins.

### Firefox and Glide

Firefox and Glide, which is Firefox with a keyboard-driven shell, speak
no Chromium protocol. pymux starts them with `--start-debugger-server`
on a Unix socket in the profile, writes a `user.js` that lets a client
in, and drives them over Firefox's own debugging protocol (`rdp.py`).
The operations are the same; `gecko.py` holds them by the names
`browser.py` gives Chromium's. Nothing sets `navigator.webdriver`, as
Marionette and WebDriver BiDi do.

What reads or acts on a page runs in the tab's content process, in
`gecko_page.js`: the accessibility tree, refs, and input through the
EventUtils Marionette uses, so a page sees a click as trusted.
Loading an address and pictures run in the parent process.

`gecko.chrome` runs JavaScript in the browser window with chrome
privilege, and answers the last statement's value. In Glide, `glide` is
its config API, as `glide.ts` sees it, so an agent reads and changes the
browser's live settings and key maps.

With no `name`, `gecko.chrome` reaches the person's own Glide, through
`glidebridge.py`. `python -m pymux_mcp.glidebridge --off --socket PATH`
prints lines for glide.ts that add `:pymux_bridge on|off|status`. `on`
opens Glide's debugger on a socket, by default
`$XDG_STATE_HOME/pymux/glide.sock`, with mode 0600; `off` closes it. The
path is written into the lines, because a Glide started from the Dock
does not see the shell's environment.

A browser's profile is thrown away when it closes, unless `profile`
names one: a kept profile lives in
`$XDG_STATE_HOME/pymux/browsers/<name>`, or `qutebrowsers/`,
`firefoxes/` or `glides/` for the other kinds, and keeps its logins,
extensions and settings for the next browser that names it. One
browser at a time can have a profile open.

`extensions` loads unpacked extensions at launch, and
`browser.extension` loads one into a browser that runs.
`browser.setting` reads or sets a preference, such as
`translate_blocked_languages`, in a settings tab opened in the
background and closed after.

`via: "client"` sends a browser's traffic out from the machine of the
person attached over `ssh://`, for a site that blocks the server's
address. pymux-mcp asks that client for `forward-port -R 127.0.0.1:0`,
a SOCKS forward. The person confirms it once, and it stays until
`unforward-port`, so the next browser uses it and asks nothing. Each
kind resolves names at the far side too: Chromium's `--proxy-server`,
qutebrowser's `content.proxy`, and Firefox's `network.proxy.socks_remote_dns`.
Lillecarl/pyterm#975.

A browser lives across calls until `browser.close`, or until it was
unused for its `idle` seconds, half an hour unless `browser.open` says
otherwise. A browser operation is use, and so is a key or the mouse at a
client that shows the browser's window. A collector in this process
looks every thirty seconds, so nothing is collected while no pymux-mcp
runs.

pymux holds each browser, so it outlives this process. Every operation
connects to the browser again, through the port written into its
profile. That port listens on the server machine's loopback, so
pymux-mcp has to run on that machine.

The browser comes from `executable`, then `PYMUX_MCP_<KIND>` such as
`PYMUX_MCP_GLIDE`, then the kind's name on the PATH, then, for Firefox
and Glide on macOS, the app in `/Applications`.

## One place for each argument

Each operation is a typed async function over libpymux, in
`pymux_mcp/api.py`. Its signature is read once into a pydantic model.
That model checks what an agent sends, is what `pymux_describe` answers,
and is the request body in the OpenAPI document.

## Who is calling

Every command carries this process's environment and directory, as the
`pymux` command line does. A job then starts where the harness started
this server, and an agent session id that the harness put in the
environment stamps the job.

## Tests

`tests/test_pymux_mcp.py` in pymux checks the registry and the schemas
with no server. `check_pymux_mcp` in `tests/drive_with_pty.py` drives the
two tools through an MCP client against a server that is really there.
`tests/test_mcp_browsers.py` drives a real Chromium, qutebrowser and
Firefox, headless and in a pane, in `checks.pymux-browsers`. nixpkgs has
no Glide, so Glide is checked by hand on macOS.
