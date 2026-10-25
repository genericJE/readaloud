//! Words, sentences and chunks: the text half of `readaloud.document`.
//!
//! Each function here was a regular expression or a loop over one in
//! document.py.  The expressions used lookahead and lookbehind, which Rust's
//! `regex` crate does not have, so each is a scanner that tries the same
//! alternatives in the same order at each position and takes the first that
//! matches, the way Python's `re` does.  Positions count characters.

use crate::text::{
    is_alnum, is_decimal, is_digit, is_letter, is_lower, is_space, is_w, is_word_char, lower,
};

/// Abbreviations whose trailing '.' is part of the word and must NOT be read
/// as the end of a sentence.  Lowercase, without the dot.
pub const ABBREVIATIONS: &[&str] = &[
    "mr", "mrs", "ms", "mx", "dr", "prof", "rev", "fr", "hon", "sr", "jr", "st", "gen", "col",
    "sgt", "capt", "lt", "cmdr", "adm", "gov", "sen", "rep", "pres", "supt", "asst", "fig",
    "figs", "eq", "eqs", "ref", "refs", "sec", "secs", "ch", "chap", "chaps", "vol", "vols",
    "pt", "pts", "no", "nos", "pp", "p", "par", "para", "ln", "ver", "ed", "eds", "trans",
    "illus", "al", "cf", "viz", "etc", "vs", "approx", "est", "esp", "incl", "excl", "min",
    "max", "avg", "inc", "ltd", "co", "corp", "dept", "univ", "assn", "bros", "mfg", "jan",
    "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec", "mon", "tue",
    "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun", "e.g", "i.e", "a.m", "p.m",
    "u.s", "u.k", "u.n", "ph.d", "b.a", "m.a", "m.s", "b.s", "d.c", "a.d", "b.c",
];

fn is_abbreviation(word: &str) -> bool {
    ABBREVIATIONS.contains(&word)
}

fn at(t: &[char], i: usize) -> Option<char> {
    t.get(i).copied()
}

fn starts_with(t: &[char], i: usize, s: &str) -> bool {
    (i..).zip(s.chars()).all(|(k, c)| at(t, k) == Some(c))
}

// ---------------------------------------------------------------------------
// words
// ---------------------------------------------------------------------------

const SCHEMES: &[&str] = &["https", "http", "ftps", "ftp", "file", "mailto", "ssh", "git"];
const CURRENCY: &[char] = &['$', '£', '€', '¥', '₹', '₩'];
/// May sit between two letters or digits without ending the word:
/// apostrophes (it's, don't), hyphens (well-known, 2026-09-10), underscore
/// (mlx_audio), dot (12.5, www.example.com), colon (3:45pm), slash (TCP/IP)
/// and ampersand (AT&T).
const CONNECT: &[char] = &['\'', '\u{2019}', '\u{2bc}', '-', '\u{2010}', '\u{2011}', '_', '.', '/', ':', '&'];
/// Sentence punctuation trimmed off the end of a URL or an e-mail address.
const URL_TRAIL: &[char] = &['.', ',', ';', ':', '!', '?', '\'', '"', '’', '”'];

/// A URL: a scheme, "://" and everything up to a space or a closing
/// delimiter.
fn url_at(t: &[char], p: usize) -> Option<usize> {
    for scheme in SCHEMES {
        if !starts_with(t, p, scheme) {
            continue;
        }
        let mut i = p + scheme.chars().count();
        if !starts_with(t, i, "://") {
            continue;
        }
        i += 3;
        let body = i;
        while let Some(c) = at(t, i) {
            if is_space(c) || "<>\"'|)]".contains(c) {
                break;
            }
            i += 1;
        }
        if i > body {
            return Some(i);
        }
    }
    None
}

