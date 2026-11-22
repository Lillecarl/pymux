"""
The fonts `pymux web` serves, from the files `capture-pane --png` draws
with: each as woff2 under `fonts/`, and `fonts.css` with an `@font-face`
for each. Lillecarl/pyterm#1070.

    python web-fonts.py OUT FACES.json

FACES.json is a list of families in fallback order, each a list of faces
`{"path", "weight", "style"}`. Each family is `pymux-<n>`, and
`--pyte-font` on `pymux-pane` names them in that order.

**A face carries the `unicode-range` of its own character map**, so a
browser fetches a fallback only for a glyph on screen that the faces
before it lack: Unifont, which covers the whole of Unicode, comes last and
is rarely fetched at all.

**A file is named by its content**, so `pymux web` can let a browser keep
it: a phone fetches a megabyte of font once and not on every load.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from pathlib import Path

from fontTools.ttLib import TTFont

#: Families of the device, asked for between the served ones: a colour
#: emoji draws in a browser, and the served Noto Emoji is one colour,
#: because Pillow sizes no colour font to a cell.
EMOJI_AFTER = 1
DEVICE_EMOJI = ['"Noto Color Emoji"', '"Apple Color Emoji"', '"Segoe UI Emoji"']


def ranges_of(points: set[int]) -> str:
    "`points` as a CSS `unicode-range`, runs joined."
    ordered = sorted(points)
    runs: list[str] = []
    start = previous = ordered[0]
    # -1 ends the last run.
    for point in [*ordered[1:], -1]:
        if point == previous + 1:
            previous = point
            continue
        runs.append("U+%X" % start if start == previous else "U+%X-%X" % (start, previous))
        start = previous = point
    return ", ".join(runs)


def woff2_of(path: str) -> tuple[bytes, set[int]]:
    "The face at `path` as woff2, and the code points it maps."
    font = TTFont(path, fontNumber=0)
    points = {point for point in font.getBestCmap() if point >= 0x20}
    font.flavor = "woff2"
    out = io.BytesIO()
    font.save(out)
    return out.getvalue(), points


def main() -> None:
    out, faces_json = Path(sys.argv[1]), Path(sys.argv[2])
    families = json.loads(faces_json.read_text())
    (out / "fonts").mkdir(parents=True)
    rules: list[str] = []
    names: list[str] = []
    for number, faces in enumerate(families):
        family = "pymux-%d" % number
        names.append('"%s"' % family)
        if number == EMOJI_AFTER:
            names.extend(DEVICE_EMOJI)
        for face in faces:
            data, points = woff2_of(face["path"])
            name = "%s-%s-%s.%s.woff2" % (family, face["weight"], face["style"], hashlib.sha256(data).hexdigest()[:12])
            (out / "fonts" / name).write_bytes(data)
            rules.append(
                "@font-face {\n"
                '  font-family: "%s";\n'
                '  src: url("fonts/%s") format("woff2");\n'
                "  font-weight: %s;\n"
                "  font-style: %s;\n"
                "  font-display: swap;\n"
                "  unicode-range: %s;\n"
                "}\n" % (family, name, face["weight"], face["style"], ranges_of(points))
            )
    names.append("monospace")
    # `pyte.html.CSS` reads it on the screen, which inherits it.
    rules.append("pymux-pane {\n  --pyte-font: %s;\n}\n" % (", ".join(names),))
    (out / "fonts.css").write_text("".join(rules))


if __name__ == "__main__":
    main()
