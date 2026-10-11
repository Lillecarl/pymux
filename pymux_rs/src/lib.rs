//! Rust kernels for pymux's image loops: the sixel encoder and the pixel
//! conversions in front of it. Each gives the bytes its Python original in
//! `pymux/sixel.py` gives, and runs with the GIL released, so a worker
//! thread encodes beside the event loop. pymux's `kernels.install()` puts
//! them in place; this package imports nothing of pymux, which depends on
//! it. Lillecarl/pymux#574.

use std::collections::{BTreeMap, HashMap};
use std::fmt::Write;

use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::PyBytes;

const MAX_COLORS: i64 = 255;
const MAX_PIXELS: i64 = 4 * 1024 * 1024;
const ALPHA_THRESHOLD: u8 = 128;
const DATA_LOW: u8 = 0x3f;
const MIN_RUN: usize = 4;

/// A colour as one number, red highest, so numbers sort as tuples do.
type Color = u32;

fn channel(color: Color, which: usize) -> u32 {
    (color >> (16 - 8 * which)) & 0xff
}

/// Python's `round` of a float: half to even.
fn round_py(value: f64) -> i64 {
    value.round_ties_even() as i64
}

/// One bucket of the median cut, with the spread of each channel kept,
/// since only a split changes it.
struct Bucket {
    colors: Vec<Color>,
    spreads: [i64; 3],
}

impl Bucket {
    fn new(colors: Vec<Color>) -> Self {
        let mut spreads = [0; 3];
        for (which, spread) in spreads.iter_mut().enumerate() {
            let values = colors.iter().map(|&color| channel(color, which));
            let (low, high) = values.fold((u32::MAX, 0), |(low, high), value| {
                (low.min(value), high.max(value))
            });
            *spread = if colors.is_empty() {
                0
            } else {
                i64::from(high) - i64::from(low)
            };
        }
        Self { colors, spreads }
    }
}

/// `pymux.sixel._median_cut`. The same bucket is cut at every step: the
/// first bucket, then the first channel, with the widest spread, and the
/// cut is a stable sort on that channel.
fn median_cut(colors: Vec<Color>, max_colors: usize) -> Vec<Vec<Color>> {
    let mut buckets = vec![Bucket::new(colors)];
    while buckets.len() < max_colors {
        let mut widest = -1;
        let mut chosen = None;
        let mut chosen_channel = 0;
        for (index, bucket) in buckets.iter().enumerate() {
            if bucket.colors.len() < 2 {
                continue;
            }
            for (which, &spread) in bucket.spreads.iter().enumerate() {
                if spread > widest {
                    widest = spread;
                    chosen = Some(index);
                    chosen_channel = which;
                }
            }
        }
        let Some(chosen) = chosen.filter(|_| widest > 0) else {
            break;
        };
        let mut colors = std::mem::take(&mut buckets[chosen].colors);
        colors.sort_by_key(|&color| channel(color, chosen_channel));
        let upper = colors.split_off(colors.len() / 2);
        buckets[chosen] = Bucket::new(colors);
        buckets.insert(chosen + 1, Bucket::new(upper));
    }
    buckets.into_iter().map(|bucket| bucket.colors).collect()
}

/// `pymux.sixel._palette`: the entries, and each colour's entry.
fn palette_of(
    order: &[Color],
    counts: &HashMap<Color, u64>,
    max_colors: usize,
) -> (Vec<[u64; 3]>, HashMap<Color, usize>) {
    if order.len() <= max_colors {
        let mut palette = order.to_vec();
        palette.sort_unstable();
        let mapping = palette
            .iter()
            .enumerate()
            .map(|(index, &color)| (color, index))
            .collect();
        let entries = palette
            .iter()
            .map(|&color| [0, 1, 2].map(|which| u64::from(channel(color, which))))
            .collect();
        return (entries, mapping);
    }
    let mut entries = Vec::new();
    let mut mapping = HashMap::new();
    for bucket in median_cut(order.to_vec(), max_colors) {
        if bucket.is_empty() {
            continue;
        }
        let weight: u64 = bucket.iter().map(|color| counts[color]).sum();
        let entry = [0, 1, 2].map(|which| {
            bucket
                .iter()
                .map(|color| u64::from(channel(*color, which)) * counts[color])
                .sum::<u64>()
                / weight
        });
        entries.push(entry);
        for color in bucket {
            mapping.insert(color, entries.len() - 1);
        }
    }
    (entries, mapping)
}

/// `pymux.sixel._runs`: one sixel row, repeats for the long runs, trailing
/// empty columns left out.
fn runs_into(out: &mut String, row: &[u8]) {
    let end = row
        .iter()
        .rposition(|&value| value != 0)
        .map_or(0, |at| at + 1);
    let mut index = 0;
    while index < end {
        let value = row[index];
        let mut run = 1;
        while index + run < end && row[index + run] == value {
            run += 1;
        }
        let char = char::from(DATA_LOW + value);
        if run >= MIN_RUN {
            let _ = write!(out, "!{run}{char}");
        } else {
            out.extend(std::iter::repeat_n(char, run));
        }
        index += run;
    }
}

