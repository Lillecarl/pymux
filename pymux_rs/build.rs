//! Bindings to the libvpx decoder, generated from the headers of the
//! libvpx this links: the struct layouts and the ABI version have to be
//! that build's own. Lillecarl/pymux#639.

use std::env;
use std::path::PathBuf;

fn main() {
    let vpx = pkg_config::probe_library("vpx").expect("libvpx, found through pkg-config");
    println!("cargo:rerun-if-changed=src/vpx.h");
    let bindings = bindgen::Builder::default()
        .header("src/vpx.h")
        .clang_args(
            vpx.include_paths
                .iter()
                .map(|path| format!("-I{}", path.display())),
        )
        .allowlist_function("vpx_.*")
        .allowlist_type("vpx_.*")
        .allowlist_var("VPX_.*")
        .generate()
        .expect("generating the libvpx bindings");
    let out = PathBuf::from(env::var("OUT_DIR").expect("cargo sets OUT_DIR"));
    bindings
        .write_to_file(out.join("vpx.rs"))
        .expect("writing the libvpx bindings");
}
