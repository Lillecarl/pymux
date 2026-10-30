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

`browser.open` starts a Chromium for an agent to drive over the DevTools
protocol: `headless` as a job, or `pane` in a window pane that anybody
attached can watch. `browser.snapshot` answers the page as its
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

A browser's profile is thrown away when it closes, unless `profile`
names one: a kept profile lives in
`$XDG_STATE_HOME/pymux/browsers/<name>` and keeps its logins,
extensions and settings for the next browser that names it. One
browser at a time can have a profile open.

`extensions` loads unpacked extensions at launch, and
`browser.extension` loads one into a browser that runs.
`browser.setting` reads or sets a preference, such as
`translate_blocked_languages`, in a settings tab opened in the
background and closed after.

A browser lives across calls until `browser.close`, or until it was
unused for its `idle` seconds, half an hour unless `browser.open` says
otherwise. A browser operation is use, and so is a key or the mouse at a
client that shows the browser's window. A collector in this process
looks every thirty seconds, so nothing is collected while no pymux-mcp
runs.

pymux holds each browser, so it outlives this process. Every operation
connects to the browser again, through the port Chromium writes into
its profile. That port listens on the server machine's loopback, so
pymux-mcp has to run on that machine.

Chromium comes from `executable`, then `PYMUX_MCP_CHROMIUM`, then
`chromium` on the PATH.

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
`tests/test_mcp_browsers.py` drives a real Chromium in both seats, in
`checks.pymux-browsers`.
