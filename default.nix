{ pkgs ? import <nixpkgs> {}}:
{
  package = pkgs.python3Packages.callPackage ./package.nix {};
}
