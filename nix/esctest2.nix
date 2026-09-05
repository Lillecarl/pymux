# The conformance suite of Thomas Dickey, after George Nachman wrote it for
# iTerm2. It judges a terminal from the inside: it runs as a program in that
# terminal, writes control sequences, and reads the reports that come back.
#
# Its modules import each other by plain name, so they have to sit on the
# path together and not under a package directory of their own. `bin/esctest`
# runs the suite; `share/esctest2` is where the check imports it from,
# because the check drives it one test at a time.
#
# It is a build input of a test and reaches no closure that runs.
{
  lib,
  stdenv,
  fetchFromGitHub,
  makeWrapper,
  python,
}:
stdenv.mkDerivation {
  pname = "esctest2";
  version = "0-unstable-2025-08-24";

  src = fetchFromGitHub {
    owner = "ThomasDickey";
    repo = "esctest2";
    rev = "664be3cf2c1e3f06bc93a8bafb48a0db83c607db";
    hash = "sha256-JmUMvWmQoPyoWttW4K7Ap3/Tn0D3n8tHVPwprpeC+Is=";
  };

  nativeBuildInputs = [ makeWrapper ];
  dontConfigure = true;
  dontBuild = true;

  installPhase = ''
    runHook preInstall

    mkdir -p $out/share/esctest2
    cp -r esctest/. $out/share/esctest2/

    makeWrapper ${python.interpreter} $out/bin/esctest \
      --add-flags $out/share/esctest2/esctest.py

    runHook postInstall
  '';

  meta = {
    description = "Conformance tests for terminal emulators";
    homepage = "https://github.com/ThomasDickey/esctest2";
    license = lib.licenses.gpl2Only;
    mainProgram = "esctest";
    platforms = lib.platforms.unix;
  };
}
