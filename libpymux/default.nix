# The client library of pymux, as a package of its own.
#
# It is a project inside this repository and not a source of the
# umbrella, and `pyproject.toml` beside this says why.
#
# There are no checks here. The suites that judge this library are
# pymux's -- `tests/test_libpymux.py` against a socket that answers
# what a test tells it to, and `tests/drive_with_pty.py` against a
# server that is really there -- and a check here could not run the
# second one without depending on the layer above.
{
  lib,
  python,
  stdenv,
  pyprojectHook,
  resolveBuildSystem,
  mkProject,
}:
(mkProject {
  root = ./.;
  inherit python;
  extra = rendered: {
    meta = rendered.meta // {
      description = "Drive a pymux server from python";
      homepage = "https://github.com/prompt-toolkit/pymux";
      license = lib.licenses.bsd3;
      platforms = lib.platforms.unix;
    };
  };
})
  {
    inherit stdenv pyprojectHook resolveBuildSystem;
  }
