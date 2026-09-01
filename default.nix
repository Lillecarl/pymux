{ pkgs ? import <nixpkgs> {}}:
{
  package = pkgs.python3Packages.callPackage ./package.nix {
    ptterm = pkgs.python3Packages.callPackage ./ptterm.nix {};
  };
}
