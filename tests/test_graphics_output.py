"""
Tests for the kitty graphics output of a client (`pymux.graphics`).

Each test builds the pane state with the real `ptterm.graphics`
classes, renders one or more frames, and checks the escape sequences
that reach the outer terminal.
"""
import base64
import re
import zlib

from ptterm.graphics import GraphicsImage, GraphicsPlacement, GraphicsState

from pymux.graphics import ClientGraphics, PaneView

# 2x2 pixels, RGB.
IMAGE_DATA = bytes(range(12))


def make_client():
    "Return (client graphics, list that collects what it writes)."
    written = []
    client = ClientGraphics(written.append, lambda: None)
    client.supported = True
    return client, written


def make_state(*placements, data=IMAGE_DATA, format=24, width=2, height=2):
    """
    A pane graphics state holding one image (id 1) and the given
    placements of it.
    """
    state = GraphicsState()
    state.images_by_id[1] = GraphicsImage(format, width, height, data)
    state.placements = list(placements)
    return state


def placement(x=0, y=0, columns=3, rows=2, z=0, virtual=False):
    return GraphicsPlacement(1, 0, x, y, columns, rows, z, virtual)


def view(state, x=0, y=0, width=80, height=24, vscroll=0, hscroll=0, pane_id=1):
    return PaneView(
        pane_id=pane_id,
        x=x,
        y=y,
        width=width,
        height=height,
        vertical_scroll=vscroll,
        horizontal_scroll=hscroll,
        graphics=state,
    )


def puts(written):
    "The put commands of everything that was written."
    return re.findall(r"\x1b\[\d+;\d+H\x1b_Ga=p,[^\x1b]*\x1b\\", "".join(written))


def transmissions(written):
    return re.findall(r"\x1b_Ga=t,[^;]*;", "".join(written))


def deletes(written):
    return re.findall(r"\x1b_Ga=d,[^\x1b]*\x1b\\", "".join(written))


def outer_id(written):
    match = re.search(r"\x1b_Ga=t,i=(\d+),", "".join(written))
    assert match
    return int(match.group(1))


def test_a_placement_is_transmitted_and_put():
    client, written = make_client()
    client.render([view(make_state(placement()), x=10, y=5)])

    image_id = outer_id(written)
    assert transmissions(written) == [
        "\x1b_Ga=t,i=%i,t=d,q=2,f=24,s=2,v=2,o=z,m=0;" % image_id
    ]
    # The cell is one based in the cursor position sequence.
    assert puts(written) == [
        "\x1b[6;11H\x1b_Ga=p,i=%i,p=1,c=3,r=2,C=1,q=2\x1b\\" % image_id
    ]


def test_the_transmitted_data_is_the_image():
    client, written = make_client()
    client.render([view(make_state(placement()))])

    payload = re.search(r"o=z,m=0;([^\x1b]*)\x1b", "".join(written)).group(1)
    assert zlib.decompress(base64.b64decode(payload)) == IMAGE_DATA


def test_png_is_sent_as_is():
    client, written = make_client()
    data = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    client.render([view(make_state(placement(), data=data, format=100))])

    assert transmissions(written) == [
        "\x1b_Ga=t,i=%i,t=d,q=2,f=100,m=0;" % outer_id(written)
    ]


def test_an_unchanged_frame_sends_nothing():
    client, written = make_client()
    state = make_state(placement())
    client.render([view(state)])
    written.clear()

    client.render([view(state)])
    assert written == []


def test_a_moved_placement_is_replaced_without_a_delete():
    client, written = make_client()
    state = make_state(placement())
    client.render([view(state)])
    image_id = outer_id(written)
    written.clear()

    state.placements[0].y = 4
    client.render([view(state)])
    assert deletes(written) == []
    assert transmissions(written) == []  # The pixels are already there.
    assert puts(written) == [
        "\x1b[5;1H\x1b_Ga=p,i=%i,p=1,c=3,r=2,C=1,q=2\x1b\\" % image_id
    ]


def test_a_removed_placement_is_deleted():
    client, written = make_client()
    state = make_state(placement())
    client.render([view(state)])
    image_id = outer_id(written)
    written.clear()

    state.placements = []
    client.render([view(state)])
    assert deletes(written) == ["\x1b_Ga=d,d=i,i=%i,p=1,q=2\x1b\\" % image_id]


def test_a_forgotten_image_is_freed():
    client, written = make_client()
    state = make_state(placement())
    client.render([view(state)])
    image_id = outer_id(written)
    written.clear()

    state.placements = []
    state.images_by_id = {}
    client.render([view(state)])
    assert "\x1b_Ga=d,d=I,i=%i,q=2\x1b\\" % image_id in "".join(written)


def test_a_scrolled_out_image_is_kept():
    "Scrolling an image out of view must not re-transmit its pixels."
    client, written = make_client()
    state = make_state(placement())
    client.render([view(state)])
    written.clear()

    client.render([view(state, vscroll=100)])
    assert puts(written) == []
    written.clear()

    client.render([view(state)])
    assert transmissions(written) == []
    assert len(puts(written)) == 1


