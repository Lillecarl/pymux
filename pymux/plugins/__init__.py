"""
Commands written as plugins: every module in this directory, then
every module in `$XDG_CONFIG_HOME/pymux/plugins`.

A plugin has the shape of a command module: a function that takes the
server and the parsed arguments, and a `register(subparsers)` that
calls `add_command` for it. The tree mounts each one after the
commands of pymux, so a plugin command runs from a key binding, the
command bar, a configuration file and the socket, and the shell
completes it. Lillecarl/pyterm#531.

A module may name an event plugin too, as `plugin`, and the server
runs it: `pymux.plugin_api` says how. Lillecarl/pyterm#982.

An installed distribution adds a module through the entry-point group
`pymux.plugins`: the entry point's name is the plugin's, and its value
names the module. That is how a plugin with dependencies of its own
ships without making them pymux's. Lillecarl/pyterm#983.

A plugin with the name of one found before it is not loaded: this
directory, then the person's, then the entry points.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import pkgutil
import sys
from collections.abc import Iterator, Mapping
from types import ModuleType
from typing import TYPE_CHECKING

from pymux.config import plugin_dir
from pymux.log import logger

if TYPE_CHECKING:
    from pymux.commands import CommandParser

#: The entry-point group an installed plugin names its module in.
GROUP = "pymux.plugins"


def register(subparsers: argparse._SubParsersAction[CommandParser], environ: Mapping[str, str] | None = None) -> None:
    """
    Mount every plugin on the tree.

    **A plugin that fails is logged and left out**, with every command
    it mounted before it failed. The tree holds every command, so one
    broken file would otherwise take all of them with it --
    `kill-server` included. That covers a name another command already
    has, which argparse refuses with a ValueError, and a target spelled
    without `add_target`.
    """
    from pymux.commands import refuse_undeclared_targets

    for name, module in modules(environ):
        mount = getattr(module, "register", None)
        if mount is None:
            continue
        before = set(subparsers.choices)
        try:
            mount(subparsers)
            refuse_undeclared_targets({name: p for name, p in subparsers.choices.items() if name not in before})
        except Exception:
            _unmount(subparsers, set(subparsers.choices) - before)
            logger.exception("pymux: the plugin %s failed to load", name)


def modules(environ: Mapping[str, str] | None = None) -> Iterator[tuple[str, ModuleType]]:
    """
    Every plugin module, by name, imported once per process. A module
    that does not import is logged and left out.

    A module may hold commands (`register`), an event plugin (`plugin`,
    which `pymux.plugin_api` says), or both.
    """
    # `iter_modules` already yields a name once, from the first directory.
    seen: set[str] = set()
    for plugin in pkgutil.iter_modules([*__path__, plugin_dir(environ)]):
        seen.add(plugin.name)
        try:
            module = _module(plugin)
        except Exception:
            logger.exception("pymux: the plugin %s failed to load", plugin.name)
            continue
        yield plugin.name, module

    for entry in importlib.metadata.entry_points(group=GROUP):
        if entry.name in seen:
            logger.warning("pymux: the plugin %s of %s is not loaded: another has its name", entry.name, entry.value)
            continue
        seen.add(entry.name)
        try:
            module = entry.load()
            if not isinstance(module, ModuleType):
                raise TypeError("the entry point %s names %r and not a module" % (entry.value, module))
        except Exception:
            logger.exception("pymux: the plugin %s failed to load", entry.name)
            continue
        yield entry.name, module


def _unmount(subparsers: argparse._SubParsersAction[CommandParser], names: set[str]) -> None:
    for name in names:
        del subparsers.choices[name]
    subparsers._choices_actions = [action for action in subparsers._choices_actions if action.dest not in names]


def _module(plugin: pkgutil.ModuleInfo) -> ModuleType:
    name = "%s.%s" % (__name__, plugin.name)
    if name in sys.modules:
        return sys.modules[name]

    spec = plugin.module_finder.find_spec(name, None)
    if spec is None or spec.loader is None:
        raise ImportError("no loader for %s" % name, name=name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module
