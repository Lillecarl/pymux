"""
A running server keeps an fd open on its own build, so Nix's collector
sees the build as a root. On macOS `lsof` is all the collector reads,
and a venv has no file of its own that is ever open. Lillecarl/pyterm#1023.
"""

from __future__ import annotations

import os
import sys

from pymux.core.main import hold_the_build


def test_the_server_holds_its_prefix_and_every_import_directory():
    held = hold_the_build()
    try:
        inodes = {(os.fstat(fd).st_dev, os.fstat(fd).st_ino) for fd in held}
        for path in [sys.prefix, *(p for p in sys.path if p and os.path.isdir(p))]:
            stat = os.stat(path)
            assert (stat.st_dev, stat.st_ino) in inodes, path
    finally:
        for fd in held:
            os.close(fd)


def test_no_program_the_server_starts_inherits_the_hold():
    held = hold_the_build()
    try:
        assert held
        assert not any(os.get_inheritable(fd) for fd in held)
    finally:
        for fd in held:
            os.close(fd)
