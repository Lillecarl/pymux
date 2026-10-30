"""
The desktop portal a server serves the programs in window panes, on a
real dbus-daemon, read back by a real D-Bus client. `checks.pymux-wayland`
runs this and `checks.pymux-unit` skips it. Lillecarl/pymux#812.

`test_color_scheme.py` judges which scheme the server decides on.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from subprocess import DEVNULL, PIPE

import anyio
import pytest
from anyio.streams.buffered import BufferedByteReceiveStream
from session import once

pytest.importorskip("dbus_fast")

from dbus_fast import Message, MessageType, Variant
from dbus_fast.aio import MessageBus

from pymux.enums import WaylandPortal
from pymux.main import Pymux
from pymux.options import ALL_SERVER_OPTIONS
from pymux.portal import ColorScheme
from pymux.portal_settings import APPEARANCE, BUS_NAME, COLOR_SCHEME, INTERFACE, OBJECT_PATH

CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <servicedir>{services}</servicedir>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

#: What a desktop installs for xdg-desktop-portal, so the bus starts it.
REAL_PORTAL = """[D-BUS Service]
Name=org.freedesktop.portal.Desktop
Exec=/bin/false
"""


@asynccontextmanager
async def a_host_bus(tmp_path, monkeypatch, real_portal=False):
    "A dbus-daemon that stands for the host's session bus."
    services = tmp_path / "services"
    services.mkdir()
    if real_portal:
        (services / "org.freedesktop.portal.Desktop.service").write_text(REAL_PORTAL)
    config = tmp_path / "bus.conf"
    config.write_text(CONFIG.format(socket=tmp_path / "host-bus", services=services))
    argv = ["dbus-daemon", "--config-file=%s" % config, "--nofork", "--nopidfile", "--print-address=1"]
    async with await anyio.open_process(argv, stdin=DEVNULL, stdout=PIPE, stderr=None) as daemon:
        try:
            assert daemon.stdout is not None
            with anyio.fail_after(10):
                address = (await BufferedByteReceiveStream(daemon.stdout).receive_until(b"\n", 4096)).decode()
            monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", address)
            yield address
        finally:
            daemon.terminate()


@asynccontextmanager
async def serving(tmp_path, mode):
    "A server whose portal holds its name."
    pymux = Pymux()
    pymux.socket_name = str(tmp_path / "pymux.sock.test")
    pymux.options.wayland_portal = mode
    async with pymux.running():
        await once(lambda: pymux.portal._settings, 10.0, "the portal never served")
        yield pymux


@asynccontextmanager
async def a_client(address):
    bus = await MessageBus(bus_address=address).connect()
    try:
        yield bus
    finally:
        bus.disconnect()


async def call(bus, member, signature, body):
    return await bus.call(
        Message(
            destination=BUS_NAME,
            path=OBJECT_PATH,
            interface=INTERFACE,
            member=member,
            signature=signature,
            body=body,
        )
    )


async def test_a_private_bus_answers_the_scheme(tmp_path):
    async with serving(tmp_path, WaylandPortal.PRIVATE) as pymux:
        pymux.portal.scheme_is(ColorScheme.DARK)
        async with a_client(pymux.portal.private_address) as bus:
            reply = await call(bus, "ReadOne", "ss", [APPEARANCE, COLOR_SCHEME])

    assert reply.message_type == MessageType.METHOD_RETURN, reply.body
    assert reply.body == [Variant("u", ColorScheme.DARK)]


async def test_read_answers_inside_a_second_variant(tmp_path):
    "Version 1's shape, which the programs that call it unwrap twice."
    async with serving(tmp_path, WaylandPortal.PRIVATE) as pymux:
        pymux.portal.scheme_is(ColorScheme.LIGHT)
        async with a_client(pymux.portal.private_address) as bus:
            reply = await call(bus, "Read", "ss", [APPEARANCE, COLOR_SCHEME])

    assert reply.body == [Variant("v", Variant("u", ColorScheme.LIGHT))]


@pytest.mark.parametrize(("patterns", "found"), [([], True), (["org.freedesktop.*"], True), (["org.gnome.*"], False)])
async def test_read_all_takes_a_pattern(tmp_path, patterns, found):
    async with serving(tmp_path, WaylandPortal.PRIVATE) as pymux:
        async with a_client(pymux.portal.private_address) as bus:
            reply = await call(bus, "ReadAll", "as", [patterns])

    assert (APPEARANCE in reply.body[0]) == found


async def test_a_key_nobody_serves_is_not_found(tmp_path):
    async with serving(tmp_path, WaylandPortal.PRIVATE) as pymux:
        async with a_client(pymux.portal.private_address) as bus:
            reply = await call(bus, "ReadOne", "ss", [APPEARANCE, "accent-color"])

    assert reply.message_type == MessageType.ERROR
    assert reply.error_name == "org.freedesktop.portal.Error.NotFound"


async def test_a_change_reaches_a_program_while_it_runs(tmp_path):
    async with serving(tmp_path, WaylandPortal.PRIVATE) as pymux:
        async with a_client(pymux.portal.private_address) as bus:
            heard = []
            bus.add_message_handler(
                lambda message: heard.append(message) if message.member == "SettingChanged" else None
            )
            await bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=["type='signal',interface='%s',member='SettingChanged'" % (INTERFACE,)],
                )
            )

            pymux.portal.scheme_is(ColorScheme.DARK)
            await once(lambda: heard, 10.0, "no SettingChanged arrived")

    assert heard[0].body == [APPEARANCE, COLOR_SCHEME, Variant("u", ColorScheme.DARK)]


async def test_off_stops_the_private_bus(tmp_path):
    async with serving(tmp_path, WaylandPortal.PRIVATE) as pymux:
        socket = pymux.portal.private_address.removeprefix("unix:path=").split(",")[0]
        assert os.path.exists(socket)

        ALL_SERVER_OPTIONS["wayland-portal"].set_value(pymux, "off")
        await once(lambda: pymux.portal.private_address is None, 10.0, "the private bus kept running")

        assert not os.path.exists(socket)
        assert pymux.portal.pids() == []


async def test_the_host_bus_is_served_while_nobody_holds_the_name(tmp_path, monkeypatch):
    async with a_host_bus(tmp_path, monkeypatch) as address, serving(tmp_path, WaylandPortal.HOST) as pymux:
        pymux.portal.scheme_is(ColorScheme.DARK)
        async with a_client(address) as bus:
            reply = await call(bus, "ReadOne", "ss", [APPEARANCE, COLOR_SCHEME])

        # A program keeps the host's bus: nothing private started.
        assert pymux.portal.private_address is None
    assert reply.body == [Variant("u", ColorScheme.DARK)]


async def test_a_real_portal_that_can_start_is_left_alone(tmp_path, monkeypatch, caplog):
    "Holding the name first would keep the desktop's portal from ever starting."
    caplog.set_level(logging.INFO, logger="pymux.portal")
    async with a_host_bus(tmp_path, monkeypatch, real_portal=True) as address:
        pymux = Pymux()
        pymux.socket_name = str(tmp_path / "pymux.sock.test")
        async with pymux.running():
            await once(lambda: "real portal" in caplog.text, 10.0, "the portal never looked")

            async with a_client(address) as bus:
                reply = await bus.call(
                    Message(
                        destination="org.freedesktop.DBus",
                        path="/org/freedesktop/DBus",
                        interface="org.freedesktop.DBus",
                        member="NameHasOwner",
                        signature="s",
                        body=[BUS_NAME],
                    )
                )

    assert reply.body == [False]