/// An e-mail address: name@host.domain, with at least one dot after the @.
fn email_at(t: &[char], p: usize) -> Option<usize> {
    if !at(t, p).is_some_and(is_word_char) {
        return None;
    }
    let mut i = p + 1;
    while at(t, i).is_some_and(|c| is_w(c) || ".+-".contains(c)) {
        i += 1;
    }
    if at(t, i) != Some('@') || !at(t, i + 1).is_some_and(is_word_char) {
        return None;
    }
    let host = |mut i: usize| {
        while at(t, i).is_some_and(|c| is_w(c) || c == '-') {
            i += 1;
        }
        i
    };
    i = host(i + 2);
    let mut parts = 0;
    while at(t, i) == Some('.') && at(t, i + 1).is_some_and(is_word_char) {
        i = host(i + 2);
        parts += 1;
    }
    (parts > 0).then_some(i)
}

/// An initialism, which keeps its dots: e.g. / U.S. / U.S.A / a.m.
fn initialism_at(t: &[char], p: usize) -> Option<usize> {
    let mut i = p;
    let mut pairs = 0;
    while at(t, i).is_some_and(is_letter) && at(t, i + 1) == Some('.') {
        i += 2;
        pairs += 1;
    }
    if pairs < 2 {
        return None;
    }
    if at(t, i).is_some_and(is_letter) && at(t, i + 1) != Some('.') {
        i += 1;
    }
    Some(i)
}

/// A known abbreviation with its dot, not followed by a word character:
/// Dr. / Fig. / etc.  Every one is ASCII letters, so the only one that can
/// fit at `p` is the whole run of ASCII letters there, ending at the dot.
fn abbrev_at(t: &[char], p: usize) -> Option<usize> {
    let mut n = 0;
    while at(t, p + n).is_some_and(|c| c.is_ascii_alphabetic()) {
        n += 1;
    }
    if n == 0 || at(t, p + n) != Some('.') || at(t, p + n + 1).is_some_and(is_w) {
        return None;
    }
    let word: String = t[p..p + n].iter().map(|c| c.to_ascii_lowercase()).collect();
    is_abbreviation(&word).then_some(p + n + 1)
}

/// The general case: letters and digits joined by single connectors, with an
/// optional currency sign in front of a digit and a percent sign after one.
fn word_at(t: &[char], p: usize) -> Option<usize> {
    let mut i = p;
    if at(t, i).is_some_and(|c| CURRENCY.contains(&c)) && at(t, i + 1).is_some_and(is_decimal) {
        i += 1;
    }
    if !at(t, i).is_some_and(is_word_char) {
        return None;
    }
    i += 1;
    loop {
        let c = at(t, i);
        let next_alnum = at(t, i + 1).is_some_and(is_word_char);
        let thousands = c == Some(',')
            && (1..=3).all(|k| at(t, i + k).is_some_and(is_decimal))
            && !at(t, i + 4).is_some_and(is_decimal);
        if (c.is_some_and(|c| CONNECT.contains(&c)) || thousands) && next_alnum {
            i += 2;
        } else if c.is_some_and(is_word_char) {
            i += 1;
        } else {
            break;
        }
    }
    if at(t, i) == Some('%') && is_decimal(t[i - 1]) {
        i += 1;
    }
    Some(i)
}

/// The characters an operator is made of, in a source file.
const OPERATOR: &[char] = &['=', '<', '>', '!', '+', '-', '*', '/', '%', '&', '|', '^', '~', '@'];

/// Char spans of every speakable word in one line of text.  With
/// `operators`, as in a source file, a run of operator characters outside
/// the words (`==`, `<=`, `->`, `*` before `args`) is a word of its own, so
/// it is lit while it is spoken; a colon joins such a run (`:=`), and a run
/// of colons alone is not one.
pub fn line_word_spans(t: &[char], operators: bool) -> Vec<(usize, usize)> {
    let mut out = words_of(t);
    if !operators {
        return out;
    }
    let mut p = 0;
    let mut k = 0;
    while p < t.len() {
        // skip the words, which may hold a connector or a percent sign
        while k < out.len() && out[k].1 <= p {
            k += 1;
        }
        if k < out.len() && out[k].0 <= p {
            p = out[k].1;
            continue;
        }
        let stop = out.get(k).map_or(t.len(), |w| w.0);
        let is_op = |c: char| OPERATOR.contains(&c) || c == ':';
        if is_op(t[p]) {
            let mut e = p;
            while e < stop && is_op(t[e]) {
                e += 1;
            }
            if t[p..e].iter().any(|c| OPERATOR.contains(c)) {
                out.push((p, e));
            }
            p = e;
        } else {
            p += 1;
        }
    }
    out.sort_unstable();
    out
}

