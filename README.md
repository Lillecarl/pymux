# pymux

> This repository is a copy. Work on pymux happens in
> [pyterm](https://github.com/Lillecarl/pyterm), which exports its `pymux/`
> directory here on every push. Send changes there, and issues to
> [Lillecarl/pymux](https://github.com/Lillecarl/pymux/issues).
> The docs are at <https://lillecarl.github.io/pymux/>.

<!-- The lines above are pyterm's nix/shared/readme-head.md, written by sync-sources. -->

A terminal multiplexer, like tmux, in Python, on top of
[prompt_toolkit].

![pymux](images/pymux.png)

    nix run github:Lillecarl/pymux

Running `pymux` with no arguments attaches to the newest server that is
running, and starts one when none is, as `tmux` does. Any other command, such as `split-window`, goes to the running
server, the way tmux does it. `pymux --help` lists the modes and the
options. pyterm's README shows how to configure it with home-manager.

## Compared to tmux

The default keys, the commands and a simple configuration file are the
ones tmux has, so the hands that know tmux know pymux. It is not meant
to be a clone, and it does more than tmux in several places:

- The command line has a completion menu and
  [fish-style](https://fishshell.com/) suggestions.
- Emacs and Vi keys for the command line and the copy buffer, from
  prompt_toolkit.
- Search in the copy buffer highlights while it searches.
- Every pane has its own title bar.
- Each client of a session can watch a different window, and each one
  then uses its whole terminal.
- Themes (`set-option theme`), true colour, and wide characters, title
  bars included.
- A pane can show a GUI program, through the Wayland compositor of
  `pysmithay`, on Linux.
- A server on another machine: `pymux -S ssh://host/path` runs the
  panes there and draws them here.

`docs/tmux-compat-decisions.md` lists where pymux differs from tmux on
purpose, and `docs/notable-options.md` the options it adds.
`docs/window-pane-support.md` records which terminals show a window
pane, and with which features.

![Multiple clients](images/multiple-clients.png)

## Driving pymux from Python

`libpymux` drives a running server over its socket, with the object
model that libtmux uses: a server holds sessions, a session holds
windows, a window holds panes. It is a distribution of its own, in
`libpymux/`, and it carries nothing of the terminal: a program that
drives a server installs a socket, a JSON message and platformdirs.

```python
from libpymux import Server

server = Server.first()
pane = server.session.active_window.active_pane
pane.send_keys("echo hello")
print(pane.capture())
```

`libpymux/README.md` says more. pymux also takes the tmux command line,
so libtmux itself can drive it through a `tmux` shim, as
`tests/drive_with_libtmux.py` does. That route reaches only what tmux
has, so it is a compatibility check and not the way to write a program.

## Configuring

pymux reads `$XDG_CONFIG_HOME/pymux/pymux.conf`, or `~/.pymux.conf`,
or the file `-f` names. It holds commands, as typed at the command
line. `examples/example-config.conf` is one.

A Python file in `$XDG_CONFIG_HOME/pymux/plugins` adds commands. It has
the shape of the example plugin, `pymux/plugins/count_panes.py`: a
function that takes the server and the parsed arguments, and a
`register(subparsers)` that calls `add_command` for it. A plugin that
fails to load is logged and left out.

## When it crashes

pymux writes the stack trace to a file named like `/tmp/pymux.crash-*`.
A server also writes its stacks to `~/.local/state/pymux/` on
`SIGUSR1`, which helps when it hangs. Open an issue with either.

## Thanks

- [Jonathan Slenders](https://github.com/jonathanslenders), who wrote
  prompt_toolkit and the first pymux.
- [pyte](https://github.com/selectel/pyte), for the VT100 parser this
  started from.
- [wcwidth](https://github.com/jquast/wcwidth), for the width of a
  character.
- [tmux](https://github.com/tmux/tmux), for the inspiration.

[prompt_toolkit]: https://github.com/prompt-toolkit/python-prompt-toolkit

<!-- The lines below are pyterm's nix/shared/readme-tail.md, written by sync-sources. -->

## Building

    nix build github:Lillecarl/pymux    # from this repository alone
    nix build --file . pymux            # in a checkout of pyterm

The suites that judge it run in pyterm, as `checks.pymux-*`.

## The collection

pymux is one source of [pyterm](https://github.com/Lillecarl/pyterm), and
each source claims one job:

| source | job |
| --- | --- |
| [pyte](https://lillecarl.github.io/pyte/) | parse, and hold a screen |
| [ptyhost](https://lillecarl.github.io/ptyhost/) | run a program on a pty, and carry its bytes |
| [ptterm](https://lillecarl.github.io/ptterm/) | draw one terminal, with prompt-toolkit |
| [txterm](https://lillecarl.github.io/txterm/) | draw one terminal, with Textual |
| [pymux](https://lillecarl.github.io/pymux/) | arrange several of them |
| [pysmithay](https://lillecarl.github.io/pysmithay/) | a headless Wayland compositor, for a GUI program in a pane |
| [pyterm-pytest](https://lillecarl.github.io/pyterm-pytest/) | the test equipment the suites share |
| [deferimport](https://lillecarl.github.io/deferimport/) | make imports lazy, without a keyword |
| [ptkhx](https://lillecarl.github.io/ptkhx/) | Helix's modal editing, for a prompt_toolkit buffer |

[pyterm's site](https://lillecarl.github.io/pyterm/) holds every source's
docs.