def test_a_placement_above_the_pane_is_cropped():
    client, written = make_client()
    # Rows 0 and 1 of the buffer, but the pane shows from row 1.
    client.render([view(make_state(placement(rows=2, columns=2)), vscroll=1)])

    image_id = outer_id(written)
    # One of the two rows is left: the top half of the image is cut.
    assert puts(written) == [
        "\x1b[1;1H\x1b_Ga=p,i=%i,p=1,c=2,r=1,C=1,q=2,x=0,y=1,w=2,h=1\x1b\\"
        % image_id
    ]


def test_a_placement_over_the_right_edge_is_cropped():
    client, written = make_client()
    client.render(
        [view(make_state(placement(x=2, columns=4, rows=2)), width=4)]
    )

    image_id = outer_id(written)
    # Two of the four columns fit. The image is 2 pixels wide, so half
    # a pixel per column: the width rounds up to one pixel.
    assert puts(written) == [
        "\x1b[1;3H\x1b_Ga=p,i=%i,p=1,c=2,r=2,C=1,q=2,x=0,y=0,w=1,h=2\x1b\\"
        % image_id
    ]


def test_a_placement_outside_the_pane_is_skipped():
    client, written = make_client()
    client.render([view(make_state(placement(y=50)), height=24)])
    assert puts(written) == []


def test_a_virtual_placement_is_skipped():
    client, written = make_client()
    client.render([view(make_state(placement(virtual=True)))])
    assert puts(written) == []


def test_the_z_index_is_passed_on():
    client, written = make_client()
    client.render([view(make_state(placement(z=-1)))])
    assert "z=-1" in "".join(written)


def test_two_placements_of_one_image_get_their_own_slot():
    client, written = make_client()
    state = make_state(placement(y=0), placement(y=4))
    client.render([view(state)])

    image_id = outer_id(written)
    assert puts(written) == [
        "\x1b[1;1H\x1b_Ga=p,i=%i,p=1,c=3,r=2,C=1,q=2\x1b\\" % image_id,
        "\x1b[5;1H\x1b_Ga=p,i=%i,p=2,c=3,r=2,C=1,q=2\x1b\\" % image_id,
    ]


def test_two_panes_with_the_same_image_id_do_not_collide():
    client, written = make_client()
    first = make_state(placement())
    second = make_state(placement())
    client.render(
        [
            view(first, pane_id=1),
            view(second, pane_id=2, y=12),
        ]
    )

    ids = re.findall(r"\x1b_Ga=t,i=(\d+),", "".join(written))
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_replacing_the_image_of_a_pane_id_re_transmits():
    client, written = make_client()
    state = make_state(placement())
    client.render([view(state)])
    first_id = outer_id(written)
    written.clear()

    # The pane transmitted new data under the same image id.
    state.images_by_id[1] = GraphicsImage(24, 2, 2, IMAGE_DATA)
    client.render([view(state)])

    assert "\x1b_Ga=d,d=I,i=%i,q=2\x1b\\" % first_id in "".join(written)
    assert len(transmissions(written)) == 1
    assert outer_id(written) != first_id


def test_no_views_removes_everything():
    "A popup hides the panes: the images must go."
    client, written = make_client()
    state = make_state(placement())
    client.render([view(state)])
    image_id = outer_id(written)
    written.clear()

    client.render([])
    joined = "".join(written)
    assert "\x1b_Ga=d,d=i,i=%i,p=1,q=2\x1b\\" % image_id in joined
    assert "\x1b_Ga=d,d=I,i=%i,q=2\x1b\\" % image_id in joined


def test_reset_removes_the_images():
    client, written = make_client()
    client.render([view(make_state(placement()))])
    image_id = outer_id(written)
    written.clear()

    client.reset()
    assert deletes(written) == ["\x1b_Ga=d,d=I,i=%i,q=2\x1b\\" % image_id]

    # And it forgot them: a new frame transmits again.
    written.clear()
    client.render([view(make_state(placement()))])
    assert len(transmissions(written)) == 1


def test_an_unsupported_terminal_gets_nothing():
    written = []
    client = ClientGraphics(written.append, lambda: None)
    client.render([view(make_state(placement()))])
    assert written == []


def test_the_batch_saves_and_restores_the_cursor():
    client, written = make_client()
    client.render([view(make_state(placement()))])

    # The transmission goes out on its own; the batch that moves the
    # cursor is wrapped.
    batch = written[-1]
    assert batch.startswith("\x1b7")
    assert batch.endswith("\x1b8")


def test_the_query_reply_enables_the_output():
    written = []
    client = ClientGraphics(written.append, lambda: None)
    assert not client.supported

    client.handle_reply("\x1b_Gi=1;OK\x1b\\")  # Another image: not ours.
    client.handle_reply("\x1b_Gi=311;OK\x1b\\")  # An id that only looks like it.
    client.handle_reply("\x1b_Gi=31;ENOENT:no such image\x1b\\")
    assert not client.supported

    client.handle_reply("\x1b_Gi=31;OK\x1b\\")
    assert client.supported


def test_the_query_reply_is_recognised_with_extra_keys():
    written = []
    client = ClientGraphics(written.append, lambda: None)
    client.handle_reply("\x1b_GI=2,i=31,p=1;OK\x1b\\")
    assert client.supported