/// Char spans of every word with a letter or digit in one line of text.
fn words_of(t: &[char]) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut p = 0;
    while p < t.len() {
        let (end, trim) = if let Some(e) = url_at(t, p) {
            (e, true)
        } else if let Some(e) = email_at(t, p) {
            (e, true)
        } else if let Some(e) = initialism_at(t, p).or_else(|| abbrev_at(t, p)) {
            (e, false)
        } else if let Some(e) = word_at(t, p) {
            (e, false)
        } else {
            p += 1;
            continue;
        };
        let mut e = end;
        if trim {
            while e > p && URL_TRAIL.contains(&t[e - 1]) {
                e -= 1;
            }
        }
        if t[p..e].iter().any(|&c| is_alnum(c)) {
            out.push((p, e));
        }
        p = end;
    }
    out
}

// ---------------------------------------------------------------------------
// sentences
// ---------------------------------------------------------------------------

const TERMINATORS: &[char] = &['.', '!', '?', '…', '‽'];
const CLOSERS: &[char] = &['"', '\'', '”', '’', '»', '›', ')', ']', '}'];

/// A run of terminators and closing quotes or brackets followed by a space or
/// by `hi`: where it ends.
fn sentence_end_at(t: &[char], p: usize, hi: usize) -> Option<usize> {
    let mut i = p;
    while i < hi && TERMINATORS.contains(&t[i]) {
        i += 1;
    }
    if i == p {
        return None;
    }
    while i < hi && CLOSERS.contains(&t[i]) {
        i += 1;
    }
    (i == hi || is_space(t[i])).then_some(i)
}

/// A dotted initialism as it looks before its final dot: "e.g", "U.S", "J".
fn is_initial(token: &[char]) -> bool {
    !token.is_empty()
        && token.len() % 2 == 1
        && token.iter().enumerate().all(|(k, &c)| if k % 2 == 0 { is_letter(c) } else { c == '.' })
}

/// Whether the terminator at `stop..end` really ends a sentence.
fn is_sentence_break(t: &[char], stop: usize, end: usize, lo: usize, hi: usize) -> bool {
    let mut i = stop;
    while i > lo && !is_space(t[i - 1]) {
        i -= 1;
    }
    let token = &t[i..stop];
    let mut j = end;
    while j < hi && is_space(t[j]) {
        j += 1;
    }
    // "he said. and then": a lowercase continuation is almost never a new
    // sentence, whatever the punctuation
    if j < hi && is_lower(t[j]) {
        return false;
    }
    if t[stop] == '.' {
        if is_abbreviation(&lower(token.iter().copied())) {
            return false; // Dr. / Fig. / etc.
        }
        if is_initial(token) {
            return false; // U.S. / e.g. / J. R. R.
        }
        if !token.is_empty() && token.len() <= 3 && token.iter().all(|&c| is_digit(c)) {
            return false; // "3." of a numbered list
        }
    }
    true
}

/// Sentence spans of `t[lo..hi]`, trimmed of spaces at both ends.
pub fn sentence_spans(t: &[char], lo: usize, hi: usize) -> Vec<(usize, usize)> {
    let mut spans = Vec::new();
    let mut cur = lo;
    let mut p = lo;
    while p < hi {
        let Some(end) = sentence_end_at(t, p, hi) else {
            p += 1;
            continue;
        };
        if end > cur && is_sentence_break(t, p, end, lo, hi) {
            let mut start = cur;
            while start < end && is_space(t[start]) {
                start += 1;
            }
            if start < end {
                spans.push((start, end));
            }
            cur = end;
        }
        p = end;
    }
    let (mut start, mut end) = (cur, hi);
    while start < end && is_space(t[start]) {
        start += 1;
    }
    while end > start && is_space(t[end - 1]) {
        end -= 1;
    }
    if start < end {
        spans.push((start, end));
    }
    spans
}

