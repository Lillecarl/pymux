# libpymux

Drive a pymux server from python.

```python
from libpymux import Server

server = Server.first()
pane = server.session.active_window.active_pane

pane.send_keys("echo hello")
print(pane.capture())
```

## What it is

The shape follows [libtmux](https://github.com/tmux-python/libtmux): a
server holds sessions, a session holds windows, a window holds panes.
Code written against libtmux reads the same way here.

The difference is underneath. libtmux runs the `tmux` binary for every
call and parses what it prints. libpymux talks to the server itself: the
wire of pymux is JSON on a unix socket, so there is no subprocess and no
shell quoting between the caller and the server.

**This is the way to drive pymux.** libtmux can drive it too, through a
`tmux` shim, and `tests/drive_with_libtmux.py` does that. But libtmux
only knows what tmux has, and pymux does more: the kitty keyboard
protocol, images, overlay panes, the pointer shape. Anything that tmux
has no command for is out of reach through that route, whatever pymux
can do. So the shim is a compatibility check, not a way to write a
program: it proves the tmux command line still behaves, and nothing
more.

## Reaching a server

```python
Server.first()                  # the one that is running
Server.list()                   # every one of this user
Server("/tmp/pymux.sock.me.7")  # a socket by name
```

`Server.list()` finds the sockets in the default place. A server started
with a socket path of its own is not in there, so name that path.

## What an object holds

Every object reads its fields once, through a format string, and keeps
what it read. It does not follow the server on its own.

```python
pane.width          # what it was when the pane was read
pane.refresh()      # read the fields again
server.panes        # the panes as they are now
```

So a loop that watches something reads the collection again each time.

## Commands

`Server.cmd()` runs any pymux command and gives back the output, the
errors and the exit code. Nothing the command line can do is out of
reach.

```python
server.cmd("list-windows")
server.cmd(["send-keys", "-t", "%1", "-l", "two words"])   # quoted for you
server.cmd("has-session -t nope", check=False)             # no exception
```

Pass a list and each argument is quoted. Pass a string and it goes as it
stands. A command that fails raises `CommandError`, which carries the
result, unless `check=False`.

## What it does not do

- **No event stream.** Nothing tells a caller that a pane exited or a
  window was renamed. Read the collection again.
- **One command for each connection.** The server closes the socket
  when a command ends, so every call opens a new one. That is what the
  command line does too.
- **A bare `;` cannot be an argument.** The server splits a command
  string on an unquoted semicolon, and quoting does not hide it. Send
  such text with `send_keys(..., literal=True)`, which does not go
  through that path.
- **One session for each server.** That is pymux, not this library.
  `Server.sessions` holds one entry, and `Server.session` is the one
  that matters.

## Tests

`tests/test_libpymux.py` answers with a socket that says what the test
tells it to say, so it runs anywhere. `tests/drive_with_pty.py` runs the
same library against a server that is really there, which is what
catches a format variable that went away or a command that changed its
options.
