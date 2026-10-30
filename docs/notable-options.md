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
| `notification-popups` | client | `auto` | A notification shows as a box inside pymux, in the corner away from the cursor, for `notification-popup-seconds` (5). `auto` shows boxes only on a terminal not known to show notifications: kitty answers the OSC 99 query, and iTerm2, WezTerm, Ghostty and foot are known by name. `on` shows both. Lillecarl/pymux#826. |
| `forward-mode` | session | `on` | A loopback URL a pane opens is forwarded to the client's machine. |
| `set-clipboard` | session | `external` | A pane's copy reaches the person's clipboard. |
| `file-transfer` | client | `auto` | A file a pane sends (iTerm2's `File=` download, or `pymux send-file FILE`) goes to the client the person used last on that session. `pymux` saves it on the client's machine, in `$XDG_DOWNLOAD_DIR`, else `~/Downloads`, else the client's directory, and never over a file that is there. `passthrough` gives the sequence to the client's terminal when it is iTerm2, the one terminal known to save it, and refuses with a message for any other. `auto` is `passthrough` for a client on the server's machine whose terminal is iTerm2, and `pymux` for every other. Up to 64 MiB a file. Lillecarl/pymux#743, Lillecarl/pymux#746. |
| `upload-paths` | client, own file only | empty | Directories whose files `pymux receive` gets from the person's machine without asking, `:` between them. A path is resolved before the check, symlinks followed and `..` removed, so nothing escapes a directory. Every other path waits for the person's `y`, `a` or `n` on the client's own terminal. A shell's completion of `receive` sees only the names that lead into these directories, or every name inside a grant. The client reads it from its own configuration file and never sends it: the server is the side a program in a pane controls, so `set-client-option` there refuses it. Lillecarl/pymux#743. |
| `upload-grant` | client, own file only | `10` | Minutes an answer of `a` lets this server read files without asking. Read the same way as `upload-paths`. |

## Sound

| option | scope | default | why |
| --- | --- | --- | --- |
| `audio-server` | session | `auto` | Panes play into a speaker and record from a microphone that pymux carries to the client used last, on the connection it attached on, ahead of window frames. `pymux integrated` has none: its panes play on the person's own sound. `auto` is `private` when the build carries PipeWire (the Linux package does), else `shared`. `private` starts a PipeWire and a WirePlumber of the server's own, so a host with no sound works and two servers never fight over a default sink. `shared` adds them to the PipeWire the server finds, named to panes in `PULSE_SINK` and `PULSE_SOURCE`; any program on that PipeWire, a pane's or not, can then play to the person and record their microphone. Lillecarl/pymux#657. |
| `audio` | client | `on` | This machine plays the panes' sound. The microphone opens only while a program on the server records, never before. `playback` never lends the microphone; `off` plays nothing. |
| `audio-rtt-multiplier` | client | `2` | The server measures the round trip on the client's own connection, and both ends buffer the slowest recent one times this, at least two frames and at most a second. Two round trips cover a link whose delay doubles for a moment. Lower is less delay and more gaps. The floor of two frames is fixed and not an option: one late packet on a local link is then not a gap. Lillecarl/pymux#661. |
| `audio-frame` | client | `auto` | How much sound one packet carries, in ms: 2.5, 5, 10, 20, 40 or 60, every size Opus encodes, or `auto`. Each end buffers two frames and its device one, so shorter is less delay; longer is fewer packets. `auto` starts at 2.5 and goes up a size whenever the client's speaker falls behind or runs dry twice in ten seconds, and says so; a new connection starts low again. The server's PipeWire moves sound 10 ms at a time whatever the frame, so frames under 10 ms shorten the buffers and the client's device but not the server's side. |
| `audio-speaker-bitrate` | client | `128` | kbit/s of Opus for the panes' sound to this client, 6 to 510. 128 is enough for music; more helps dense music on a fast link, and 64 to 96 saves a slow one. A change takes effect at the next frame. Lillecarl/pymux#674. |
| `audio-microphone-bitrate` | client | `64` | kbit/s of Opus for this client's microphone, 6 to 510. 64 is a clear voice; 24 to 32 is still a clear voice and saves a slow link. A change takes effect at the next frame. |

The build switch: the root `default.nix` passes `pipewire` and `wireplumber` to the pymux package on Linux. Pass `null` for either to leave them out of the closure; `private` then needs both on the PATH.

## Wayland windows in panes

| option | scope | default | why |
| --- | --- | --- | --- |
| `wayland-server` | session | on | Panes get a Wayland display, so `pymux wl` and the clipboard tools work. Absent where pysmithay is not, for example on macOS. |
| `wayland-portal` | server | `host` | Programs in window panes draw light or dark the way the theme in front of you does, live. `host` serves the portal on the host's session bus only where no real portal runs or can start, so programs keep `systemctl --user` and notifications. `private` works beside a real portal, but `pymux wl` programs lose the host's bus. |
| `window-codec` | window | `lossless` | Exact pixels. `vp9` sends far fewer bytes over a slow link, at some sharpness, and smooths video. |
| `wayland-scale` | window | `auto` | A window draws at the density of the client's cells. |
| `window-scroll` | client | `smooth` | One scroll event moves one cell of pixels, which follows a trackpad. `notch` is a mouse wheel's step. The terminal does not say which device it has. |

## Which windows want attention

A window that no client looks at carries a mark in `#F` (and so in the status line), in the window chooser's session label, and in `#{session_alerts}`. Looking at the window clears its marks. Lillecarl/pymux#767.

| option | scope | default | why |
| --- | --- | --- | --- |
| `monitor-activity` | window | on | Output marks the window with `activity-symbol`. tmux has it off; a mark nobody turned on tells nobody which window is busy. |
| `monitor-notification` | window | on | A notification from a pane (OSC 9, 99 or 777, or `pymux notify`) marks its window with `notification-symbol`. Not in tmux. |
| `monitor-silence` | window | `0` | Seconds of quiet after output that mark the window with `silence-symbol`: the mark that says a build ended. Off, as in tmux, because the right time depends on the program. |
| `monitor-bell` | window | on | A bell marks the window with `bell-symbol`, as in tmux. |
| `activity-symbol`, `bell-symbol`, `silence-symbol`, `notification-symbol` | session | `#`, `!`, `~`, `?` | ASCII, so every font has them; the first three are tmux's. Each mark has its own symbol until use shows which programs send what. Set icons here for a font that has them. |

## A log of what you do

| option | scope | default | why |
| --- | --- | --- | --- |
| `action-log` | session | off | On, every command goes to `$XDG_STATE_HOME/pymux/actions.db`, with what started it (a key and its table, the command bar, a menu, a chooser, a click, a hook, the config file, the socket), the client, session, window and pane, the program in that pane, and why it failed if it did. So do clicks that select a pane or a window, and rows taken in a chooser. The text of `send-keys` and the value of `set-buffer` are only their length; nothing a pane shows is kept. It is for reading back, by a person or a model, to find the bindings and features worth learning. Off, because nobody expects a multiplexer to write down what they do. |
| `action-log-limit` | session | `500000` | The most rows the file keeps, oldest out first: about a year of a busy day, near 100 MB. Zero keeps all. The servers of every socket write the one file. |

Ask it with `sqlite3`, for example the commands you run most and how:

    sqlite3 ~/.local/state/pymux/actions.db \
      "SELECT name, origin, key_table, key, count(*) AS n FROM actions
       GROUP BY 1, 2, 3, 4 ORDER BY n DESC LIMIT 30"

## The person's terminal

| option | scope | default | why |
| --- | --- | --- | --- |
| `mouse` | session | on | Unlike tmux: a click selects a pane and reaches the program. A drag of the line between two panes, or of the bars between two stacked panes, resizes them, as in tmux. Over such a line the pointer takes a resize shape, in a terminal that reads OSC 22. |
| `extended-keys` | session | `on` | Every key a terminal can name reaches the pane. |
| `status-keys` | session | from `$VISUAL`, else `$EDITOR` | The keys of the `:` line and of `command-prompt`: `emacs`, `vi`, or `helix`. An editor whose name holds `vi` gives `vi`, as tmux does; `hx` or `helix` gives `helix`. With `vi` or `helix` the first Escape leaves insert mode and the second closes the line, and the line shows its mode. Lillecarl/pymux#749. |
| `mode-keys` | session | from the editor, as above | The keys of copy mode: `emacs`, `vi`, or `helix`. With `helix` the copy is Helix: its moves, selections, `/` search and edits, so a person can trim or change the text before copying it. `y` or Enter copies and leaves, `q` or Escape in normal mode leaves, and `Alt-r` takes a new copy, refused while there are edits. Leaving drops the edits. The corner shows the mode, a search as it is typed, and whether the copy is edited. Lillecarl/pymux#751. |
| `steal-focus` | session | off | On, a program's `OSC 1337 ; StealFocus` takes every client on the session to its pane, and asks the terminal to raise its window. Off, because a program that moves people at will is a nuisance. |
| `notification-keep-seen` | server | `3600` | The notification hub forgets a notification an hour after a client saw the window it came from, so the hub holds what is new. A notification that came while a client looked at its window is seen then; one whose pane is gone is seen when the minute's sweep finds it so. An unseen one stays. Zero keeps every one until the hub holds 256. |
| `notification-click-shows-pane` | session | on | A click on a desktop notification takes the client whose terminal was clicked to the pane it came from: its session, window and pane. It needs a terminal that reports clicks (OSC 99 with `a=report`), such as kitty; pymux asks the terminal at attach. To such a terminal, OSC 777, `pymux notify` and an attention request go out as OSC 99 too, so a click on them works the same. A program that said `a=-focus` keeps the client where it is. |
| `copy-mode-wheel-lines` | session | `1` | One notch of the wheel moves copy mode one line, so a mouse wheel reads the history without skipping. tmux binds five. |
| `key-profile` | session | `kitty` | Where terminals split on a key, a legacy program gets what the client's own terminal sends. This names the terminal for a client whose terminal does not name itself. |
| `theme` | client | the theme nearest the terminal's own colours | Per client, because two people may sit at different screens. |
| `chooser-session-lines` | session | on | The window chooser starts each session on a line of its own, so the sessions read as groups. With one session it changes nothing. |
| `pane-title-format` | window | `#T` and the compact path | The bar over each pane shows the program's title and the pane's directory, written as fish's `prompt_pwd` writes it: `~/C/pyterm`. The directory stays after the program ends. A fish pane shows the path twice, because fish puts it in the title too. |
| `full-screen` | client | off | On, one pane fills this terminal, with no status line. |
| `frames-in-flight`, `bytes-in-flight` | client | `5`, `4096` | Over a link where the terminal says what it read (ssh), the next frame waits once this many frames, or this many bytes, are on their way unread. With one, an animation runs one frame a round trip, 20 a second at 50 ms; five let it reach `frame-rate`. The bytes keep a slow link honest: a key's frame waits behind what is in the link, and a frame always goes when nothing is, so a big screen on a phone never builds a backlog. 0 is no limit. Lillecarl/pymux#825. |
| `window-status-format`, `window-status-current-format` | session | `#I:#W#F` | tmux's. `#F` ends with how far along a program in the window says it is (ConEmu's `OSC 9 ; 4`, which cargo, winget and many tools send): `42%`, `!42%` for an error, `~` while it cannot say, `=42%` paused. `#{window_progress}` is that alone, and `#{pane_progress}` one pane's. A client's terminal that draws a progress (kitty from 0.44, Ghostty from 1.2, iTerm2 from 3.6) is given the progress of the pane it looks at. |
| `status-max-rows` | session | `3` | A window list wider than its row wraps onto more rows, a whole window at a time, so a phone still shows every window. Past this many rows the rest are cut. `1` is tmux's single row. The tallest status line of any client watching a window comes off that window's size. |
| `status-right` | session | the clock and the date | A string holding `{{` is a jinja2 template, drawn for each client with that client's facts, so one line can differ between a phone and a desktop: `{% if client_width \| int < 100 %}…{% else %}…{% endif %}`. `client_fps` is the frames a second this client gets, and `client_window_fps` a window pane's own, empty while none draws; both over the last five seconds. `client_rate_down` and `client_rate_up` are bytes a second the same way. `client_bytes_down` is a plain count for scripts, and `{{ client_bytes_down \| int \| filesizeformat }}` sizes it for a person: `1.4 MB`. A template's time is `{{ now.strftime('%H:%M') }}`. Any number that moves costs an idle client a frame each `status-interval`, and the byte counts count that frame too. |

F12 is passthrough: every key goes to the pane, the prefix too, until the next F12. It is for a tmux or a pymux inside a pane. While it holds, the status line shows `PASSTHROUGH` before `status-right`, whatever that holds, and pymux sets the user variable `pymux_passthrough` on the person's terminal, so the terminal can release its own shortcuts as well. In kitty, one line per shortcut:

    map --when-focus-on var:pymux_passthrough kitty_mod+t

`bind-key -n <key> enter-mode passthrough` and `bind-key -T passthrough <key> leave-mode` move it to another key.