// ---------------------------------------------------------------------------
// line groups
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Blank,
    Code,
    Para,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::Blank => "blank",
            Kind::Code => "code",
            Kind::Para => "para",
        }
    }
}

fn is_blank(line: &[char]) -> bool {
    line.iter().all(|&c| is_space(c))
}

/// The fence character of a ``` or ~~~ line, indented at most three spaces.
fn fence(line: &[char]) -> Option<char> {
    let mut i = 0;
    while i < 3 && matches!(at(line, i), Some(' ' | '\t')) {
        i += 1;
    }
    let c = at(line, i)?;
    if (c == '`' || c == '~') && at(line, i + 1) == Some(c) && at(line, i + 2) == Some(c) {
        Some(c)
    } else {
        None
    }
}

fn is_indented(line: &[char]) -> bool {
    !is_blank(line) && (starts_with(line, 0, "    ") || at(line, 0) == Some('\t'))
}

/// A list marker at the start of a stripped line, or a quote's ">".
fn is_list_mark(s: &[char]) -> bool {
    let space_at = |i: usize| at(s, i).is_some_and(is_space);
    match at(s, 0) {
        Some('>') => true,
        Some(c) if "-*+•◦‣⁃·".contains(c) => space_at(1),
        Some(c) if is_decimal(c) => {
            let mut i = 0;
            while at(s, i).is_some_and(is_decimal) {
                i += 1;
            }
            matches!(at(s, i), Some('.' | ')')) && space_at(i + 1)
        }
        Some(c) if c.is_ascii_alphabetic() => matches!(at(s, 1), Some('.' | ')')) && space_at(2),
        _ => false,
    }
}

fn strip(line: &[char]) -> &[char] {
    let a = line.iter().position(|&c| !is_space(c)).unwrap_or(line.len());
    let b = line.iter().rposition(|&c| !is_space(c)).map_or(a, |b| b + 1);
    &line[a..b]
}

/// An indented line that is not just a deeply nested list item.
fn starts_code(line: &[char]) -> bool {
    is_indented(line) && !is_list_mark(strip(line))
}

/// Partition lines `[lo, hi)` into (start, end exclusive, kind) groups: blank
/// runs, code (fenced or indented, never split further) and paragraphs.
pub fn line_groups(lines: &[Vec<char>], lo: usize, hi: usize) -> Vec<(usize, usize, Kind)> {
    let mut groups = Vec::new();
    let n = hi;
    let mut i = lo;
    let mut prev_blank = lo == 0 || is_blank(&lines[lo - 1]);
    while i < n {
        if is_blank(&lines[i]) {
            let mut j = i;
            while j < n && is_blank(&lines[j]) {
                j += 1;
            }
            groups.push((i, j, Kind::Blank));
            i = j;
            prev_blank = true;
            continue;
        }
        if let Some(marker) = fence(&lines[i]) {
            let mut j = i + 1;
            while j < n {
                let close = fence(&lines[j]);
                j += 1;
                if close == Some(marker) {
                    break;
                }
            }
            groups.push((i, j, Kind::Code));
            i = j;
            prev_blank = false;
            continue;
        }
        if prev_blank && starts_code(&lines[i]) {
            let (mut j, mut last) = (i, i);
            while j < n {
                if is_indented(&lines[j]) {
                    last = j;
                    j += 1;
                } else if is_blank(&lines[j]) {
                    let mut k = j;
                    while k < n && is_blank(&lines[k]) {
                        k += 1;
                    }
                    if k < n && is_indented(&lines[k]) {
                        j = k; // a blank line inside the code block
                    } else {
                        break;
                    }
                } else {
                    break;
                }
            }
            groups.push((i, last + 1, Kind::Code));
            i = last + 1;
            prev_blank = false;
            continue;
        }
        let mut j = i;
        while j < n && !is_blank(&lines[j]) && fence(&lines[j]).is_none() {
            j += 1;
        }
        groups.push((i, j, Kind::Para));
        i = j;
        prev_blank = false;
    }
    groups
}

