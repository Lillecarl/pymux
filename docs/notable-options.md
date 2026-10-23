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
| `window-codec` | window | `lossless` | Exact pixels. `vp9` sends far fewer bytes over a slow link, at some sharpness, and smooths video. |
| `wayland-scale` | window | `auto` | A window draws at the density of the client's cells. |
| `window-scroll` | client | `smooth` | One scroll event moves one cell of pixels, which follows a trackpad. `notch` is a mouse wheel's step. The terminal does not say which device it has. |

## The person's terminal

| option | scope | default | why |
| --- | --- | --- | --- |
| `mouse` | session | on | Unlike tmux: a click selects a pane and reaches the program. |
| `extended-keys` | session | `on` | Every key a terminal can name reaches the pane. |
| `status-keys` | session | from `$VISUAL`, else `$EDITOR` | The keys of the `:` line and of `command-prompt`: `emacs`, `vi`, or `helix`. An editor whose name holds `vi` gives `vi`, as tmux does; `hx` or `helix` gives `helix`. With `vi` or `helix` the first Escape leaves insert mode and the second closes the line, and the line shows its mode. Lillecarl/pymux#749. |
| `mode-keys` | session | from the editor, as above | The keys of copy mode: `emacs`, `vi`, or `helix`. With `helix` the copy is Helix: its moves, selections, `/` search and edits, so a person can trim or change the text before copying it. `y` or Enter copies and leaves, `q` or Escape in normal mode leaves, and `Alt-r` takes a new copy, refused while there are edits. Leaving drops the edits. The corner shows the mode, a search as it is typed, and whether the copy is edited. Lillecarl/pymux#751. |
| `key-profile` | session | `kitty` | Where terminals split on a key, a legacy program gets what the client's own terminal sends. This names the terminal for a client whose terminal does not name itself. |
| `theme` | client | the theme nearest the terminal's own colours | Per client, because two people may sit at different screens. |
| `chooser-session-lines` | session | on | The window chooser starts each session on a line of its own, so the sessions read as groups. With one session it changes nothing. |
| `full-screen` | client | off | On, one pane fills this terminal, with no status line. |
| `status-max-rows` | session | `3` | A window list wider than its row wraps onto more rows, a whole window at a time, so a phone still shows every window. Past this many rows the rest are cut. `1` is tmux's single row. The tallest status line of any client watching a window comes off that window's size. |
| `status-fps` | session | off | On, the status line shows the frames a second this client gets, over the last five seconds, before the clock. Off because the number moves: an idle client then gets a frame each `status-interval`, and the number counts that frame too. `#{client_rate_down}` shows the bytes the same way. |

F12 is passthrough: every key goes to the pane, the prefix too, until the next F12. It is for a tmux or a pymux inside a pane. While it holds, the status line shows `PASSTHROUGH` before `status-right`, whatever that holds, and pymux sets the user variable `pymux_passthrough` on the person's terminal, so the terminal can release its own shortcuts as well. In kitty, one line per shortcut:

    map --when-focus-on var:pymux_passthrough kitty_mod+t

`bind-key -n <key> enter-mode passthrough` and `bind-key -T passthrough <key> leave-mode` move it to another key.
