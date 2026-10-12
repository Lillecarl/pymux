"""
A Wayland window for the window pane suite: it draws one colour, then a
red square once its first frame was shown, and writes every key and
button it receives to the file named on its command line. With
`--animate` it then draws a frame each time the last one was shown, and
writes "frame" for each. With `--idle` it commits nothing but a frame
callback instead, as Firefox does to wait for the next frame, and
writes "idle" each time one is answered. Each time the pointer enters,
it asks for a text pointer.

It stays up until it is asked to close, which is what a window pane
does to a window when the pane is killed.
"""

from __future__ import annotations

import mmap
import sys

from pywayland.client import Display
from pywayland.protocol.cursor_shape_v1 import WpCursorShapeDeviceV1, WpCursorShapeManagerV1
from pywayland.protocol.wayland import WlCompositor, WlSeat, WlShm
from pywayland.protocol.xdg_shell import XdgWmBase
from pywayland.utils import AnonymousFile

#: B, G, R, A, as an ARGB8888 word is in memory.
FILL = bytes([0x99, 0x66, 0x33, 0xFF])
RED = bytes([0x00, 0x00, 0xFF, 0xFF])


def main(log_path: str, animate: bool, idle: bool) -> None:
    log = open(log_path, "a", buffering=1)
    display = Display()
    display.connect()
    registry = display.get_registry()
    offered = {}
    registry.dispatcher["global"] = lambda _r, name, interface, version: offered.update({interface: (name, version)})
    display.roundtrip()

    def bind(interface):
        name, version = offered[interface.name]
        return registry.bind(name, interface, min(version, interface.version))

    compositor = bind(WlCompositor)
    shm = bind(WlShm)
    wm_base = bind(XdgWmBase)
    wm_base.dispatcher["ping"] = lambda wm, serial: wm.pong(serial)
    seat = bind(WlSeat)
    keyboard = seat.get_keyboard()
    keyboard.dispatcher["key"] = lambda _k, _serial, _time, key, state: log.write("key %d %d\n" % (key, state))
    pointer = seat.get_pointer()
    pointer.dispatcher["motion"] = lambda _p, _time, x, y: log.write("motion %d %d\n" % (x, y))
    pointer.dispatcher["button"] = lambda _p, _serial, _time, button, state: log.write(
        "button %d %d\n" % (button, state)
    )
    pointer.dispatcher["axis"] = lambda _p, _time, axis, value: log.write("axis %d %d\n" % (axis, int(value)))
    shapes = bind(WpCursorShapeManagerV1).get_pointer(pointer)
    pointer.dispatcher["enter"] = lambda _p, serial, *_rest: shapes.set_shape(
        serial, WpCursorShapeDeviceV1.shape.text.value
    )

    surface = compositor.create_surface()
    xdg_surface = wm_base.get_xdg_surface(surface)
    toplevel = xdg_surface.get_toplevel()
    toplevel.set_title("probe")
    toplevel.set_app_id("pymux.probe")

    size = [64, 32]
    configured = []
    closing = []
    #: The pane's size arrived, which resizes the canvas pymux draws on.
    sized = []

    def on_toplevel_configure(_toplevel, width, height, _states):
        if width and height:
            sized.append((width, height))
            log.write("size %d %d\n" % (width, height))

    def on_configure(xdg, serial):
        xdg.ack_configure(serial)
        configured.append(serial)

    toplevel.dispatcher["configure"] = on_toplevel_configure
    toplevel.dispatcher["close"] = lambda _toplevel: closing.append(True)
    xdg_surface.dispatcher["configure"] = on_configure
    surface.commit()
    while not configured:
        display.dispatch(block=True)

    width, height = size
    stride = width * 4
    length = stride * height
    with AnonymousFile(length) as fd:
        pool = shm.create_pool(fd, length)
        pixels = mmap.mmap(fd, length)
    buffer = pool.create_buffer(0, width, height, stride, WlShm.format.argb8888.value)

    pixels[:] = FILL * (width * height)
    surface.attach(buffer, 0, 0)
    surface.damage_buffer(0, 0, width, height)
    shown = []
    surface.frame().dispatcher["done"] = lambda _callback, _time: shown.append(True)
    surface.commit()
    # The buffer keeps its first size, and the square comes after the
    # pane's size did: a canvas that changes size is sent whole, so an
    # edit before it would never be one.
    while not (shown and sized):
        display.dispatch(block=True)
    # One frame at the new size, taken by the client, before the square:
    # the resize and the square in one frame would go whole.
    shown.clear()
    surface.attach(buffer, 0, 0)
    surface.damage_buffer(0, 0, 1, 1)
    surface.frame().dispatcher["done"] = lambda _callback, _time: shown.append(True)
    surface.commit()
    while not shown:
        display.dispatch(block=True)

    for row in range(5, 9):
        start = row * stride + 10 * 4
        pixels[start : start + 16] = RED * 4
    surface.attach(buffer, 0, 0)
    surface.damage_buffer(10, 5, 4, 4)
    shown.clear()
    surface.frame().dispatcher["done"] = lambda _callback, _time: shown.append(True)
    surface.commit()
    log.write("drew\n")

    # With --animate, a new frame each time the last was shown.
    flip = 0
    while not closing:
        display.dispatch(block=True)
        if animate and shown:
            shown.clear()
            log.write("frame\n")
            flip ^= 1
            pixels[0:4] = RED if flip else FILL
            surface.attach(buffer, 0, 0)
            surface.damage_buffer(0, 0, 1, 1)
            surface.frame().dispatcher["done"] = lambda _callback, _time: shown.append(True)
            surface.commit()
        elif idle and shown:
            shown.clear()
            log.write("idle\n")
            surface.frame().dispatcher["done"] = lambda _callback, _time: shown.append(True)
            surface.commit()
    log.write("closed\n")
    toplevel.destroy()
    xdg_surface.destroy()
    surface.destroy()
    display.roundtrip()


if __name__ == "__main__":
    main(sys.argv[1], "--animate" in sys.argv[2:], "--idle" in sys.argv[2:])
