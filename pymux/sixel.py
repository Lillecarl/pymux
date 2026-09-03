"""
Sixel image encoding.

A terminal that does not speak the kitty graphics protocol may still
draw sixel images. This module turns the RGBA pixels of a pane image
into the DCS sequence that such a terminal understands, so that the
same image reaches both kinds of client.

Sixel holds at most 256 colours and no alpha channel. The encoder
therefore reduces the colours with a median cut, and it leaves the
pixels that are more transparent than half undrawn, which the sixel
"P2 = 1" mode keeps clear.
"""
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "encode_sixel",
    "scale_rgba",
    "to_rgba",
]

# Sixel colour registers.
MAX_COLORS = 255

# Bound the work of one image. A terminal image is far below this.
MAX_PIXELS = 4 * 1024 * 1024

# A pixel that is more transparent than this is not drawn.
ALPHA_THRESHOLD = 128

_DATA_LOW = 0x3F  # "?"

# A run shorter than this costs more as "!n" than as repeated
# characters.
_MIN_RUN = 4


def to_rgba(
    image_format: int, width: int, height: int, data: bytes
) -> Optional[bytes]:
    """
    The RGBA bytes of a pane image, or None when the format is one that
    this module cannot read.

    PNG needs a decoder; it lives in ptterm, next to the graphics
    state that holds the image.
    """
    expected = width * height
    if expected <= 0 or expected > MAX_PIXELS:
        return None

    if image_format == 32:
        return data if len(data) >= expected * 4 else None

    if image_format == 24:
        if len(data) < expected * 3:
            return None
        out = bytearray(expected * 4)
        for index in range(expected):
            source = index * 3
            target = index * 4
            out[target : target + 3] = data[source : source + 3]
            out[target + 3] = 255
        return bytes(out)

    if image_format == 100:
        try:
            from ptterm.png import decode_png
        except ImportError:
            return None
        decoded = decode_png(data)
        if decoded is None:
            return None
        return decoded[2]

    return None


def scale_rgba(
    rgba: bytes, width: int, height: int, new_width: int, new_height: int
) -> bytes:
    """
    Resize RGBA pixels with the nearest neighbour. (The outer terminal
    decides how many pixels a cell holds; the image has to match.)
    """
    if (new_width, new_height) == (width, height):
        return rgba

    columns = [x * width // new_width for x in range(new_width)]
    out = bytearray(new_width * new_height * 4)
    for y in range(new_height):
        source_row = (y * height // new_height) * width
        target = y * new_width * 4
        for x, source_column in enumerate(columns):
            source = (source_row + source_column) * 4
            out[target : target + 4] = rgba[source : source + 4]
            target += 4
    return bytes(out)


# ----------------------------------------------------------------------
# Colour reduction.


def _median_cut(
    counts: Dict[Tuple[int, int, int], int], max_colors: int
) -> List[List[Tuple[int, int, int]]]:
    """
    Split the colours into at most `max_colors` buckets.

    Every step cuts the bucket with the widest channel in half at the
    median of that channel, which is the median cut of Heckbert.
    """
    buckets: List[List[Tuple[int, int, int]]] = [list(counts)]

    while len(buckets) < max_colors:
        widest = -1
        chosen = -1
        chosen_channel = 0
        for index, bucket in enumerate(buckets):
            if len(bucket) < 2:
                continue
            for channel in range(3):
                values = [color[channel] for color in bucket]
                spread = max(values) - min(values)
                if spread > widest:
                    widest = spread
                    chosen = index
                    chosen_channel = channel
        if chosen < 0 or widest <= 0:
            break

        bucket = sorted(buckets[chosen], key=lambda color: color[chosen_channel])
        middle = len(bucket) // 2
        buckets[chosen : chosen + 1] = [bucket[:middle], bucket[middle:]]

    return buckets


def _palette(
    counts: Dict[Tuple[int, int, int], int], max_colors: int
) -> Tuple[List[Tuple[int, int, int]], Dict[Tuple[int, int, int], int]]:
    """
    A palette of at most `max_colors` entries, and the map from every
    colour of the image to its entry.
    """
    if len(counts) <= max_colors:
        palette = sorted(counts)
        return palette, {color: index for index, color in enumerate(palette)}

    palette = []
    mapping: Dict[Tuple[int, int, int], int] = {}
    for index, bucket in enumerate(_median_cut(counts, max_colors)):
        if not bucket:
            continue
        weight = sum(counts[color] for color in bucket)
        entry = tuple(
            sum(color[channel] * counts[color] for color in bucket) // weight
            for channel in range(3)
        )
        palette.append(entry)
        for color in bucket:
            mapping[color] = len(palette) - 1

    return palette, mapping


# ----------------------------------------------------------------------
# Encoding.


def _runs(row: Sequence[int], width: int) -> str:
    """
    One sixel row as data characters, with the repeat introducer for
    the long runs. Trailing empty columns are left out.
    """
    end = width
    while end > 0 and row[end - 1] == 0:
        end -= 1
    if end == 0:
        return ""

    out: List[str] = []
    index = 0
    while index < end:
        value = row[index]
        run = 1
        while index + run < end and row[index + run] == value:
            run += 1
        char = chr(_DATA_LOW + value)
        if run >= _MIN_RUN:
            out.append("!%i%s" % (run, char))
        else:
            out.append(char * run)
        index += run
    return "".join(out)


def encode_sixel(
    width: int, height: int, rgba: bytes, max_colors: int = MAX_COLORS
) -> Optional[str]:
    """
    The full DCS sequence that draws `rgba` as a sixel image, or None
    when there is nothing to draw.

    The pixels that are more transparent than half stay clear: the
    header asks for the transparent background mode.
    """
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        return None
    if len(rgba) < width * height * 4:
        return None

    # Index every pixel, and count the colours on the way.
    counts: Dict[Tuple[int, int, int], int] = {}
    indexed: List[Optional[Tuple[int, int, int]]] = [None] * (width * height)
    for index in range(width * height):
        offset = index * 4
        if rgba[offset + 3] < ALPHA_THRESHOLD:
            continue
        color = (rgba[offset], rgba[offset + 1], rgba[offset + 2])
        indexed[index] = color
        counts[color] = counts.get(color, 0) + 1

    if not counts:
        return None

    palette, mapping = _palette(counts, max(1, min(max_colors, MAX_COLORS)))

    out = ['\x1bP0;1;0q"1;1;%i;%i' % (width, height)]
    for index, (red, green, blue) in enumerate(palette):
        out.append(
            "#%i;2;%i;%i;%i"
            % (
                index,
                round(red * 100 / 255),
                round(green * 100 / 255),
                round(blue * 100 / 255),
            )
        )

    for band in range(0, height, 6):
        rows: Dict[int, List[int]] = {}
        for offset in range(6):
            y = band + offset
            if y >= height:
                break
            base = y * width
            bit = 1 << offset
            for x in range(width):
                color = indexed[base + x]
                if color is None:
                    continue
                entry = mapping[color]
                row = rows.get(entry)
                if row is None:
                    row = rows[entry] = [0] * width
                row[x] |= bit

        parts = []
        for entry in sorted(rows):
            data = _runs(rows[entry], width)
            if data:
                parts.append("#%i%s" % (entry, data))
        out.append("$".join(parts))
        if band + 6 < height:
            out.append("-")

    out.append("\x1b\\")
    return "".join(out)
