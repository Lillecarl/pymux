{ pkgs ? import <nixpkgs> {}}:
let
  # Build against local working copies of ptterm and pyte when they sit
  # next to this repo, and fall back to the pinned upstream sources
  # otherwise.
  pttermSrc =
    if builtins.pathExists ../ptterm
    then ../ptterm
    else null;

  pyte =
    if builtins.pathExists ../pyte
    then pkgs.python3Packages.callPackage ./pyte.nix { src = ../pyte; }
    else pkgs.python3Packages.pyte;

  ptterm = pkgs.python3Packages.callPackage ./ptterm.nix ({
    inherit pyte;
  } // pkgs.lib.optionalAttrs (pttermSrc != null) { src = pttermSrc; });

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
