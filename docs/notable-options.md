# Notable options

The options a person will probably want an opinion on. Each line gives the
default and why. A default is chosen in this order: what a user probably
wants, then what the maintainer wants, then what tmux does.

Set a session or window option with `set-option`, `set-window-option`, or
the `-g` form for new windows. Set a client option with
`set-client-option`, or in the client's own configuration file, which the
client reads where the person sits.

## Programs in panes reaching the person's machine

| option | scope | default | why |
| --- | --- | --- | --- |
| `pane-browser` | session | `agents` | Playwright MCP and `playwright-cli` in a pane drive a Chrome on the person's machine through `pymux-browser`, and each launch is asked first. `all` adds Puppeteer, whose test suites would ask on every launch; `off` keeps every browser on the server. Lillecarl/pymux#653. |
| `browser-program` | client | first Chromium found | Which browser `pymux-browser` starts on this machine. Playwright drives only a Chromium: Chrome, Chromium, Edge or Brave. A Firefox is used only when no Chromium is there, and only BiDi tools (Puppeteer, Selenium) can drive it. |
| `browser-profile` | client | one pymux keeps | Never the person's own profile: Chrome refuses remote control of it, and a script should not hold the person's logins. |
| `open-url-mode` | session | `open` | A URL a pane opens goes to the browser of the client used last, at once. `ask` asks first; `off` drops it. |
| `open-url-shim` | session | off | On, `xdg-open` and `$BROWSER` in a pane open on the person's machine. Off by default because it shadows the server's own opener. |
| `notify-shim` | session | off | On, `notify-send` in a pane notifies through pymux. |
| `forward-mode` | session | `on` | A loopback URL a pane opens is forwarded to the client's machine. |
| `set-clipboard` | session | `external` | A pane's copy reaches the person's clipboard. |

## Wayland windows in panes

| option | scope | default | why |
| --- | --- | --- | --- |
| `wayland-server` | session | on | Panes get a Wayland display, so `pymux wl` and the clipboard tools work. Absent where pysmithay is not, for example on macOS. |
| `window-codec` | window | `lossless` | Exact pixels. `vp9` sends far fewer bytes over a slow link, at some sharpness, and smooths video. |
| `wayland-scale` | window | `auto` | A window draws at the density of the client's cells. |
| `window-scroll` | client | `smooth` | One scroll event moves one cell of pixels, which follows a trackpad. `notch` is a mouse wheel's step. The terminal does not say which device it has. |

## The person's terminal

| option | scope | default | why |
| --- | --- | --- | --- |
| `mouse` | session | on | Unlike tmux: a click selects a pane and reaches the program. |
| `extended-keys` | session | `on` | Every key a terminal can name reaches the pane. |
| `theme` | client | the theme nearest the terminal's own colours | Per client, because two people may sit at different screens. |
| `full-screen` | client | off | On, one pane fills this terminal, with no status line. |
