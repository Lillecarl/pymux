"""
A terminal's cell can change while it stays attached: kitty moved to a
display of another density keeps its columns and changes its cell. The
server asks again at each resize, and reads the answer whenever it
comes. With the first cell kept, every pixel mouse report landed at
half its column. Lillecarl/pymux#774.
"""

from __future__ import annotations

from libpymux.protocol import Field, Packet
from prompt_toolkit.data_structures import Size
from session import once, over_connection
from test_show_terminal import frames_of

from pymux.graphics.graphics import CELL_SIZE_QUERY

SIZE = Size(rows=24, columns=80)


async def test_a_resize_asks_the_cell_again_and_the_answer_is_taken():
    packets: list = []
    async with over_connection(read_packet=packets.append) as session:
        state, _ = await session.attach("kitty", SIZE)
        connection = state.connection
        session.typed(state, "\x1b[6;35;19t")
        await once(lambda: connection.graphics.cell_width == 19, 5.0, "the first cell was not read")
        connection.replies.pixel_mouse = True

        packets.clear()
        connection._process({Field.CMD: Packet.SIZE, Field.DATA: [SIZE.rows, SIZE.columns]})
        await once(lambda: CELL_SIZE_QUERY in frames_of(packets), 5.0, "the cell was not asked again")

        session.typed(state, "\x1b[6;18;10t")
        await once(lambda: connection.graphics.cell_width == 10, 5.0, "the new cell was not read")
        assert connection.replies.pixel_cell()[:2] == (10, 18)
