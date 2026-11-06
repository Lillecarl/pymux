"""
The bridge into a person's own Glide: a glide.ts fragment that adds the ex
command `:pymux_bridge on|off|status`. `on` opens Glide's debugger on a Unix
socket at `default_path()`, where `gecko.chrome` with no `name` reaches it.

`python -m pymux_mcp.glidebridge [--off] [--socket PATH]` prints the fragment
for glide.ts, with the socket's path written in: a Glide started from the Dock
does not see the shell's `XDG_STATE_HOME`. `--off` leaves the bridge closed
until the person types `:pymux_bridge on`.

The socket is the person's to grant. Whoever opens it runs any code as them,
in the browser that holds their logins, so the bridge makes it mode 0600.

glide.ts reaches the debugger through `window.ChromeUtils`: Glide's config
sandbox has no `Components`, and its `window` is a privileged document.
Measured on Glide 0.1.65a.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Final

__all__ = ["SOCKET", "default_path", "script"]

#: The socket's name, under pymux's state directory.
SOCKET: Final = "glide.sock"


def default_path() -> Path:
    "Where the person's own Glide serves its debugger once the bridge is on."
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "pymux" / SOCKET


#: `%(socket)s` and `%(start)s` are JSON.
_SCRIPT: Final = """\
// pymux's bridge into this Glide, from pymux-mcp's glidebridge.py.
// `:pymux_bridge on` opens the debugger on the socket below, where pymux-mcp's
// `gecko.chrome` runs code in this browser with full privilege; `off` closes it.
{
  const SOCKET = %(socket)s;
  const devtools = () => {
    const { require } = window.ChromeUtils.importESModule("resource://devtools/shared/loader/Loader.sys.mjs");
    return {
      server: require("resource://devtools/server/devtools-server.js").DevToolsServer,
      SocketListener: require("resource://devtools/shared/security/socket.js").SocketListener,
      auth: require("resource://devtools/shared/security/auth.js"),
    };
  };
  // On the server, a module singleton, so a config reload still finds it.
  const listening = () => devtools().server.pymuxBridge;

  const on = async () => {
    const { server, SocketListener, auth } = devtools();
    if (server.pymuxBridge) return;
    await window.IOUtils.makeDirectory(SOCKET.slice(0, SOCKET.lastIndexOf("/")), { ignoreExisting: true });
    await window.IOUtils.remove(SOCKET, { ignoreAbsent: true });
    // Without it the server refuses a listener.
    glide.prefs.set("devtools.debugger.remote-enabled", true);
    server.init();
    server.registerAllActors();
    server.allowChromeProcess = true;
    // The server otherwise ends once its last connection closes, and its listener with it.
    const keptAlive = server.keepAlive;
    server.keepAlive = true;
    // The socket is the grant: no prompt for each connection.
    const authenticator = new (auth.Authenticators.get().Server)();
    authenticator.authenticate = () => auth.AuthenticationResult.ALLOW;
    const listener = new SocketListener(server, { portOrPath: SOCKET, authenticator });
    listener.open();
    // Gecko makes it 0666; whoever connects runs code as the person.
    await window.IOUtils.setPermissions(SOCKET, 0o600);
    server.pymuxBridge = { listener, keptAlive };
  };

  const off = async () => {
    const { server } = devtools();
    const bridge = server.pymuxBridge;
    if (!bridge) return;
    bridge.listener.close();
    server.keepAlive = bridge.keptAlive;
    delete server.pymuxBridge;
    glide.prefs.clear("devtools.debugger.remote-enabled");
    await window.IOUtils.remove(SOCKET, { ignoreAbsent: true });
  };

  const say = (text) => glide.excmds.execute(`echo ${JSON.stringify(text)}`);

  glide.excmds.create(
    { name: "pymux_bridge", description: "Let pymux-mcp into this browser: on, off or status" },
    async ({ args_arr }) => {
      const [wanted = "status"] = args_arr;
      if (wanted === "on") await on();
      else if (wanted === "off") await off();
      else if (wanted !== "status") throw new Error(`pymux_bridge: on, off or status, not ${wanted}`);
      await say(listening() ? `pymux bridge: on at ${SOCKET}` : "pymux bridge: off");
    },
  );

  if (%(start)s) on().catch(error => console.error("pymux bridge:", error));
}
"""


def script(socket: Path, start: bool) -> str:
    "The glide.ts fragment, serving at `socket`, open at once when `start`."
    return _SCRIPT % {"socket": json.dumps(str(socket)), "start": json.dumps(start)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="python -m pymux_mcp.glidebridge", description=__doc__.split("\n\n")[0])
    parser.add_argument("--off", action="store_true", help="Closed until `:pymux_bridge on`.")
    parser.add_argument("--socket", type=Path, default=default_path(), help="Where to serve.")
    arguments = parser.parse_args()
    print(script(arguments.socket, start=not arguments.off), end="")
