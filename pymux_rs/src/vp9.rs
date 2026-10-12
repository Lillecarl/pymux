//! The client's half of a window pane in VP9: libvpx decodes each frame,
//! and the rectangles that changed go back out as RGB rows, the way the
//! terminal reads them. pysmithay holds the encoder, on the server.
//! Lillecarl/pymux#639.

use std::ffi::CStr;
use std::sync::Mutex;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::PyBytes;

#[allow(
    non_upper_case_globals,
    non_camel_case_types,
    non_snake_case,
    dead_code,
    unused_imports,
    unsafe_op_in_unsafe_fn,
    clippy::all
)]
mod ffi {
    include!(concat!(env!("OUT_DIR"), "/vpx.rs"));
}

/// A rectangle in frame pixels: x, y, width, height.
type Rect = (i32, i32, i32, i32);

/// What `decode` answers for one rectangle: where it is, and its RGB rows.
type DecodedRect<'py> = (i32, i32, i32, i32, Bound<'py, PyBytes>);

/// A decoded frame's width and height, and the rectangles cut from it.
type Decoded = (u32, u32, Vec<(Rect, Vec<u8>)>);

struct Context(ffi::vpx_codec_ctx_t);

// SAFETY: libvpx keeps no thread-local state for a context; the Mutex
// in `Vp9Decoder` lets one thread at a time use it.
unsafe impl Send for Context {}

impl Context {
    fn error(&self) -> String {
        // SAFETY: libvpx answers a static string for any context.
        unsafe { CStr::from_ptr(ffi::vpx_codec_error(&self.0)) }
            .to_string_lossy()
            .into_owned()
    }
}

impl Drop for Context {
    fn drop(&mut self) {
        // SAFETY: the context was initialised by `new`, and dies once.
        unsafe { ffi::vpx_codec_destroy(&mut self.0) };
    }
}

/// One VP9 stream, decoded frame after frame. The stream holds its
/// reference frames, so a decoder belongs to one window pane.
#[pyclass(name = "Vp9Decoder")]
pub struct Vp9Decoder {
    context: Mutex<Context>,
}

#[pymethods]
impl Vp9Decoder {
    #[new]
    fn new() -> PyResult<Self> {
        // SAFETY: an all-zero context is what `vpx_codec_dec_init` expects.
        let mut context = Context(unsafe { std::mem::zeroed() });
        let config = ffi::vpx_codec_dec_cfg_t {
            threads: 4,
            w: 0,
            h: 0,
        };
        // SAFETY: `context` and `config` outlive the call.
        let status = unsafe {
            ffi::vpx_codec_dec_init_ver(
                &mut context.0,
                ffi::vpx_codec_vp9_dx(),
                &config,
                0,
                ffi::VPX_DECODER_ABI_VERSION as i32,
            )
        };
        if status != ffi::vpx_codec_err_t_VPX_CODEC_OK {
            // A failed init leaves nothing to destroy.
            let message = context.error();
            std::mem::forget(context);
            return Err(PyValueError::new_err(message));
        }
        Ok(Self {
            context: Mutex::new(context),
        })
    }

    /// Decode one frame, and answer `(width, height, rects)`: each of
    /// `rects` as `(x, y, w, h, rgb)`, cut to the frame. With no `rects`,
    /// the whole frame. The GIL is released while libvpx works.
    #[pyo3(signature = (data, rects=None))]
    fn decode<'py>(
        &self,
        py: Python<'py>,
        data: PyBackedBytes,
        rects: Option<Vec<Rect>>,
    ) -> PyResult<(u32, u32, Vec<DecodedRect<'py>>)> {
        let (width, height, cut) = py
            .detach(|| {
                let mut context = self
                    .context
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                decode(&mut context, &data, rects)
            })
            .map_err(PyValueError::new_err)?;
        let cut = cut
            .into_iter()
            .map(|((x, y, w, h), rgb)| (x, y, w, h, PyBytes::new(py, &rgb)))
            .collect();
        Ok((width, height, cut))
    }
}

fn decode(context: &mut Context, data: &[u8], rects: Option<Vec<Rect>>) -> Result<Decoded, String> {
    // SAFETY: `data` outlives the call; libvpx copies what it keeps.
    let status = unsafe {
        ffi::vpx_codec_decode(
            &mut context.0,
            data.as_ptr(),
            data.len() as u32,
            std::ptr::null_mut(),
            0,
        )
    };
    if status != ffi::vpx_codec_err_t_VPX_CODEC_OK {
        return Err(context.error());
    }
    let mut iterator: ffi::vpx_codec_iter_t = std::ptr::null();
    // SAFETY: the image lives until the next decode, which the lock holds off.
    let image = unsafe { ffi::vpx_codec_get_frame(&mut context.0, &mut iterator).as_ref() }
        .ok_or("the data held no frame")?;
    if image.fmt & ffi::VPX_IMG_FMT_HIGHBITDEPTH != 0 {
        return Err("a frame of more than eight bits".into());
    }
    let (width, height) = (image.d_w as i32, image.d_h as i32);
    let rects = rects.unwrap_or_else(|| vec![(0, 0, width, height)]);
    let cut = rects
        .into_iter()
        .filter_map(|rect| clip(rect, width, height))
        .map(|rect| (rect, rgb_rows(image, rect)))
        .collect();
    Ok((image.d_w, image.d_h, cut))
}

fn clip((x, y, w, h): Rect, width: i32, height: i32) -> Option<Rect> {
    let (left, top) = (x.max(0), y.max(0));
    let (right, bottom) = ((x + w).min(width), (y + h).min(height));
    (right > left && bottom > top).then_some((left, top, right - left, bottom - top))
}

/// The pixels of `rect` as RGB rows: full range BT.601, the inverse of
/// pysmithay's `rgb_to_yuv`.
fn rgb_rows(image: &ffi::vpx_image_t, (x, y, w, h): Rect) -> Vec<u8> {
    let (x_shift, y_shift) = (image.x_chroma_shift, image.y_chroma_shift);
    let plane = |index: usize, column: usize, row: usize| -> i32 {
        // SAFETY: libvpx gives each plane `stride` bytes a row, for
        // every row and column of the frame, cut down by the shifts.
        unsafe {
            i32::from(
                *image.planes[index]
                    .offset(row as isize * image.stride[index] as isize + column as isize),
            )
        }
    };
    let mut out = Vec::with_capacity(w as usize * h as usize * 3);
    for row in y as usize..(y + h) as usize {
        let chroma_row = row >> y_shift;
        for column in x as usize..(x + w) as usize {
            let chroma_column = column >> x_shift;
            let luma = plane(0, column, row);
            let u = plane(1, chroma_column, chroma_row) - 128;
            let v = plane(2, chroma_column, chroma_row) - 128;
            out.push(channel(luma, 91881 * v));
            out.push(channel(luma, -22554 * u - 46802 * v));
            out.push(channel(luma, 116130 * u));
        }
    }
    out
}

/// One channel: luma plus a chroma term in 1/65536ths, rounded and
/// clamped.
fn channel(luma: i32, term: i32) -> u8 {
    (luma + ((term + 32768) >> 16)).clamp(0, 255) as u8
}
