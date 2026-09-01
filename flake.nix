{
  description = "pymux - Pure Python terminal multiplexer";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs { inherit system; };
        pythonPackages = pkgs.python3Packages;

        ptterm = pythonPackages.callPackage ./ptterm.nix {};
        pymux = pythonPackages.callPackage ./package.nix { inherit ptterm; };

        pythonEnv = pythonPackages.python.withPackages (ps: [
          ps.prompt-toolkit
          ps.pyte
          ps.wcwidth
          ps.docopt-ng
          ptterm
          pymux
        ]);
      in
      {
        packages = {
          inherit pymux ptterm;
          default = pymux;
        };

        apps.default = {
          type = "app";
          program = "${pymux}/bin/pymux";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.ruff
            pkgs.black
          ];
        };

        checks.pymux = pymux;
      });
}
