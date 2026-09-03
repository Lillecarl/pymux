{ pkgs ? import <nixpkgs> {}}:
let
  # Build against local working copies of ptterm and pyte when they sit
  # next to this repo, and fall back to the pinned fork commits
  # otherwise. (Neither upstream carries the kitty protocol support.)
  localSrc = path: if builtins.pathExists path then path else null;

  pyte = pkgs.python3Packages.callPackage ./pyte.nix {
    localSrc = localSrc ../pyte;
  };

  ptterm = pkgs.python3Packages.callPackage ./ptterm.nix {
    inherit pyte;
    localSrc = localSrc ../ptterm;
  };

  package = pkgs.python3Packages.callPackage ./package.nix { inherit ptterm; };

  # Development shell with the dependencies of `tests/drive_with_libtmux.py`
  # and the linters.
  shell = pkgs.mkShell {
    packages = [
      (pkgs.python3.withPackages (ps: [
        ps.prompt-toolkit
        pyte
        ps.wcwidth
        ps.docopt-ng
        ps.libtmux
        ps.pytest
        ptterm
      ]))
      pkgs.ruff
      pkgs.black
    ];
  };
in
{
  inherit package shell;
}
