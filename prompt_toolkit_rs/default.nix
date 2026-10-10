# The Rust kernels of prompt_toolkit, as a package of its own.
#
# It lives in pymux and not in prompt-toolkit, because every patch to
# prompt-toolkit has to be one upstream could take: that source only names
# the functions a kernel replaces, and the Rust is here, beside the program
# that installs it. `pyte/pyte_rs/default.nix` says why this is not
# `mkProject`. Lillecarl/pymux#566.
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
  #     nix build --file . checks.prompt-toolkit-rs-lint
  checks.lint = suite {
    name = "prompt-toolkit-rs-lint";
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

  testEnv = mkVirtualEnv "prompt-toolkit-rs-test-env" {
    prompt-toolkit = [ ];
    prompt-toolkit-rs = [ ];
    hypothesis = [ ];
    pytest = [ ];
  };

  # The kernels against the Python they replace.
  #
  #     nix build --file . checks.prompt-toolkit-rs-parity
  checks.parity = suite {
    name = "prompt-toolkit-rs-parity";
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
  pname = "prompt_toolkit_rs";
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
    dependencies = {
      prompt-toolkit = [ ];
    };
    inherit checks;
  };

  meta = {
    description = "Rust kernels for prompt_toolkit's per-cell loops";
    license = lib.licenses.bsd3;
    platforms = lib.platforms.unix;
  };
}
