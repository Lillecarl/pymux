{ pkgs ? import <nixpkgs> {}}:
let
  ptterm = pkgs.python3Packages.callPackage ./ptterm.nix {};
  package = pkgs.python3Packages.callPackage ./package.nix { inherit ptterm; };

  # Development shell with the dependencies of `tests/drive_with_libtmux.py`
  # and the linters.
  shell = pkgs.mkShell {
    packages = [
      (pkgs.python3.withPackages (ps: [
        ps.prompt-toolkit
        ps.pyte
        ps.wcwidth
        ps.docopt-ng
        ps.libtmux
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
