"""Write the shell completion scripts of an argcomplete program.

`default.nix` runs this against a python that has argcomplete, and
`installShellCompletion` puts what it writes under `share/`, so that a
shell loads it when the package is in `environment.systemPackages` or
`home.packages`.

**The whole script comes from argcomplete, through public API.**
`argcomplete.shell_integration.shellcode` is what
`register-python-argcomplete` calls, and it takes the name of the
program and the name of the shell. Nothing here knows what the
completion protocol is: the script it writes makes the shell ask
`pymux` with the line and the cursor position, and pymux answers
through its own parser tree.
"""

from __future__ import annotations

import pathlib
import sys

#: The shells this writes a script for. One file each, into `share/`.
SHELLS = ("bash", "zsh", "fish")


def main() -> None:
    program, out_dir = sys.argv[1], pathlib.Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)
    for shell in SHELLS:
        text = shellcode([program], shell=shell)
        (out_dir / shell).write_text(text, encoding="utf-8")
        print(f"{shell}: {len(text)} bytes")  # noqa: T201 -- the build log is the report


if __name__ == "__main__":
    from argcomplete.shell_integration import shellcode

    main()
