//! readaloud._core: the computational half of readaloud, in Rust.
//!
//! This file is the only one that knows about Python.  Every other module is
//! plain Rust over `&[char]` and `&str`, with offsets in code points, the way
//! the Python side counts them.

use pyo3::prelude::*;
use pyo3::types::PyString;

mod tables;
mod text;
mod width;

/// A Python str as Rust text.  A lone surrogate (from a command line argument
/// that was not UTF-8) becomes U+FFFD, one character for one, so every offset
/// stays where it was.
fn text_of(s: &Bound<'_, PyString>) -> String {
    if let Ok(text) = s.to_str() {
        return text.to_owned();
    }
    // Encoded with surrogatepass, the only bytes that are not UTF-8 are the
    // three of each surrogate.
    let bytes: Vec<u8> = s
        .call_method1("encode", ("utf-8", "surrogatepass"))
        .and_then(|b| b.extract())
        .unwrap_or_default();
    let mut out = String::with_capacity(bytes.len());
    let mut rest = &bytes[..];
    while !rest.is_empty() {
        match std::str::from_utf8(rest) {
            Ok(valid) => {
                out.push_str(valid);
                break;
            }
            Err(err) => {
                let (valid, bad) = rest.split_at(err.valid_up_to());
                out.push_str(std::str::from_utf8(valid).unwrap_or_default());
                out.push('\u{fffd}');
                rest = &bad[bad.len().min(3)..];
            }
        }
    }
    out
}

#[pyfunction]
fn char_width(ch: &Bound<'_, PyString>) -> usize {
    text_of(ch).chars().next().map_or(0, width::char_width)
}

#[pyfunction]
fn text_width(text: &Bound<'_, PyString>) -> usize {
    width::text_width(&text_of(text))
}

#[pyfunction]
fn cell_offsets(text: &Bound<'_, PyString>) -> Vec<usize> {
    width::cell_offsets(&text_of(text))
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(char_width, m)?)?;
    m.add_function(wrap_pyfunction!(text_width, m)?)?;
    m.add_function(wrap_pyfunction!(cell_offsets, m)?)?;
    Ok(())
}
