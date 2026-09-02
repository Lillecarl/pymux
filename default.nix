{ pkgs ? import <nixpkgs> {}}:
let
  # Build against a local ptterm working copy when one sits next to this
  # repo, and fall back to the pinned upstream commit otherwise.
  pttermSrc =
    if builtins.pathExists ../ptterm
    then ../ptterm
    else null;

  ptterm = pkgs.python3Packages.callPackage ./ptterm.nix
    (pkgs.lib.optionalAttrs (pttermSrc != null) { src = pttermSrc; });

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
