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
}:
let
  fs = lib.fileset;

  inherit (callPackage ../nix/suite.nix { }) suite;

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
