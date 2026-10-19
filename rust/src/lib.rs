//! readaloud._core: the computational half of readaloud, in Rust.
//!
//! This file is the only one that knows about Python.  Every other module is
//! plain Rust over `&[char]` and `&str`, with offsets in code points, the way
//! the Python side counts them.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyString;

mod pronounce;
mod tables;
mod text;
mod width;
mod words;

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

/// The config file's pronunciations, ready to be found in text.
/// `readaloud.pronounce.Lexicon` wraps it.
#[pyclass(name = "Lexicon", frozen, module = "readaloud._core")]
struct PyLexicon {
    inner: pronounce::Lexicon,
}

#[pymethods]
impl PyLexicon {
    /// `pairs` are `(written, spoken, is_pattern)` in file order.
    #[new]
    #[pyo3(signature = (pairs = Vec::new()))]
    fn new(pairs: Vec<(Bound<'_, PyString>, Bound<'_, PyString>, bool)>) -> Self {
        let pairs: Vec<(String, String, bool)> =
            pairs.iter().map(|(w, s, p)| (text_of(w), text_of(s), *p)).collect();
        PyLexicon { inner: pronounce::Lexicon::new(&pairs) }
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    #[pyo3(signature = (text, symbols = Vec::new()))]
    fn find(
        &self,
        py: Python<'_>,
        text: &Bound<'_, PyString>,
        symbols: Vec<(usize, usize, Bound<'_, PyString>)>,
    ) -> Vec<pronounce::Found> {
        let text = text_of(text);
        let symbols: Vec<(usize, usize, String)> =
            symbols.iter().map(|(s, e, sym)| (*s, *e, text_of(sym))).collect();
        py.detach(|| self.inner.find(&text, &symbols))
    }
}

/// `text` with every match said its way: `(text, offsets, word_texts)`.
#[pyfunction]
fn respell(
    text: &Bound<'_, PyString>,
    slots: Vec<(usize, usize)>,
    matches: Vec<(usize, usize, Bound<'_, PyString>)>,
) -> PyResult<(String, Vec<usize>, Vec<String>)> {
    let matches: Vec<(usize, usize, String)> =
        matches.iter().map(|(s, e, say)| (*s, *e, text_of(say))).collect();
    pronounce::respell(&text_of(text), &slots, &matches).map_err(PyValueError::new_err)
}

/// Why a pronunciation pattern cannot be used, or None.
#[pyfunction]
fn check_pattern(pattern: &str) -> Option<String> {
    pronounce::check_pattern(pattern)
}

fn chars_of(s: &Bound<'_, PyString>) -> Vec<char> {
    text_of(s).chars().collect()
}

fn lines_of(lines: &[Bound<'_, PyString>]) -> Vec<Vec<char>> {
    lines.iter().map(chars_of).collect()
}

/// Char spans of every speakable word in one line of text.
#[pyfunction]
fn line_word_spans(text: &Bound<'_, PyString>) -> Vec<(usize, usize)> {
    words::line_word_spans(&chars_of(text))
}

/// The word spans of every line, one list per line.
#[pyfunction]
fn lines_word_spans(lines: Vec<Bound<'_, PyString>>) -> Vec<Vec<(usize, usize)>> {
    lines.iter().map(|line| words::line_word_spans(&chars_of(line))).collect()
}

/// Sentence spans of `text[lo:hi]`, as absolute offsets.
#[pyfunction]
#[pyo3(signature = (text, lo = 0, hi = None))]
fn sentence_spans(text: &Bound<'_, PyString>, lo: usize, hi: Option<usize>) -> Vec<(usize, usize)> {
    let t = chars_of(text);
    let hi = hi.unwrap_or(t.len()).min(t.len());
    words::sentence_spans(&t, lo.min(hi), hi)
}

/// `(start, end, kind)` line groups of `plain[lo:hi]`.
#[pyfunction]
#[pyo3(signature = (plain, lo = 0, hi = None))]
fn line_groups(
    plain: Vec<Bound<'_, PyString>>,
    lo: usize,
    hi: Option<usize>,
) -> Vec<(usize, usize, &'static str)> {
    let lines = lines_of(&plain);
    let hi = hi.unwrap_or(lines.len()).min(lines.len());
    words::line_groups(&lines, lo.min(hi), hi)
        .into_iter()
        .map(|(a, b, k)| (a, b, k.name()))
        .collect()
}

/// An over-long sentence `text[lo:hi]` cut into spans of at most `max_chars`.
#[pyfunction]
fn split_long(text: &Bound<'_, PyString>, lo: usize, hi: usize, max_chars: usize) -> Vec<(usize, usize)> {
    let t = chars_of(text);
    let hi = hi.min(t.len());
    words::split_long(&t, lo.min(hi), hi, max_chars.max(1))
}

/// One paragraph `text[lo:hi]` cut into chunk-sized spans.
#[pyfunction]
fn paragraph_spans(
    text: &Bound<'_, PyString>,
    lo: usize,
    hi: usize,
    max_sentences: usize,
    max_chars: usize,
) -> Vec<(usize, usize)> {
    let t = chars_of(text);
    let hi = hi.min(t.len());
    words::paragraph_spans(&t, lo.min(hi), hi, max_sentences.max(1), max_chars.max(1))
}

/// A chunk span as Python sees it: (start, end, kind, line_start, line_end).
type PySpan = (usize, usize, &'static str, usize, usize);

/// `(start, end, kind, line_start, line_end)` of every chunk outside the
/// tables, one list per `(first, stop)` piece.
#[pyfunction]
fn prose_spans(
    plain: Vec<Bound<'_, PyString>>,
    pieces: Vec<(usize, usize)>,
    word_at: Vec<usize>,
    max_sentences: usize,
    max_chars: usize,
) -> PyResult<Vec<Vec<PySpan>>> {
    let lines = lines_of(&plain);
    if pieces.iter().any(|&(a, b)| a >= b || b > lines.len()) {
        return Err(PyValueError::new_err("prose_spans: a piece outside the lines"));
    }
    Ok(words::prose_spans(&lines, &pieces, &word_at, max_sentences.max(1), max_chars.max(1))
        .into_iter()
        .map(|spans| spans.into_iter().map(|(a, b, k, ls, le)| (a, b, k.name(), ls, le)).collect())
        .collect())
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(char_width, m)?)?;
    m.add_function(wrap_pyfunction!(text_width, m)?)?;
    m.add_function(wrap_pyfunction!(cell_offsets, m)?)?;
    m.add_class::<PyLexicon>()?;
    m.add_function(wrap_pyfunction!(respell, m)?)?;
    m.add_function(wrap_pyfunction!(check_pattern, m)?)?;
    m.add("ABBREVIATIONS", words::ABBREVIATIONS.to_vec())?;
    m.add_function(wrap_pyfunction!(line_word_spans, m)?)?;
    m.add_function(wrap_pyfunction!(lines_word_spans, m)?)?;
    m.add_function(wrap_pyfunction!(sentence_spans, m)?)?;
    m.add_function(wrap_pyfunction!(line_groups, m)?)?;
    m.add_function(wrap_pyfunction!(split_long, m)?)?;
    m.add_function(wrap_pyfunction!(paragraph_spans, m)?)?;
    m.add_function(wrap_pyfunction!(prose_spans, m)?)?;
    Ok(())
}
