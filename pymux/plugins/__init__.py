"""
Commands written as plugins: every module in this directory, then
every module in `$XDG_CONFIG_HOME/pymux/plugins`.

A plugin has the shape of a command module: a function that takes the
server and the parsed arguments, and a `register(subparsers)` that
calls `add_command` for it. The tree mounts each one after the
commands of pymux, so a plugin command runs from a key binding, the
command bar, a configuration file and the socket, and the shell
completes it. Lillecarl/pymux#529.

A person's plugin with the name of one shipped here is not loaded:
the first directory wins.
"""

from __future__ import annotations

import argparse
import importlib.util
import pkgutil
import sys
from types import ModuleType
from typing import TYPE_CHECKING

from pymux.config import plugin_dir
from pymux.log import logger

if TYPE_CHECKING:
    from pymux.commands import CommandParser


def register(subparsers: argparse._SubParsersAction[CommandParser], environ=None) -> None:
    """
    Mount every plugin on the tree.

    **A plugin that fails is logged and left out.** The tree holds
    every command, so one broken file would otherwise take all of them
    with it -- `kill-server` included. That covers a name another
    command already has, which argparse refuses with a ValueError.
    """
    for plugin in pkgutil.iter_modules([*__path__, plugin_dir(environ)]):
        try:
            _module(plugin).register(subparsers)
        except Exception:
            logger.exception("pymux: the plugin %s in %s failed to load", plugin.name, plugin.module_finder.path)


def _module(plugin: pkgutil.ModuleInfo) -> ModuleType:
    name = "%s.%s" % (__name__, plugin.name)
    if name in sys.modules:
        return sys.modules[name]

    spec = plugin.module_finder.find_spec(name, None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module
