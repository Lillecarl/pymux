# The Rust kernels of pymux's image loops, as a package of their own.
#
# pymux depends on this, so this depends on nothing of pymux: the
# package holds the kernels, and `pymux/kernels.py` puts them in place.
# Its parity check still takes pymux, for the Python each kernel is held
# to. `pyte/pyte_rs/default.nix` says why this is not `mkProject`.
# Lillecarl/pymux#574.
{
  lib,
  stdenv,
  python,
  pyprojectHook,
  mkVirtualEnv,
  callPackage,
  rustPlatform,
  cargo,
  rustc,
  maturin,
  clippy,
  rustfmt,
}:
let
  fs = lib.fileset;

  inherit (callPackage ../nix/suite.nix { }) suite;

  src = fs.toSource {
    root = ./.;
    fileset = fs.unions [
      ./Cargo.toml
      ./Cargo.lock
      ./pyproject.toml
      ./src
      (fs.fileFilter (file: file.hasExt "py") ./python)
    ];
  };

  cargoDeps = rustPlatform.importCargoLock { lockFile = ./Cargo.lock; };

  # The Rust's style, held by the formatter and by clippy with every
  # warning an error, the way `ruff` holds the Python's.
  #
  #     nix build --file . checks.pymux-rs-lint
  checks.lint = suite {
    name = "pymux-rs-lint";
    # A compiler, for the build scripts of the crates: a `runCommand`
    # has none of its own.
    inputs = [
      cargo
      rustc
      clippy
      rustfmt
      stdenv.cc
    ];
    setup = ''
      cp -r ${src}/. crate
      chmod -R +w crate
      cd crate
      export HOME="$TMPDIR"
      export PYO3_PYTHON=${python.interpreter}
      export CARGO_TARGET_DIR="$TMPDIR/target"
      # What `cargoSetupHook` writes, which a `runCommand` never runs.
      mkdir -p .cargo
      printf '[source.crates-io]\nreplace-with = "vendored"\n[source.vendored]\ndirectory = "%s"\n' ${cargoDeps} > .cargo/config.toml
    '';
  } ''
    cargo fmt --check
    cargo clippy --offline --frozen -- -D warnings
  '';

  testEnv = mkVirtualEnv "pymux-rs-test-env" {
    pymux = [ ];
    pymux-rs = [ ];
    hypothesis = [ ];
    pytest = [ ];
  };

  # The kernels against the Python they replace.
  #
  #     nix build --file . checks.pymux-rs-parity
  checks.parity = suite {
    name = "pymux-rs-parity";
    inputs = [ testEnv ];
    setup = ''
      cp -r ${./tests} tests
      chmod -R +w tests
      export HOME="$TMPDIR"
      export LANG=C.UTF-8
      export PYTHONDONTWRITEBYTECODE=1
    '';
  } "python -m pytest tests -q -p no:cacheprovider";
in
stdenv.mkDerivation {
  pname = "pymux_rs";
  version = "0.1.0";

  inherit src cargoDeps;

  nativeBuildInputs = [
    pyprojectHook
    rustPlatform.cargoSetupHook
    cargo
    rustc
    maturin
  ];

  buildPhase = ''
    runHook preBuild
    maturin build --release --offline --frozen --interpreter ${python.interpreter} --out dist
    runHook postBuild
  '';

  passthru = {
    dependencies = { };
    inherit checks;
  };

  meta = {
    description = "Rust kernels for pymux's image loops";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.unix;
  };
}