// ---------------------------------------------------------------------------
// chunk spans
// ---------------------------------------------------------------------------

const CLAUSE: &[char] = &[',', ';', ':', '—', '–'];
const CLAUSE_CLOSERS: &[char] = &['"', '\'', '”', '’', ')', ']'];

/// A clause terminator (and a closing quote) followed by a space before
/// `endpos`: where it ends.
fn clause_at(t: &[char], p: usize, endpos: usize) -> Option<usize> {
    if !CLAUSE.contains(&t[p]) {
        return None;
    }
    let space = |i: usize| i < endpos && is_space(t[i]);
    if p + 1 < endpos && CLAUSE_CLOSERS.contains(&t[p + 1]) && space(p + 2) {
        Some(p + 2)
    } else if space(p + 1) {
        Some(p + 1)
    } else {
        None
    }
}

/// Break an over-long sentence, never inside a word: at a line break, then a
/// clause terminator, then any space.  A single unbreakable token longer than
/// `max_chars` is kept whole.
pub fn split_long(t: &[char], lo: usize, hi: usize, max_chars: usize) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut cur = lo;
    while hi - cur > max_chars {
        let window = hi.min(cur + max_chars);
        let endpos = (window + 1).min(t.len());
        let mut cut: Option<usize> = (cur + 1..endpos).rev().find(|&k| t[k] == '\n');
        if cut.is_none() {
            let mut p = cur + 1;
            while p < endpos {
                match clause_at(t, p, endpos) {
                    Some(e) => {
                        cut = Some(e);
                        p = e;
                    }
                    None => p += 1,
                }
            }
            cut = cut.filter(|&c| c > cur);
        }
        if cut.is_none() {
            cut = (cur + 1..endpos).rev().find(|&k| is_space(t[k]));
        }
        if cut.is_none() {
            // nothing to break on inside the window: run on to the next space
            // rather than splitting a word in half
            cut = (window..hi).find(|&k| is_space(t[k])).filter(|&c| c > cur);
        }
        let Some(mut cut) = cut else { break };
        let mut end = cut;
        while end > cur && is_space(t[end - 1]) {
            end -= 1;
        }
        if end <= cur {
            break;
        }
        out.push((cur, end));
        while cut < hi && is_space(t[cut]) {
            cut += 1;
        }
        cur = cut;
    }
    if cur < hi {
        out.push((cur, hi));
    }
    out
}

/// Split one paragraph into chunk-sized spans at sentence boundaries.
pub fn paragraph_spans(
    t: &[char],
    lo: usize,
    hi: usize,
    max_sentences: usize,
    max_chars: usize,
) -> Vec<(usize, usize)> {
    let sentences = sentence_spans(t, lo, hi);
    if sentences.is_empty() {
        return vec![(lo, hi)];
    }
    let mut units = Vec::new();
    for (s, e) in sentences {
        if e - s > max_chars {
            units.extend(split_long(t, s, e, max_chars));
        } else {
            units.push((s, e));
        }
    }
    if units.is_empty() {
        return vec![(lo, hi)];
    }
    let mut out = Vec::new();
    let (mut start, mut end, mut count) = (units[0].0, units[0].1, 1);
    for &(s, e) in &units[1..] {
        if count >= max_sentences || e - start > max_chars {
            out.push((start, end));
            (start, end, count) = (s, e, 1);
        } else {
            end = e;
            count += 1;
        }
    }
    out.push((start, end));
    out
}

/// One chunk's span of the flat text: `(start, end, kind, line_start,
/// line_end)`, the line range end exclusive.
pub type Span = (usize, usize, Kind, usize, usize);

