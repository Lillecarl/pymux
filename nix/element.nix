# `<pymux-pane>`, compiled.
#
# The element is TypeScript, so something has to compile it. That
# something is here, and the output is two things at once:
#
# - the files `pymux/web/static/` serves, which `default.nix` installs
#   into the package and gives to the suites;
# - an npm package, so that a front end built with node links the element
#   as `node_modules/pymux-pane` and imports it by name. Reaching into
#   another package's `lib/python3.14/site-packages/` would work and would
#   break on the next interpreter. Lillecarl/pymux#461.
#
# **node is here and in nothing pymux installs.** A compiler at build
# time costs a browser nothing at run time, and it buys the two things a
# hand-written pair could not have: a `.d.ts` that cannot drift from the
# code, and a file that is known to parse before anybody serves it.
{
  lib,
  runCommand,
  writeText,
  typescript,
  # The TypeScript of the element and of the demo page.
  src,
  # pymux's own version, read from `pyproject.toml`, so the two
  # distributions of the same element cannot disagree about what they are.
  version,
}:
let
  # npm requires three parts and pymux's version has two.
  npmVersion =
    let
      parts = lib.splitString "." version;
    in
    lib.concatStringsSep "." (parts ++ lib.optional (builtins.length parts < 3) "0");

  # No `sideEffects: false`: importing the element is what defines the
  # custom element, so a bundler that dropped the import as unused would
  # leave a page with a tag nothing answers.
  manifest = writeText "package.json" (
    builtins.toJSON {
      name = "pymux-pane";
      version = npmVersion;
      description = "One pane of a pymux server, as a custom element.";
      license = "BSD-3-Clause";
      type = "module";
      main = "./pymux-pane.js";
      types = "./pymux-pane.d.ts";
      exports = {
        "." = {
          types = "./pymux-pane.d.ts";
          default = "./pymux-pane.js";
        };
      };
      files = [
        "pymux-pane.js"
        "pymux-pane.d.ts"
      ];
    }
  );
in
runCommand "pymux-element-${npmVersion}"
  {
    nativeBuildInputs = [ typescript ];
    passthru = { inherit npmVersion; };
    meta = {
      description = "The <pymux-pane> custom element, compiled";
      license = lib.licenses.bsd3;
    };
  }
  ''
    cp -r ${src}/. .
    chmod -R +w .

    # `tsconfig.json` holds every option, so that a person running `tsc`
    # in the checkout compiles it the way this does.
    tsc --project tsconfig.json

    mkdir -p "$out"
    # `page.js` rides along and the manifest does not export it: the
    # demo page is served beside the element, not imported through it.
    install -m644 dist/pymux-pane.js dist/pymux-pane.d.ts dist/page.js "$out"/
    install -m644 ${manifest} "$out"/package.json
  ''
