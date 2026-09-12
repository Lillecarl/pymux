"""
The base16 collection, as one JSON object.

Reads the YAML files of one checkout of tinted-theming/schemes -- the
`base16` directory, whose schemes are the sixteen the spec names --
and writes a JSON object: a scheme's name to its palette. The build
runs this, so a theme never needs a YAML parser to be read; see
`pymux/style_base16.py`.

A file whose front matter does not say `system: base16` is another
spec's scheme and goes by: the collection carries those beside it.
"""

import json
import sys
from pathlib import Path

import yaml

source, destination = Path(sys.argv[1]), Path(sys.argv[2])

schemes = {}
for file in sorted(source.glob("*.yaml")):
    scheme = yaml.safe_load(file.read_text())
    if scheme.get("system") != "base16":
        continue
    schemes[file.stem] = scheme["palette"]

with open(destination, "w") as out:
    json.dump(schemes, out)
