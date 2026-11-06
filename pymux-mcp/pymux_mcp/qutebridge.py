"""
A socket inside qutebrowser: an agent runs Python or a qutebrowser
command on the browser's own loop, and reads back the value, what it
printed, and every message the browser showed meanwhile.
Lillecarl/pymux#942.

qutebrowser runs its config.py inside the browser, so the snippet that
`loader()` writes, put in a config.py, is the whole installation. A
qutebrowser pymux starts gets it without asking.

This file runs inside qutebrowser and imports nothing of pymux-mcp. It
reaches qutebrowser through `import_module`, because only the browser
has it to import.

**A thread owns the socket, and Qt's main thread runs each request.**
config.py runs before qutebrowser makes its QApplication, and a
QLocalServer made then listens but never accepts: its notifier has no
event dispatcher, measured on qutebrowser 3.7.0. A queued signal needs
the loop only when it is delivered.

**config.py runs in every qutebrowser process**, including the
short-lived one that `qutebrowser ':open x'` starts to hand its command
to the running browser. So `serve` leaves alone a socket that answers.

**The socket runs any Python as the person**, in a browser that holds
their logins. It is mode 0600: only the person's own programs reach it,
and those can read the profile already.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import socket
import sys
import threading
import traceback
from importlib import import_module
from typing import Any, Final

__all__ = ["MODULE", "SOCKET", "default_path", "loader", "register", "serve", "stop"]

#: The socket's name in qutebrowser's data directory.
SOCKET: Final = "pymux-bridge.sock"
#: The name the loader keeps this module under, so a second config.py
#: run, `:config-source`, finds the bridge already serving.
MODULE: Final = "pymux_qutebridge"
#: How long a request waits for the main thread, which a modal prompt holds.
ANSWER_TIMEOUT: Final = 60.0
#: The most one request may be.
REQUEST_LIMIT: Final = 1 << 20


def loader(path: str = __file__, start: bool = True, socket_path: str | None = None) -> str:
    """
    Python for a config.py that loads the bridge in `path` and registers
    `:pymux-bridge`; and serves it at once when `start`, at `socket_path`
    or in qutebrowser's data directory.
    """
    return (
        "import importlib.util as _util, sys as _sys\n"
        "if %(module)r not in _sys.modules:\n"
        "    _spec = _util.spec_from_file_location(%(module)r, %(path)r)\n"
        "    _bridge = _util.module_from_spec(_spec)\n"
        "    _sys.modules[%(module)r] = _bridge\n"
        "    _spec.loader.exec_module(_bridge)\n"
        "    _bridge.register(%(socket)r)\n"
        "    if %(start)r:\n"
        "        _bridge.serve(%(socket)r)\n"
    ) % {"module": MODULE, "path": path, "start": start, "socket": socket_path}


def default_path() -> str:
    return os.path.join(import_module("qutebrowser.utils.standarddir").data(), SOCKET)


#: The socket this process serves, while it does.
_listener: socket.socket | None = None


def serve(path: str | None = None) -> str | None:
    """
    Answer on the socket at `path`, by default in qutebrowser's data
    directory, and answer the path; None when a live bridge has it or it
    cannot listen there.
    """
    global _listener
    path = path or default_path()
    if _listener is not None:
        return path
    if _answers(path):
        return None
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    listener = socket.socket(socket.AF_UNIX)
    try:
        listener.bind(path)
        os.chmod(path, 0o600)
        listener.listen()
    except OSError as error:
        # Raised in config.py, this stops qutebrowser at a dialog, and a
        # headless one waits there for ever.
        listener.close()
        sys.stderr.write("pymux bridge: cannot listen at %s: %s\n" % (path, error))
        return None
    _listener = listener
    threading.Thread(target=_accept, args=(listener, _runner()), name="pymux-bridge", daemon=True).start()
    return path


def stop() -> None:
    "Stop answering. A request already running finishes."
    global _listener
    if _listener is None:
        return
    path = _listener.getsockname()
    # On Linux a close leaves `accept` in the other thread blocked; a
    # shutdown wakes it.
    with contextlib.suppress(OSError):
        _listener.shutdown(socket.SHUT_RDWR)
    _listener.close()
    _listener = None
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def register(path: str | None = None) -> None:
    """
    Register `:pymux-bridge`, which a person types to let an agent in, or
    to shut it out; it serves at `path`, or in qutebrowser's data directory.
    """
    cmdutils = import_module("qutebrowser.api.cmdutils")
    message = import_module("qutebrowser.api.message")

    @cmdutils.register(name="pymux-bridge")
    @cmdutils.argument("state", choices=["on", "off", "status"])
    def pymux_bridge(state: str = "status") -> None:
        """
        Let an agent run Python and commands in this browser, through pymux-mcp.

        Args:
            state: `on` opens the socket, `off` closes it, `status` says which.
        """
        if state == "on" and serve(path) is None:
            raise cmdutils.CommandError(
                "cannot serve at %s: another qutebrowser does, or the path is too long" % (path or default_path(),)
            )
        if state == "off":
            stop()
        if _listener is None:
            message.info("pymux bridge: off")
        else:
            message.info("pymux bridge: on, at %s" % (_listener.getsockname(),))


def _answers(path: str) -> bool:
    with socket.socket(socket.AF_UNIX) as probe:
        try:
            probe.connect(path)
        except OSError:
            return False
    return True


class _Call:
    def __init__(self, request: dict[str, Any]) -> None:
        self.request = request
        self.answer: dict[str, Any] = {}
        self.done = threading.Event()


def _runner() -> Any:
    "A QObject of the main thread whose `call` signal, emitted anywhere, runs a call there."
    core = import_module("qutebrowser.qt.core")

    class Runner(core.QObject):
        call = core.pyqtSignal(object)

        def __init__(self) -> None:
            super().__init__()
            self.call.connect(_run)

    return Runner()


def _accept(listener: socket.socket, runner: Any) -> None:
    while True:
        try:
            connection, _ = listener.accept()
        except OSError:
            return  # `stop` closed it.
        threading.Thread(target=_answer, args=(connection, runner), daemon=True).start()


def _answer(connection: socket.socket, runner: Any) -> None:
    with connection:
        raw = b""
        while not raw.endswith(b"\n") and len(raw) < REQUEST_LIMIT:
            chunk = connection.recv(65536)
            if not chunk:
                break
            raw += chunk
        try:
            call = _Call(json.loads(raw))
        except ValueError as error:
            answer: dict[str, Any] = {"error": "not a JSON request: %s" % (error,)}
        else:
            runner.call.emit(call)
            if call.done.wait(ANSWER_TIMEOUT):
                answer = call.answer
            else:
                answer = {"error": "qutebrowser's main thread did not answer in %gs" % (ANSWER_TIMEOUT,)}
        connection.sendall(json.dumps(answer, default=repr).encode() + b"\n")


#: What a request's Python sees, kept between requests so an agent can
#: define a helper once.
NAMESPACE: dict[str, Any] = {}


def _namespace() -> dict[str, Any]:
    if not NAMESPACE:
        objreg = import_module("qutebrowser.utils.objreg")
        NAMESPACE.update(
            {
                "__builtins__": __builtins__,
                "objreg": objreg,
                "config": import_module("qutebrowser.config.config"),
                "message": import_module("qutebrowser.utils.message"),
                "command": _command,
                "window": objreg.last_visible_window,
                "tab": lambda: objreg.last_visible_window().tabbed_browser.widget.currentWidget(),
            }
        )
    return NAMESPACE


def _run(call: _Call) -> None:
    "On the main thread: run one request, and record what it answered and what the browser said."
    bridge = import_module("qutebrowser.utils.message").global_bridge
    messages: list[dict[str, str]] = []

    def shown(info: Any) -> None:
        messages.append({"level": info.level.name, "text": info.text})

    bridge.show_message.connect(shown)
    refused = import_module("qutebrowser.commands.cmdexc").Error
    out = io.StringIO()
    answer: dict[str, Any] = {"value": None, "error": None}
    try:
        with contextlib.redirect_stdout(out):
            if "command" in call.request:
                _command(call.request["command"])
            else:
                answer["value"] = _python(call.request["python"])
    except refused as error:
        # What qutebrowser would show in its status bar, without the
        # traceback of its own command machinery.
        answer["error"] = str(error)
    except BaseException:
        answer["error"] = traceback.format_exc(limit=-8)
    finally:
        bridge.show_message.disconnect(shown)
        answer["stdout"] = out.getvalue()
        answer["messages"] = messages
        call.answer = answer
        call.done.set()


def _python(code: str) -> Any:
    "Run `code` as a module, and answer its last line's value when that line is an expression."
    tree = ast.parse(code, "<agent>")
    last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
    namespace = _namespace()
    exec(compile(tree, "<agent>", "exec"), namespace)
    if last is None:
        return None
    value = eval(compile(ast.Expression(last.value), "<agent>", "eval"), namespace)
    try:
        json.dumps(value)
    except TypeError, ValueError:
        return repr(value)
    return value


def _command(text: str) -> None:
    "Run a qutebrowser command line in the window last shown, as if typed after `:`."
    runners = import_module("qutebrowser.commands.runners")
    objreg = import_module("qutebrowser.utils.objreg")
    runners.CommandRunner(objreg.last_visible_window().win_id).run(text.removeprefix(":"))


if __name__ == "__main__":
    # `--off`: load and register `:pymux-bridge`, and serve only once a person types it.
    sys.stdout.write(loader(start="--off" not in sys.argv))
