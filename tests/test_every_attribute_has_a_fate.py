"""
Every attribute of a live session says what a hot upgrade does with it.

A class declares each attribute of its instances in `KEEP`: saved,
rebuilt or dropped (`pyte.keep.Keep`). This walks a running session on
a pyte screen, with a split window and a client attached, and fails on
two things:

- an attribute with no entry, so a field added without a decision
  cannot ship;
- an entry with no attribute, so a declaration cannot outlive the field
  it was written for.

A snapshot that missed one field would restore a session that looks
right and is not, and nothing else would notice. Lillecarl/pymux#399.
"""

from __future__ import annotations

import pytest
from prompt_toolkit.application.current import set_app
from pyte.keep import Keep
from test_a_session_on_a_pyte_screen import attached, settled, shows

from pymux.main import Pymux


@pytest.fixture
def pymux():
    mux = Pymux()
    mux.test_mode = True
    try:
        yield mux
    finally:
        for pane in list(mux.panes_by_id.values()):
            if not pane.process.is_terminated:
                pane.process.kill()


def declared(cls) -> dict:
    "Every entry of `KEEP` along the class's MRO, the nearest winning."
    keep: dict = {}
    for one in reversed(cls.__mro__):
        keep.update(one.__dict__.get("KEEP", {}))
    return keep


def attributes_of(one) -> set:
    """
    Every attribute set on this instance, in its `__dict__` or its slots,
    and every property its class declares a fate for: a property carries
    state the instance holds in another shape, such as a decoder's.
    """
    have = set(getattr(one, "__dict__", ()))
    for name in declared(type(one)):
        if isinstance(getattr(type(one), name, None), property):
            have.add(name)
    for cls in type(one).__mro__:
        slots = cls.__dict__.get("__slots__", ())
        for slot in (slots,) if isinstance(slots, str) else slots:
            if slot not in ("__dict__", "__weakref__") and hasattr(one, slot):
                have.add(slot)
    return have


def live_objects(pymux):
    "The objects a snapshot has to account for, one of each kind or more."
    yield pymux
    for session in pymux.sessions:
        yield session
        yield session.arrangement
        for window in session.arrangement.windows:
            yield window
            yield from window.splits
            for pane in window.panes:
                yield pane
                terminal = pane.terminal
                yield terminal
                yield terminal.terminal_control
                yield terminal.terminal_control.stream
                yield pane.process
                yield pane.process.backend
                yield pane.process.backend._reader
                screen = pane.screen
                yield screen
                yield screen.page
                yield screen.titles
                yield screen.colors
                yield screen.pointer_shapes
                yield screen.graphics
    for connection in pymux.connections:
        yield connection
        if connection.client_state is not None:
            yield connection.client_state


async def test_every_attribute_has_a_fate(pymux):
    async with pymux.running(), attached(pymux) as session:
        await shows(session, "$")
        client_state = pymux.connections[-1].client_state
        with set_app(client_state.app):
            pymux.handle_command("split-window -h")
        await settled(session)

        found = []
        seen_classes = set()
        for one in live_objects(pymux):
            cls = type(one)
            if cls in seen_classes:
                continue
            seen_classes.add(cls)
            keep = declared(cls)
            have = attributes_of(one)
            name = "%s.%s" % (cls.__module__, cls.__qualname__)
            for attribute in sorted(have - keep.keys()):
                found.append("%s.%s has no fate in KEEP" % (name, attribute))
            for attribute in sorted(keep.keys() - have):
                found.append("%s.%s is in KEEP and not on the object" % (name, attribute))
            # ptyhost does not import pyte, so it writes the three words
            # as strings; each one has to be one of them.
            for attribute, fate in sorted(keep.items()):
                if fate not in set(Keep):
                    found.append("%s.%s has the fate %r, which is not a Keep" % (name, attribute, fate))

        assert not found, "\n".join(found)
