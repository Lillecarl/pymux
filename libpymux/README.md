# libpymux

Drive a pymux server from python, with anyio.

```python
from libpymux import Server

server = await Server.first()
window = await (await server.session()).active_window()
pane = await window.active_pane()

await pane.send_keys("echo hello")
print(await pane.capture())
```

## What it is

A server holds sessions, a session holds windows, a window holds panes.
libpymux talks to the server itself: the wire of pymux is JSON on a unix
socket, so there is no subprocess and no shell quoting between the
caller and the server.

**Every read that asks the server is awaited.** A field the object
already holds, such as `pane.width`, is a property. Anything that goes
to the server, such as `server.panes()`, is a method you await.

**Its API is its own.** It follows what pymux can do, not what tmux can.
For a tmux-shaped API with no `await`, use libtmux itself: it drives
pymux as it drives tmux, with `libtmux.Server(tmux_bin="pymux")`. That
route reaches only what tmux has a command for, and
`tests/drive_with_libtmux.py` checks that it still works.

**It carries nothing of the terminal.** This is a distribution of its
own, and what it asks for is platformdirs, anyio and ormsgpack -- the
room a server binds its socket in, the async it speaks, and the packets
it answers. No prompt_toolkit, no ptterm, no parser: a program that
drives a server does not install one. pymux depends on this package,
never the other way round.

## Reaching a server

```python
await Server.first()  # the one that is running
await Server.list()  # every one of this user
Server("/tmp/pymux.sock.me.7")  # a socket by name
```

`Server.list()` finds the sockets in the default place. A server started
with a socket path of its own is not in there, so name that path.

## What an object holds

Every object reads its fields once, through a format string, and keeps
what it read. It does not follow the server on its own.

```python
pane.width  # what it was when the pane was read
await pane.refresh()  # read the fields again
await server.panes()  # the panes as they are now
```

So a loop that watches something reads the collection again each time,
or waits with `pane.wait_for_change()`.

## Commands

`Server.cmd()` runs any pymux command and gives back the output, the
errors and the exit code. Nothing the command line can do is out of
reach.

```python
await server.cmd("list-windows")
await server.cmd(["send-keys", "-t", "%1", "-l", "two words"])  # quoted for you
await server.cmd("has-session -t nope", check=False)  # no exception
```

Pass a list and each argument is quoted. Pass a string and it goes as it
stands. A command that fails raises `CommandError`, which carries the
result, unless `check=False`.

## How it is built

`connection.py` turns a command into bytes and reads the answer back
without I/O, and `Connection` only moves the bytes. `operations.py`
holds every command an object sends, as a generator that yields what it
needs and returns rows, and `objects.py` runs them. So the commands and
the parsing are testable with no socket at all.

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
- **As many sessions as a person makes.** A server holds every session
  it started, so `await server.sessions()` is the list and
  `await server.session()` is the first of them.

## Tests

`tests/test_libpymux.py` answers with a socket that says what the test
tells it to say, so it runs anywhere. `tests/drive_with_pty.py` runs the
same library against a server that is really there, which is what
catches a format variable that went away or a command that changed its
options.
