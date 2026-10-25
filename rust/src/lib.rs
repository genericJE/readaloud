//! readaloud._core: the computational half of readaloud, in Rust.
//!
//! This file is the only one that knows about Python.  Every other module is
//! plain Rust over `&[char]` and `&str`, with offsets in code points, the way
//! the Python side counts them.

use std::path::PathBuf;
use std::sync::Arc;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyCapsule, PyCapsuleMethods, PyString};

mod align;
mod ansi;
mod pronounce;
mod shapes;
mod syntax;
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
    /// `pairs` are `(written, spoken, is_pattern)` in file order; `shapes`
    /// are `(pattern, wording)` in file order, and `subs` the named shapes,
    /// `(name, pattern, wording)`.  `grammar` and `rules`, `(kind, wording)`,
    /// say a source file's tree once `parse` has been given the file.
    #[new]
    #[pyo3(signature = (pairs = Vec::new(), shapes = Vec::new(), subs = Vec::new(), grammar = None, rules = Vec::new()))]
    fn new(
        pairs: Vec<(Bound<'_, PyString>, Bound<'_, PyString>, bool)>,
        shapes: Vec<(Bound<'_, PyString>, Bound<'_, PyString>)>,
        subs: Vec<(Bound<'_, PyString>, Bound<'_, PyString>, Bound<'_, PyString>)>,
        grammar: Option<PyRef<'_, PyGrammar>>,
        rules: Vec<(Bound<'_, PyString>, Bound<'_, PyString>)>,
    ) -> Self {
        let pairs: Vec<(String, String, bool)> =
            pairs.iter().map(|(w, s, p)| (text_of(w), text_of(s), *p)).collect();
        let shapes: Vec<(String, String)> =
            shapes.iter().map(|(pattern, say)| (text_of(pattern), text_of(say))).collect();
        let subs: Vec<(String, String, String)> =
            subs.iter().map(|(n, p, s)| (text_of(n), text_of(p), text_of(s))).collect();
        let rules: Vec<(String, String)> = rules.iter().map(|(k, w)| (text_of(k), text_of(w))).collect();
        let syntax = grammar.map(|g| syntax::Syntax::new(g.inner.clone(), &rules));
        PyLexicon { inner: pronounce::Lexicon::new(&pairs, &shapes, &subs, syntax) }
    }

    /// Whether `find` wants the word slots: there are shapes, or a grammar.
    fn uses_slots(&self) -> bool {
        self.inner.uses_slots()
    }

    /// Parse `text`, the whole document, with the grammar; False without one
    /// or when it could not parse.
    fn parse(&self, py: Python<'_>, text: &Bound<'_, PyString>) -> bool {
        let text = text_of(text);
        py.detach(|| self.inner.parse(&text))
    }

    fn __len__(&self) -> usize {
        self.inner.len()
    }

    /// How many shapes are on.
    fn shape_count(&self) -> usize {
        self.inner.shape_count()
    }

    #[pyo3(signature = (text, symbols = Vec::new(), slots = Vec::new(), at = None))]
    fn find(
        &self,
        py: Python<'_>,
        text: &Bound<'_, PyString>,
        symbols: Vec<(usize, usize, Bound<'_, PyString>)>,
        slots: Vec<(usize, usize)>,
        at: Option<usize>,
    ) -> Vec<pronounce::Found> {
        let text = text_of(text);
        let symbols: Vec<(usize, usize, String)> =
            symbols.iter().map(|(s, e, sym)| (*s, *e, text_of(sym))).collect();
        py.detach(|| self.inner.find(&text, &symbols, &slots, at))
    }
}

/// A language's grammar for `syntax_tree`.  `readaloud.syntax` wraps it.
#[pyclass(name = "Grammar", frozen, module = "readaloud._core")]
struct PyGrammar {
    inner: Arc<syntax::Grammar>,
    /// the capsule the grammar came from, kept alive with it
    _capsule: Option<Py<PyAny>>,
}

#[pymethods]
impl PyGrammar {
    /// From the `tree_sitter.Language` capsule that
    /// `tree_sitter_<language>.language()` returns.
    #[staticmethod]
    fn from_capsule(capsule: &Bound<'_, PyAny>) -> PyResult<Self> {
        let wrong = || {
            PyTypeError::new_err(
                "a grammar is the tree_sitter.Language capsule that tree_sitter_<language>.language() returns",
            )
        };
        let capsule = capsule.cast::<PyCapsule>().map_err(|_| wrong())?;
        // the pointer, if the capsule is valid and carries that name
        let pointer = capsule.pointer_checked(Some(c"tree_sitter.Language")).map_err(|_| wrong())?;
        // SAFETY: the capsule holds a TSLanguage that is static data of the
        // grammar's module, and the capsule is kept alive with the grammar
        let inner = unsafe { syntax::Grammar::from_raw(pointer.as_ptr()) }.map_err(PyValueError::new_err)?;
        Ok(PyGrammar { inner: Arc::new(inner), _capsule: Some(capsule.clone().into_any().unbind()) })
    }

    /// From the shared library `path`, which exports `tree_sitter_<name>`,
    /// as a Neovim `parser/<name>.so` does.
    #[staticmethod]
    fn load(path: PathBuf, name: &str) -> PyResult<Self> {
        let inner = syntax::Grammar::load(&path, name).map_err(PyValueError::new_err)?;
        Ok(PyGrammar { inner: Arc::new(inner), _capsule: None })
    }

    #[getter]
    fn abi_version(&self) -> usize {
        self.inner.abi_version()
    }

    /// The kinds of node the grammar makes, named ones first.
    fn node_kinds(&self) -> Vec<String> {
        self.inner.node_kinds()
    }

    /// The names of the fields the grammar's nodes have.
    fn field_names(&self) -> Vec<String> {
        self.inner.field_names()
    }

    /// Every stretch of `text` the highlights `query` colours: `(start, end,
    /// name)` in characters, `name` an index into `names`.
    fn highlight(
        &self,
        py: Python<'_>,
        text: &Bound<'_, PyString>,
        query: &str,
        names: Vec<String>,
    ) -> PyResult<Vec<(usize, usize, usize)>> {
        let text = text_of(text);
        py.detach(|| self.inner.highlight(&text, query, &names)).map_err(PyValueError::new_err)
    }
}

/// The syntax tree `grammar` makes of `text`, one node per line.
#[pyfunction]
fn syntax_tree(py: Python<'_>, text: &Bound<'_, PyString>, grammar: &PyGrammar) -> PyResult<String> {
    let text = text_of(text);
    py.detach(|| {
        let tree = grammar.inner.parse(&text).ok_or_else(|| PyValueError::new_err("the grammar could not parse"))?;
        Ok(syntax::outline(&tree, &text))
    })
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

/// `(problem, None)` when `pattern` cannot be a shape, else `(None, (group
/// names, group count))`.
#[pyfunction]
fn check_shape(pattern: &str) -> (Option<String>, Option<(Vec<String>, usize)>) {
    match shapes::check_shape(pattern) {
        Ok(groups) => (None, Some(groups)),
        Err(problem) => (Some(problem), None),
    }
}

/// Why `pattern` does not compile, or None.  A part may match empty text.
#[pyfunction]
fn check_regex(pattern: &str) -> Option<String> {
    regex::Regex::new(pattern).err().map(|e| {
        let text = e.to_string();
        text.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or(&text).trim()
            .trim_start_matches("error: ").to_string()
    })
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
#[pyo3(signature = (plain, pieces, word_at, max_sentences, max_chars, split_code = false))]
fn prose_spans(
    plain: Vec<Bound<'_, PyString>>,
    pieces: Vec<(usize, usize)>,
    word_at: Vec<usize>,
    max_sentences: usize,
    max_chars: usize,
    split_code: bool,
) -> PyResult<Vec<Vec<PySpan>>> {
    let lines = lines_of(&plain);
    if pieces.iter().any(|&(a, b)| a >= b || b > lines.len()) {
        return Err(PyValueError::new_err("prose_spans: a piece outside the lines"));
    }
    Ok(words::prose_spans(&lines, &pieces, &word_at, max_sentences.max(1), max_chars.max(1), split_code)
        .into_iter()
        .map(|spans| spans.into_iter().map(|(a, b, k, ls, le)| (a, b, k.name(), ls, le)).collect())
        .collect())
}

/// A style as Python sees it: (fg, bg, bold, dim, italic, underline,
/// reverse, href), the fields of `readaloud.ansi.Style` in order.
type PyStyle = (Option<i64>, Option<i64>, bool, bool, bool, bool, bool, Option<String>);
/// Lines of (text, style index) runs, and the styles they index.
type PyLines = (Vec<PyStyle>, Vec<ansi::Line>);

fn style_of(t: PyStyle) -> ansi::Style {
    let (fg, bg, bold, dim, italic, underline, reverse, href) = t;
    ansi::Style { fg, bg, bold, dim, italic, underline, reverse, href }
}

fn tuple_of(s: ansi::Style) -> PyStyle {
    (s.fg, s.bg, s.bold, s.dim, s.italic, s.underline, s.reverse, s.href)
}

/// `data` as lines of runs: `(styles, lines)`, each run `(text, style
/// index)`.  Style 0 is the default.
#[pyfunction]
fn parse(py: Python<'_>, data: &Bound<'_, PyString>) -> PyLines {
    let data = text_of(data);
    let (styles, lines) = py.detach(|| ansi::parse(&data));
    (styles.into_iter().map(tuple_of).collect(), lines)
}

#[pyfunction]
fn has_ansi(data: &Bound<'_, PyString>) -> bool {
    ansi::has_ansi(&text_of(data))
}

/// `style` with one SGR parameter body applied.
#[pyfunction]
fn apply_sgr(style: PyStyle, body: &Bound<'_, PyString>) -> PyStyle {
    tuple_of(ansi::apply_sgr(&style_of(style), &text_of(body)))
}

#[pyfunction]
fn rgb_to_x256(r: i64, g: i64, b: i64) -> i64 {
    ansi::rgb_to_x256(r, g, b)
}

/// Raw Markdown lines lightly de-syntaxed: `(styles, lines)` as `parse`
/// returns them, the given styles first and in order.
#[pyfunction]
fn strip_markdown(py: Python<'_>, styles: Vec<PyStyle>, lines: Vec<ansi::Line>) -> PyResult<PyLines> {
    let styles: Vec<ansi::Style> = styles.into_iter().map(style_of).collect();
    if lines.iter().flatten().any(|&(_, k)| k >= styles.len()) {
        return Err(PyValueError::new_err("strip_markdown: a run's style is not among the styles"));
    }
    let (styles, lines) = py.detach(|| ansi::strip_markdown(styles, lines));
    Ok((styles.into_iter().map(tuple_of).collect(), lines))
}

/// A misaki token as the glue hands it over: (text, start_ts, end_ts).
type PyToken<'py> = (Bound<'py, PyString>, Option<f64>, Option<f64>);

/// `needle`'s char span in `text` at or after `cursor`, or None.
#[pyfunction]
fn locate(text: &Bound<'_, PyString>, needle: &Bound<'_, PyString>, cursor: usize) -> Option<(usize, usize)> {
    align::locate(&chars_of(text), &chars_of(needle), cursor)
}

/// `(slot, start, end)` for every word slot, from one `(tokens, seconds)`
/// pair per pipeline Result.
#[pyfunction]
fn align_words(
    py: Python<'_>,
    text: &Bound<'_, PyString>,
    spans: Vec<(i64, i64)>,
    results: Vec<(Vec<PyToken<'_>>, f64)>,
) -> Vec<align::Timing> {
    let text = chars_of(text);
    let results: Vec<(Vec<align::Token>, f64)> = results
        .iter()
        .map(|(tokens, duration)| {
            let tokens = tokens
                .iter()
                .map(|(t, start, end)| align::Token { text: chars_of(t), start: *start, end: *end })
                .collect();
            (tokens, *duration)
        })
        .collect();
    py.detach(|| align::align_words(&text, &spans, &results))
}

/// One `(start, end)` char span per word slot inside `text`.
#[pyfunction]
#[pyo3(signature = (text, offsets, word_texts = None))]
fn word_spans(
    text: &Bound<'_, PyString>,
    offsets: Vec<Option<usize>>,
    word_texts: Option<Vec<Bound<'_, PyString>>>,
) -> Vec<(usize, usize)> {
    let word_texts: Option<Vec<Vec<char>>> = word_texts.map(|w| lines_of(&w));
    align::word_spans(&chars_of(text), &offsets, word_texts.as_deref())
}

/// `duration` spread over `nslots` slots by span width.
#[pyfunction]
fn even_timings(spans: Vec<(i64, i64)>, duration: f64, nslots: i64) -> Vec<align::Timing> {
    align::even_timings(&spans, duration, nslots)
}

/// `(slot, start, end)` timings clamped and made monotonic; ValueError when
/// they do not fit the slots.
#[pyfunction]
fn sanitize(timings: Vec<(i64, f64, f64)>, nslots: i64, duration: f64) -> PyResult<Vec<align::Timing>> {
    align::sanitize(&timings, nslots, duration).map_err(PyValueError::new_err)
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(char_width, m)?)?;
    m.add_function(wrap_pyfunction!(text_width, m)?)?;
    m.add_function(wrap_pyfunction!(cell_offsets, m)?)?;
    m.add_class::<PyLexicon>()?;
    m.add_function(wrap_pyfunction!(check_shape, m)?)?;
    m.add_function(wrap_pyfunction!(check_regex, m)?)?;
    m.add_function(wrap_pyfunction!(respell, m)?)?;
    m.add_function(wrap_pyfunction!(check_pattern, m)?)?;
    m.add_function(wrap_pyfunction!(parse, m)?)?;
    m.add_function(wrap_pyfunction!(has_ansi, m)?)?;
    m.add_function(wrap_pyfunction!(apply_sgr, m)?)?;
    m.add_function(wrap_pyfunction!(rgb_to_x256, m)?)?;
    m.add_function(wrap_pyfunction!(strip_markdown, m)?)?;
    m.add("ABBREVIATIONS", words::ABBREVIATIONS.to_vec())?;
    m.add_function(wrap_pyfunction!(line_word_spans, m)?)?;
    m.add_function(wrap_pyfunction!(lines_word_spans, m)?)?;
    m.add_function(wrap_pyfunction!(sentence_spans, m)?)?;
    m.add_function(wrap_pyfunction!(line_groups, m)?)?;
    m.add_function(wrap_pyfunction!(split_long, m)?)?;
    m.add_function(wrap_pyfunction!(paragraph_spans, m)?)?;
    m.add_function(wrap_pyfunction!(prose_spans, m)?)?;
    m.add_function(wrap_pyfunction!(locate, m)?)?;
    m.add_function(wrap_pyfunction!(align_words, m)?)?;
    m.add_function(wrap_pyfunction!(word_spans, m)?)?;
    m.add_function(wrap_pyfunction!(even_timings, m)?)?;
    m.add_function(wrap_pyfunction!(sanitize, m)?)?;
    m.add_class::<PyGrammar>()?;
    m.add_function(wrap_pyfunction!(syntax_tree, m)?)?;
    Ok(())
}