fn encode(width: i64, height: i64, rgba: &[u8], max_colors: i64) -> Option<String> {
    if width <= 0 || height <= 0 || width * height > MAX_PIXELS {
        return None;
    }
    let (width, height) = (width as usize, height as usize);
    if rgba.len() < width * height * 4 {
        return None;
    }

    let mut order: Vec<Color> = Vec::new();
    let mut counts: HashMap<Color, u64> = HashMap::new();
    let mut indexed: Vec<Option<Color>> = vec![None; width * height];
    for (index, pixel) in rgba.chunks_exact(4).take(width * height).enumerate() {
        if pixel[3] < ALPHA_THRESHOLD {
            continue;
        }
        let color = (u32::from(pixel[0]) << 16) | (u32::from(pixel[1]) << 8) | u32::from(pixel[2]);
        indexed[index] = Some(color);
        let count = counts.entry(color).or_insert_with(|| {
            order.push(color);
            0
        });
        *count += 1;
    }
    if order.is_empty() {
        return None;
    }

    let max_colors = max_colors.clamp(1, MAX_COLORS) as usize;
    let (palette, mapping) = palette_of(&order, &counts, max_colors);

    let mut out = String::new();
    let _ = write!(out, "\x1bP0;1;0q\"1;1;{width};{height}");
    for (index, entry) in palette.iter().enumerate() {
        let [red, green, blue] = entry.map(|value| round_py((value * 100) as f64 / 255.0));
        let _ = write!(out, "#{index};2;{red};{green};{blue}");
    }

    for band in (0..height).step_by(6) {
        let mut rows: BTreeMap<usize, Vec<u8>> = BTreeMap::new();
        for offset in 0..6 {
            let y = band + offset;
            if y >= height {
                break;
            }
            let bit = 1 << offset;
            for (x, color) in indexed[y * width..(y + 1) * width].iter().enumerate() {
                let Some(color) = color else {
                    continue;
                };
                let row = rows.entry(mapping[color]).or_insert_with(|| vec![0; width]);
                row[x] |= bit;
            }
        }
        let mut first = true;
        for (entry, row) in &rows {
            let mut data = String::new();
            runs_into(&mut data, row);
            if data.is_empty() {
                continue;
            }
            if !first {
                out.push('$');
            }
            first = false;
            let _ = write!(out, "#{entry}{data}");
        }
        if band + 6 < height {
            out.push('-');
        }
    }
    out.push_str("\x1b\\");
    Some(out)
}

/// `pymux.sixel.encode_sixel`.
#[pyfunction]
#[pyo3(signature = (width, height, rgba, max_colors = MAX_COLORS))]
fn encode_sixel(
    py: Python<'_>,
    width: i64,
    height: i64,
    rgba: PyBackedBytes,
    max_colors: i64,
) -> Option<String> {
    py.detach(|| encode(width, height, &rgba, max_colors))
}

/// `pymux.sixel.scale_rgba` for a size that changes, on pixels that hold
/// the whole image: the nearest neighbour.
#[pyfunction]
fn scale_rgba<'py>(
    py: Python<'py>,
    rgba: PyBackedBytes,
    width: usize,
    height: usize,
    new_width: usize,
    new_height: usize,
) -> Bound<'py, PyBytes> {
    let out = py.detach(|| {
        let columns: Vec<usize> = (0..new_width).map(|x| x * width / new_width).collect();
        let mut out = Vec::with_capacity(new_width * new_height * 4);
        for y in 0..new_height {
            let source_row = (y * height / new_height) * width;
            for &column in &columns {
                let source = (source_row + column) * 4;
                out.extend_from_slice(&rgba[source..source + 4]);
            }
        }
        out
    });
    PyBytes::new(py, &out)
}

/// The RGB branch of `pymux.sixel.to_rgba`, on data that holds every pixel.
#[pyfunction]
fn rgb_to_rgba<'py>(py: Python<'py>, data: PyBackedBytes, pixels: usize) -> Bound<'py, PyBytes> {
    let out = py.detach(|| {
        let mut out = Vec::with_capacity(pixels * 4);
        for pixel in data.chunks_exact(3).take(pixels) {
            out.extend_from_slice(pixel);
            out.push(255);
        }
        out
    });
    PyBytes::new(py, &out)
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(encode_sixel, module)?)?;
    module.add_function(wrap_pyfunction!(scale_rgba, module)?)?;
    module.add_function(wrap_pyfunction!(rgb_to_rgba, module)?)?;
    Ok(())
}
