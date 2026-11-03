//! `pymux.client.kitty_rsync._Blocks`: the blocks of a kitty rsync
//! signature, and the rolling search for one of them in new bytes. The
//! search answers what the Python answers, with the GIL released.
//! Lillecarl/pymux#916.

use std::collections::HashMap;

use pyo3::buffer::PyBuffer;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::PyBytes;
use xxhash_rust::xxh3::xxh3_64;

const RECORD: usize = 20;
const WEAK_MASK: u32 = 0xffff;

/// The rsync rolling checksum of a whole block. kitty's sums wrap at
/// 2**32, a multiple of the modulus, so wrapping sums give the same
/// remainder.
fn weak_hash(block: &[u8]) -> (u32, u32) {
    let size = block.len() as u32;
    let (mut alpha, mut beta) = (0u32, 0u32);
    for (at, &byte) in block.iter().enumerate() {
        let byte = u32::from(byte);
        alpha = alpha.wrapping_add(byte);
        beta = beta.wrapping_add((size - at as u32).wrapping_mul(byte));
    }
    (alpha & WEAK_MASK, beta & WEAK_MASK)
}

/// `pymux.client.kitty_rsync._records`: the signature records of the
/// blocks of `run`, numbered from `first`. Lillecarl/pymux#921.
#[pyfunction]
pub fn rsync_records<'py>(
    py: Python<'py>,
    run: PyBackedBytes,
    block_size: usize,
    first: u64,
) -> PyResult<Bound<'py, PyBytes>> {
    if block_size == 0 {
        return Err(PyValueError::new_err("a block holds at least one byte"));
    }
    let out = py.detach(|| {
        let mut out = Vec::with_capacity(run.len().div_ceil(block_size) * RECORD);
        for (index, block) in (first..).zip(run.chunks(block_size)) {
            let (alpha, beta) = weak_hash(block);
            out.extend_from_slice(&index.to_le_bytes());
            out.extend_from_slice(&(alpha | (beta << 16)).to_le_bytes());
            out.extend_from_slice(&xxh3_64(block).to_le_bytes());
        }
        out
    });
    Ok(PyBytes::new(py, &out))
}

#[pyclass(module = "pymux_rs")]
pub struct RsyncBlocks {
    #[pyo3(get)]
    block_size: usize,
    /// Each weak hash, with the strong hash and the index of every block
    /// that has it, in signature order: the first that matches wins.
    by_weak: HashMap<u32, Vec<(u64, u64)>>,
}

impl RsyncBlocks {
    fn matching(&self, weak: u32, window: &[u8]) -> Option<u64> {
        let candidates = self.by_weak.get(&weak)?;
        let strong = xxh3_64(window);
        candidates
            .iter()
            .find(|(value, _)| *value == strong)
            .map(|&(_, index)| index)
    }

    fn search(&self, buffer: &[u8], mut start: usize, limit: usize) -> (usize, Option<u64>) {
        let size = self.block_size;
        if start + size > buffer.len() {
            return (start, None);
        }
        let (mut alpha, mut beta) = weak_hash(&buffer[start..start + size]);
        let step = size as u32;
        loop {
            if let Some(index) = self.matching(alpha | (beta << 16), &buffer[start..start + size]) {
                return (start, Some(index));
            }
            if start + size >= buffer.len() {
                return (start, None);
            }
            let leaving = u32::from(buffer[start]);
            let entering = u32::from(buffer[start + size]);
            alpha = alpha.wrapping_sub(leaving).wrapping_add(entering) & WEAK_MASK;
            beta = beta
                .wrapping_sub(step.wrapping_mul(leaving))
                .wrapping_add(alpha)
                & WEAK_MASK;
            start += 1;
            if start >= limit {
                return (start, None);
            }
        }
    }
}

#[pymethods]
impl RsyncBlocks {
    #[new]
    fn new(block_size: usize) -> Self {
        Self {
            block_size,
            by_weak: HashMap::new(),
        }
    }

    /// Hold the blocks of whole signature records.
    fn add(&mut self, records: PyBackedBytes) -> PyResult<()> {
        if records.len() % RECORD != 0 {
            return Err(PyValueError::new_err("a signature record is 20 bytes"));
        }
        for record in records.chunks_exact(RECORD) {
            let index = u64::from_le_bytes(record[0..8].try_into().unwrap());
            let weak = u32::from_le_bytes(record[8..12].try_into().unwrap());
            let strong = u64::from_le_bytes(record[12..20].try_into().unwrap());
            self.by_weak.entry(weak).or_default().push((strong, index));
        }
        Ok(())
    }

    /// `_Blocks.find`: from `start`, the first place a block of the
    /// signature stands and its index, or the place at `limit` or where
    /// the window cannot move on, with None.
    fn find(
        &self,
        py: Python<'_>,
        buffer: PyBuffer<u8>,
        start: usize,
        limit: usize,
    ) -> PyResult<(usize, Option<u64>)> {
        if !buffer.is_c_contiguous() {
            return Err(PyValueError::new_err("the buffer must be contiguous"));
        }
        // The export holds a bytearray's size fixed until `buffer` drops,
        // so the bytes stay where the pointer says while the GIL is away.
        let bytes = unsafe {
            std::slice::from_raw_parts(buffer.buf_ptr() as *const u8, buffer.len_bytes())
        };
        Ok(py.detach(|| self.search(bytes, start, limit)))
    }
}