/// The chunk spans of every piece of the document outside its tables.
///
/// `lines` are the document's lines, joined by "\n" into the flat text the
/// spans index; `pieces` are `(first, stop)` line ranges; `word_at` is the
/// flat offset of every word outside the tables, ascending.  Blank runs and
/// code are one span each; a paragraph with words is split at sentence
/// boundaries into at most `max_sentences` sentences and `max_chars`
/// characters each.
pub fn prose_spans(
    lines: &[Vec<char>],
    pieces: &[(usize, usize)],
    word_at: &[usize],
    max_sentences: usize,
    max_chars: usize,
    split_code: bool,
) -> Vec<Vec<Span>> {
    let nlines = lines.len();
    let mut line_offsets = Vec::with_capacity(nlines);
    let mut flat: Vec<char> = Vec::new();
    for (k, line) in lines.iter().enumerate() {
        if k > 0 {
            flat.push('\n');
        }
        line_offsets.push(flat.len());
        flat.extend_from_slice(line);
    }
    let bisect_right = |xs: &[usize], x: usize| xs.partition_point(|&v| v <= x);
    let bisect_left = |xs: &[usize], x: usize| xs.partition_point(|&v| v < x);
    let clamp = |first: usize, stop: usize| {
        let first = first.min(nlines - 1);
        (first, stop.min(nlines).max(first + 1))
    };
    let mut out = Vec::with_capacity(pieces.len());
    for &(first, stop) in pieces {
        let mut spans = Vec::new();
        for (g_first, g_stop, kind) in line_groups(lines, first, stop) {
            let lo = line_offsets[g_first];
            let hi = line_offsets[g_stop - 1] + lines[g_stop - 1].len();
            let has_words = bisect_left(word_at, lo) < bisect_left(word_at, hi);
            // a code block is one chunk in Markdown; in a source file,
            // where indentation is structure and not a fence, it is cut
            // like a paragraph, so a class body is not one long chunk
            if (kind == Kind::Para || (split_code && kind == Kind::Code)) && has_words {
                for (a, b) in paragraph_spans(&flat, lo, hi, max_sentences, max_chars) {
                    let line_start = bisect_right(&line_offsets, a).saturating_sub(1);
                    let line_end = bisect_right(&line_offsets, if b > a { b - 1 } else { a });
                    let (ls, le) = clamp(line_start, line_end);
                    spans.push((a, b, kind, ls, le));
                }
            } else {
                let (ls, le) = clamp(g_first, g_stop);
                spans.push((lo, hi, kind, ls, le));
            }
        }
        out.push(spans);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn words(s: &str) -> Vec<String> {
        let t: Vec<char> = s.chars().collect();
        line_word_spans(&t, false).iter().map(|&(a, b)| t[a..b].iter().collect()).collect()
    }

    #[test]
    fn operators_are_words_in_a_source_file() {
        let t: Vec<char> = "return a == self.b and x <= col < y; f(*args) -> z: int = -1 # ok".chars().collect();
        let ops: Vec<String> = line_word_spans(&t, true)
            .iter()
            .map(|&(a, b)| t[a..b].iter().collect())
            .filter(|w: &String| !w.chars().any(is_alnum))
            .collect();
        assert_eq!(ops, ["==", "<=", "<", "*", "->", "=", "-"]);
        assert_eq!(line_word_spans(&t, false).len(), line_word_spans(&t, true).len() - ops.len());
    }

    #[test]
    fn words_hold_together() {
        assert_eq!(
            words("It's $4.50, e.g. at 3:45pm see https://x.org/a. Dr. mlx_audio 1,000 12%"),
            vec!["It's", "$4.50", "e.g.", "at", "3:45pm", "see", "https://x.org/a", "Dr.",
                 "mlx_audio", "1,000", "12%"]
        );
        assert_eq!(words("mail me@x.co. -- ok"), vec!["mail", "me@x.co", "ok"]);
        assert_eq!(words("settings.py:281-283"), vec!["settings.py:281-283"]);
    }

    #[test]
    fn sentences_skip_abbreviations() {
        let t: Vec<char> = "Dr. Who came. He left! e.g. this. 3. Item".chars().collect();
        let got: Vec<String> = sentence_spans(&t, 0, t.len())
            .iter()
            .map(|&(a, b)| t[a..b].iter().collect())
            .collect();
        assert_eq!(got, vec!["Dr. Who came.", "He left! e.g. this.", "3. Item"]);
    }
}
