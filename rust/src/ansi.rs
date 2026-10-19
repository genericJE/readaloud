//! Escape sequences to styled runs, and the light Markdown cleanup of input
//! that has none: the computation of `readaloud.ansi`.
//!
//! A style is a value; every distinct one gets an id in a [`Styles`] table,
//! and a run is its text with the id of its style.  The Python side turns the
//! table back into its frozen `Style` dataclasses.  The Markdown rewrites were
//! regular expressions with lookbehind and backreferences; each is a matcher
//! that tries the same thing at each position that Python's `re` tried, and
//! the rewrites run in the same order over the same masked text.

use std::collections::HashMap;

use crate::text::{is_space, is_w};

pub const TAB_WIDTH: usize = 4;

#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct Style {
    pub fg: Option<i64>,
    pub bg: Option<i64>,
    pub bold: bool,
    pub dim: bool,
    pub italic: bool,
    pub underline: bool,
    pub reverse: bool,
    pub href: Option<String>,
}

/// Every style in use, each once, by id.
#[derive(Default)]
pub struct Styles {
    pub list: Vec<Style>,
    index: HashMap<Style, usize>,
}

impl Styles {
    pub fn new(seed: Vec<Style>) -> Self {
        let mut styles = Styles::default();
        for style in seed {
            styles.list.push(style.clone());
            styles.index.entry(style).or_insert(styles.list.len() - 1);
        }
        styles
    }

    pub fn id(&mut self, style: &Style) -> usize {
        if let Some(&i) = self.index.get(style) {
            return i;
        }
        self.list.push(style.clone());
        self.index.insert(style.clone(), self.list.len() - 1);
        self.list.len() - 1
    }

    fn with(&mut self, id: usize, change: impl FnOnce(&mut Style)) -> usize {
        let mut style = self.list[id].clone();
        change(&mut style);
        self.id(&style)
    }
}

/// One line: its runs, as (text, style id).
pub type Line = Vec<(String, usize)>;

// --------------------------------------------------------------------------
// colours
// --------------------------------------------------------------------------

const ANSI16_RGB: [(i64, i64, i64); 16] = [
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
];
const CUBE: [i64; 6] = [0, 95, 135, 175, 215, 255];

fn xterm_rgb(idx: i64) -> (i64, i64, i64) {
    let idx = idx.clamp(0, 255);
    if idx < 16 {
        return ANSI16_RGB[idx as usize];
    }
    if idx < 232 {
        let n = (idx - 16) as usize;
        return (CUBE[n / 36], CUBE[(n / 6) % 6], CUBE[n % 6]);
    }
    let v = 8 + 10 * (idx - 232);
    (v, v, v)
}

fn dist(a: (i64, i64, i64), b: (i64, i64, i64)) -> i64 {
    let (dr, dg, db) = (a.0 - b.0, a.1 - b.1, a.2 - b.2);
    3 * dr * dr + 6 * dg * dg + db * db // BT.601-ish weighting
}

/// The nearest 0-255 xterm palette index to a truecolour triple.
pub fn rgb_to_x256(r: i64, g: i64, b: i64) -> i64 {
    let q = |v: i64| {
        let v = v.clamp(0, 255);
        (0..6).min_by_key(|&i| (CUBE[i] - v).abs()).unwrap() as i64
    };
    let cube = 16 + 36 * q(r) + 6 * q(g) + q(b);
    let mean = (r as f64 + g as f64 + b as f64) / 3.0;
    let grey = 232 + (((mean - 8.0) / 10.0).round_ties_even() as i64).clamp(0, 23);
    let target = (r.clamp(0, 255), g.clamp(0, 255), b.clamp(0, 255));
    if dist(xterm_rgb(grey), target) < dist(xterm_rgb(cube), target) { grey } else { cube }
}

// --------------------------------------------------------------------------
// SGR
// --------------------------------------------------------------------------

/// A numeric parameter: its value (saturating, for comparing) and its low
/// byte (exact, for `& 0xFF`).
#[derive(Clone, Copy)]
struct Param {
    v: u64,
    low: u8,
}

impl Param {
    fn rgb(self) -> i64 {
        self.v.min(i64::MAX as u64) as i64
    }
}

/// ECMA-48: an omitted (or empty) parameter takes its default value, 0.
fn to_int(field: &str) -> Param {
    if field.is_empty() || !field.bytes().all(|b| b.is_ascii_digit()) {
        return Param { v: 0, low: 0 };
    }
    let (mut v, mut low) = (0u64, 0u8);
    for b in field.bytes() {
        let d = u64::from(b - b'0');
        v = v.saturating_mul(10).saturating_add(d);
        low = low.wrapping_mul(10).wrapping_add(d as u8);
    }
    Param { v, low }
}

#[derive(Clone, Copy)]
enum Op {
    Int(Param),
    /// a colour the colon form resolved: (background?, index)
    Colour(bool, i64),
}

/// Split an SGR parameter body into ops: plain parameters, and the colours
/// of the colon-separated ISO 8613-6 form (`38:5:196` / `38:2::R:G:B`).
fn sgr_ops(body: &str) -> Vec<Op> {
    let mut ops = Vec::new();
    for field in body.split(';') {
        if !field.contains(':') {
            ops.push(Op::Int(to_int(field)));
            continue;
        }
        let subs: Vec<&str> = field.split(':').collect();
        let head = to_int(subs[0]).v;
        if head != 38 && head != 48 {
            continue; // 58 (underline colour) and friends
        }
        let bg = head == 48;
        let mode = subs.get(1).map_or(0, |s| to_int(s).v);
        if mode == 5 && subs.len() > 2 {
            ops.push(Op::Colour(bg, i64::from(to_int(subs[2]).low)));
        } else if mode == 2 {
            let nums: Vec<i64> = subs[2..].iter().map(|s| to_int(s).rgb()).collect();
            let rgb = match nums.len() {
                n if n >= 4 => &nums[1..4], // 38:2:<colour-space-id>:R:G:B
                3 => &nums[..],              // 38:2:R:G:B
                _ => continue,
            };
            ops.push(Op::Colour(bg, rgb_to_x256(rgb[0], rgb[1], rgb[2])));
        }
    }
    ops
}

/// Apply one `CSI ... m` parameter body to `style`.  The href survives SGR 0
/// on purpose: OSC 8 sets it, and mdcat emits `ESC[0m` inside a hyperlink.
pub fn apply_sgr(style: &Style, body: &str) -> Style {
    let ops = if body.is_empty() { vec![Op::Int(Param { v: 0, low: 0 })] } else { sgr_ops(body) };
    let mut s = style.clone();
    let int_at = |k: usize| match ops.get(k) {
        Some(Op::Int(p)) => Some(*p),
        _ => None,
    };
    let mut i = 0;
    while i < ops.len() {
        let p = match ops[i] {
            Op::Colour(bg, idx) => {
                if bg { s.bg = Some(idx) } else { s.fg = Some(idx) }
                i += 1;
                continue;
            }
            Op::Int(p) => p,
        };
        match p.v {
            0 => s = Style { href: s.href.take(), ..Style::default() },
            1 => s.bold = true,
            2 => s.dim = true,
            3 => s.italic = true,
            4 | 21 => s.underline = true, // 21 = double underline
            7 => s.reverse = true,
            22 => (s.bold, s.dim) = (false, false),
            23 => s.italic = false,
            24 => s.underline = false,
            27 => s.reverse = false,
            30..=37 => s.fg = Some(p.v as i64 - 30),
            39 => s.fg = None,
            40..=47 => s.bg = Some(p.v as i64 - 40),
            49 => s.bg = None,
            90..=97 => s.fg = Some(p.v as i64 - 90 + 8),
            100..=107 => s.bg = Some(p.v as i64 - 100 + 8),
            38 | 48 => {
                let bg = p.v == 48;
                let set = |s: &mut Style, idx: i64| if bg { s.bg = Some(idx) } else { s.fg = Some(idx) };
                match int_at(i + 1).map(|m| m.v) {
                    Some(5) => {
                        if let Some(idx) = int_at(i + 2) {
                            set(&mut s, i64::from(idx.low));
                        }
                        i += 2;
                    }
                    Some(2) => {
                        if let (Some(r), Some(g), Some(b)) = (int_at(i + 2), int_at(i + 3), int_at(i + 4)) {
                            set(&mut s, rgb_to_x256(r.rgb(), g.rgb(), b.rgb()));
                        }
                        i += 4;
                    }
                    _ => break, // unknown colour space: bail out
                }
            }
            // everything else is ignored, notably SGR 9/29 (strikethrough),
            // which has no field in the Style
            _ => {}
        }
        i += 1;
    }
    s
}

// --------------------------------------------------------------------------
// escape sequences
// --------------------------------------------------------------------------

enum Escape {
    Nothing,
    Sgr(usize, usize),
    Osc(usize, usize),
}

/// Consume the escape sequence at `d[i] == ESC`: where it ends, and what it
/// asks for.
fn scan_escape(d: &[char], i: usize) -> (usize, Escape) {
    let n = d.len();
    let mut j = i + 1;
    if j >= n {
        return (n, Escape::Nothing); // a bare ESC at the end
    }
    let c = d[j];
    if c == '[' {
        j += 1;
        let p_start = j;
        while j < n && ('\x30'..='\x3f').contains(&d[j]) {
            j += 1; // parameter bytes
        }
        let p_end = j;
        while j < n && ('\x20'..='\x2f').contains(&d[j]) {
            j += 1; // intermediate bytes
        }
        if j >= n {
            return (n, Escape::Nothing); // truncated at the end
        }
        let fin = d[j];
        j += 1;
        if fin == 'm' {
            // a private-parameter CSI (ESC[?...m, ESC[>...m) is not SGR
            if p_end > p_start && "<=>?".contains(d[p_start]) {
                return (j, Escape::Nothing);
            }
            return (j, Escape::Sgr(p_start, p_end));
        }
        return (j, Escape::Nothing);
    }
    if "]P^_X".contains(c) {
        // OSC / DCS / PM / APC / SOS: a string up to ST or BEL
        let osc = c == ']';
        j += 1;
        let start = j;
        let found = |end| if osc { Escape::Osc(start, end) } else { Escape::Nothing };
        while j < n {
            match d[j] {
                '\x07' => return (j + 1, found(j)),
                '\x1b' if j + 1 < n && d[j + 1] == '\\' => return (j + 2, found(j)),
                // a fresh escape inside the string: the producer never closed
                // it, so end the string here and read the escape
                '\x1b' => return (j, found(j)),
                _ => j += 1,
            }
        }
        return (n, Escape::Nothing); // unterminated: swallow it
    }
    if "()*+-./%".contains(c) {
        return ((j + 2).min(n), Escape::Nothing); // charset designation
    }
    (j + 1, Escape::Nothing) // ESC 7, ESC =, ESC c, ...
}

/// The href an OSC 8 payload sets: None for no change, Some(None) to end
/// the link.
fn osc8_href(payload: &[char]) -> Option<Option<String>> {
    if payload.len() < 2 || payload[0] != '8' || payload[1] != ';' {
        return None;
    }
    let rest = &payload[2..];
    let uri = match rest.iter().position(|&c| c == ';') {
        Some(k) => &rest[k + 1..],
        None => &[][..],
    };
    let a = uri.iter().position(|&c| !is_space(c)).unwrap_or(uri.len());
    let b = uri.iter().rposition(|&c| !is_space(c)).map_or(a, |b| b + 1);
    let uri: String = uri[a..b].iter().collect();
    Some(if uri.is_empty() { None } else { Some(uri) })
}

fn is_control(c: char) -> bool {
    c < '\x20' || ('\x7f'..='\u{9f}').contains(&c)
}

/// Split `data` into logical lines of styled runs, every escape removed.
pub fn parse(data: &str) -> (Vec<Style>, Vec<Line>) {
    let d: Vec<char> = data.chars().collect();
    let mut styles = Styles::new(vec![Style::default()]);
    if d.is_empty() {
        return (styles.list, Vec::new());
    }
    let mut lines: Vec<Line> = Vec::new();
    let mut cur: Line = Vec::new();
    let mut buf = String::new();
    let mut buf_style = Style::default();
    let mut style = Style::default();
    let mut col = 0;
    let mut i = 0;
    let n = d.len();

    let flush = |buf: &mut String, buf_style: &Style, cur: &mut Line, styles: &mut Styles| {
        if !buf.is_empty() {
            cur.push((std::mem::take(buf), styles.id(buf_style)));
        }
    };
    while i < n {
        let ch = d[i];
        if ch == '\x1b' {
            let (next, esc) = scan_escape(&d, i);
            i = next;
            match esc {
                Escape::Sgr(a, b) => {
                    let body: String = d[a..b].iter().collect();
                    style = apply_sgr(&style, &body);
                }
                Escape::Osc(a, b) => {
                    if let Some(href) = osc8_href(&d[a..b]) {
                        style.href = href;
                    }
                }
                Escape::Nothing => {}
            }
            continue;
        }
        if ch == '\n' {
            flush(&mut buf, &buf_style, &mut cur, &mut styles);
            lines.push(std::mem::take(&mut cur));
            col = 0;
            i += 1;
            continue;
        }
        let (text, next): (String, usize) = if ch == '\t' {
            (" ".repeat(TAB_WIDTH - col % TAB_WIDTH), i + 1)
        } else if is_control(ch) {
            // \r, the other C0 controls, DEL and the C1 controls: no width
            i += 1;
            continue;
        } else {
            let mut j = i + 1;
            while j < n && !(d[j] == '\x1b' || d[j] == '\n' || d[j] == '\t' || is_control(d[j])) {
                j += 1;
            }
            (d[i..j].iter().collect(), j)
        };
        if !buf.is_empty() && buf_style != style {
            flush(&mut buf, &buf_style, &mut cur, &mut styles);
        }
        if buf.is_empty() {
            buf_style = style.clone();
        }
        col += text.chars().count();
        buf.push_str(&text);
        i = next;
    }
    flush(&mut buf, &buf_style, &mut cur, &mut styles);
    if !cur.is_empty() || !data.ends_with('\n') {
        lines.push(cur);
    }
    (styles.list, lines)
}

/// Whether `data` carries at least one plausible escape sequence.
pub fn has_ansi(data: &str) -> bool {
    let mut it = data.chars().peekable();
    while let Some(c) = it.next() {
        if c == '\x1b' {
            if let Some(&n) = it.peek() {
                if "[]()*+%\\^_=><".contains(n)
                    || ('@'..='Z').contains(&n)
                    || n.is_ascii_lowercase()
                    || n.is_ascii_digit()
                {
                    return true;
                }
            }
        }
    }
    false
}

// --------------------------------------------------------------------------
// strip_markdown
// --------------------------------------------------------------------------

/// One character with its style, and whether the syntax rules below must not
/// see it (the `*` inside a code span, the `_` inside a URL).
#[derive(Clone, Copy)]
struct Cell {
    ch: char,
    style: usize,
    prot: bool,
}

/// What the syntax rules see: a protected character is NUL.
const MASK: char = '\0';

/// A match: where it starts and ends, and where its groups are.
struct M {
    start: usize,
    end: usize,
    groups: Vec<(usize, usize)>,
}

fn at(t: &[char], i: usize) -> Option<char> {
    t.get(i).copied()
}

fn sp(c: Option<char>) -> bool {
    c.is_some_and(is_space)
}

fn word(c: Option<char>) -> bool {
    c.is_some_and(is_w)
}

fn blank(c: Option<char>) -> bool {
    matches!(c, Some(' ' | '\t'))
}

fn starts(t: &[char], i: usize, s: &str) -> bool {
    (i..).zip(s.chars()).all(|(k, c)| at(t, k) == Some(c))
}

const PUNCT: &str = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~";

/// `\\` and an ASCII punctuation mark.
fn m_escape(t: &[char], p: usize) -> Option<M> {
    (t[p] == '\\' && at(t, p + 1).is_some_and(|c| PUNCT.contains(c)))
        .then(|| M { start: p, end: p + 2, groups: vec![(p + 1, p + 2)] })
}

/// `` (`+)(.+?)\1 ``: a run of backticks, the shortest text, the same run.
fn m_code(t: &[char], p: usize) -> Option<M> {
    let mut r = 0;
    while at(t, p + r) == Some('`') {
        r += 1;
    }
    for k in (1..=r).rev() {
        let body = p + k;
        for e in body + 1..=t.len().saturating_sub(k) {
            if (e..e + k).all(|x| t[x] == '`') {
                return Some(M { start: p, end: e + k, groups: vec![(p, body), (body, e)] });
            }
        }
    }
    None
}

/// `<scheme:...>`, an autolink.
fn m_autolink(t: &[char], p: usize) -> Option<M> {
    if t[p] != '<' {
        return None;
    }
    for scheme in ["https", "http", "ftp", "file", "mailto"] {
        let mut i = p + 1 + scheme.len();
        if !starts(t, p + 1, scheme) || at(t, i) != Some(':') {
            continue;
        }
        i += 1;
        let body = i;
        while at(t, i).is_some_and(|c| c != '>' && !is_space(c)) {
            i += 1;
        }
        if i > body && at(t, i) == Some('>') {
            return Some(M { start: p, end: i + 1, groups: vec![(p + 1, i)] });
        }
    }
    None
}

/// `[text](url "title")` from `p`, at the `[`: groups text and url.
fn link_from(t: &[char], start: usize, p: usize) -> Option<M> {
    if at(t, p) != Some('[') {
        return None;
    }
    let mut i = p + 1;
    while at(t, i).is_some_and(|c| c != ']') {
        i += 1;
    }
    let text = (p + 1, i);
    if at(t, i) != Some(']') || at(t, i + 1) != Some('(') {
        return None;
    }
    i += 2;
    let u = i;
    while at(t, i).is_some_and(|c| c != ')' && !is_space(c)) {
        i += 1;
    }
    let url = (u, i);
    if at(t, i) == Some(')') {
        return Some(M { start, end: i + 1, groups: vec![text, url] });
    }
    // the title: spaces, a quote, anything but quotes or ")", a quote, ")"
    let mut k = i;
    while sp(at(t, k)) {
        k += 1;
    }
    if k == i || !matches!(at(t, k), Some('"' | '\'')) {
        return None;
    }
    k += 1;
    while at(t, k).is_some_and(|c| !"\"')".contains(c)) {
        k += 1;
    }
    if matches!(at(t, k), Some('"' | '\'')) && at(t, k + 1) == Some(')') {
        return Some(M { start, end: k + 2, groups: vec![text, url] });
    }
    None
}

fn m_image(t: &[char], p: usize) -> Option<M> {
    if t[p] != '!' {
        return None;
    }
    link_from(t, p, p + 1)
}

fn m_link(t: &[char], p: usize) -> Option<M> {
    if p > 0 && t[p - 1] == '!' {
        return None;
    }
    link_from(t, p, p)
}

/// `[text][label]`, a reference link.
fn m_reflink(t: &[char], p: usize) -> Option<M> {
    if t[p] != '[' {
        return None;
    }
    let mut i = p + 1;
    while at(t, i).is_some_and(|c| c != ']') {
        i += 1;
    }
    if i == p + 1 || at(t, i) != Some(']') || at(t, i + 1) != Some('[') {
        return None;
    }
    let text = (p + 1, i);
    i += 2;
    while at(t, i).is_some_and(|c| c != ']') {
        i += 1;
    }
    (at(t, i) == Some(']')).then(|| M { start: p, end: i + 1, groups: vec![text] })
}

/// A bare URL: `scheme:` and no space, or `mailto:` or `www.` at a word
/// break.
fn m_url(t: &[char], p: usize) -> Option<M> {
    let rest = |i: usize| {
        let mut e = i;
        while at(t, e).is_some_and(|c| !is_space(c)) {
            e += 1;
        }
        (e > i).then(|| M { start: p, end: e, groups: vec![] })
    };
    for scheme in ["https:", "http:", "ftp:", "file:", "data:"] {
        if starts(t, p, scheme) {
            if let Some(m) = rest(p + scheme.len()) {
                return Some(m);
            }
        }
    }
    let at_break = p == 0 || !is_w(t[p - 1]);
    if at_break && starts(t, p, "mailto:") {
        if let Some(m) = rest(p + 7) {
            return Some(m);
        }
    }
    if at_break && starts(t, p, "www.") {
        return rest(p + 4);
    }
    None
}

/// `^([ \t]{0,3})#{1,6}[ \t]+`
fn m_heading(t: &[char], p: usize) -> Option<M> {
    if p != 0 {
        return None;
    }
    let mut i = 0;
    while i < 3 && blank(at(t, i)) {
        i += 1;
    }
    let lead = i;
    while at(t, i) == Some('#') {
        i += 1;
    }
    if !(1..=6).contains(&(i - lead)) || !blank(at(t, i)) {
        return None;
    }
    while blank(at(t, i)) {
        i += 1;
    }
    Some(M { start: 0, end: i, groups: vec![(0, lead)] })
}

/// `([ \t]+#+[ \t]*)$`
fn m_heading_tail(t: &[char], p: usize) -> Option<M> {
    let mut i = p;
    while blank(at(t, i)) {
        i += 1;
    }
    if i == p || at(t, i) != Some('#') {
        return None;
    }
    while at(t, i) == Some('#') {
        i += 1;
    }
    while blank(at(t, i)) {
        i += 1;
    }
    (i == t.len()).then(|| M { start: p, end: i, groups: vec![(p, i)] })
}

/// `^([ \t]*)(?:>[ \t]?)+`
fn m_quote(t: &[char], p: usize) -> Option<M> {
    if p != 0 {
        return None;
    }
    let mut i = 0;
    while blank(at(t, i)) {
        i += 1;
    }
    let lead = i;
    while at(t, i) == Some('>') {
        i += 1;
        if blank(at(t, i)) {
            i += 1;
        }
    }
    (i > lead).then(|| M { start: 0, end: i, groups: vec![(0, lead)] })
}

/// `^([ \t]*)([-*+])([ \t]+)`
fn m_bullet(t: &[char], p: usize) -> Option<M> {
    if p != 0 {
        return None;
    }
    let mut i = 0;
    while blank(at(t, i)) {
        i += 1;
    }
    let lead = i;
    if !matches!(at(t, i), Some('-' | '*' | '+')) || !blank(at(t, i + 1)) {
        return None;
    }
    let mut e = i + 1;
    while blank(at(t, e)) {
        e += 1;
    }
    Some(M { start: 0, end: e, groups: vec![(0, lead), (i, i + 1)] })
}

/// `open(?!\s)(.+?)(?<!\s)close`, the inner text at least one character,
/// with `after` checked past the closing marker.
fn delimited(
    t: &[char],
    p: usize,
    marker: &str,
    first: impl Fn(char) -> bool,
    inner: impl Fn(char) -> bool,
    strict: bool,
    after: impl Fn(Option<char>) -> bool,
) -> Option<M> {
    let k = marker.chars().count();
    if !starts(t, p, marker) {
        return None;
    }
    let body = p + k;
    match at(t, body) {
        Some(c) if !is_space(c) && first(c) => {}
        _ => return None,
    }
    let mut e = body + 1;
    while e + k <= t.len() {
        if !inner(t[e - 1]) {
            return None;
        }
        if starts(t, e, marker) && !(strict && is_space(t[e - 1])) && after(at(t, e + k)) {
            return Some(M { start: p, end: e + k, groups: vec![(body, e)] });
        }
        e += 1;
    }
    None
}

fn any(_: char) -> bool {
    true
}

/// `~~(.+?)~~`
fn m_strike(t: &[char], p: usize) -> Option<M> {
    // .+? may start with a space and end with one
    if !starts(t, p, "~~") || p + 2 >= t.len() {
        return None;
    }
    (p + 3..=t.len().saturating_sub(2))
        .find(|&e| starts(t, e, "~~"))
        .map(|e| M { start: p, end: e + 2, groups: vec![(p + 2, e)] })
}

/// `\*\*(?!\s)(.+?)(?<!\s)\*\*`
fn m_bold_star(t: &[char], p: usize) -> Option<M> {
    delimited(t, p, "**", any, any, true, |_| true)
}

/// `(?<![\w\\])__(?!\s)(.+?)(?<!\s)__(?!\w)`
fn m_bold_und(t: &[char], p: usize) -> Option<M> {
    if p > 0 && (is_w(t[p - 1]) || t[p - 1] == '\\') {
        return None;
    }
    delimited(t, p, "__", any, any, true, |c| !word(c))
}

/// `(?<![\*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?!\*)`
fn m_ital_star(t: &[char], p: usize) -> Option<M> {
    if p > 0 && (is_w(t[p - 1]) || t[p - 1] == '*') {
        return None;
    }
    let not_star = |c: char| c != '*';
    delimited(t, p, "*", not_star, not_star, true, |c| c != Some('*'))
}

/// `(?<![\w\\])_(?!\s)([^_]+?)(?<!\s)_(?!\w)`
fn m_ital_und(t: &[char], p: usize) -> Option<M> {
    if p > 0 && (is_w(t[p - 1]) || t[p - 1] == '\\') {
        return None;
    }
    let not_und = |c: char| c != '_';
    delimited(t, p, "_", not_und, not_und, true, |c| !word(c))
}

/// A thematic break: three or more of one of `*`, `-` or `_`, spaces allowed
/// between, or three or more `=`.
fn is_rule_line(t: &[char]) -> bool {
    let mut i = 0;
    while blank(at(t, i)) {
        i += 1;
    }
    match at(t, i) {
        Some(c @ ('*' | '-' | '_')) => {
            let mut count = 0;
            while let Some(x) = at(t, i) {
                if x == c {
                    count += 1;
                } else if x != ' ' && x != '\t' {
                    return false;
                }
                i += 1;
            }
            count >= 3
        }
        Some('=') => {
            let s = i;
            while at(t, i) == Some('=') {
                i += 1;
            }
            let run = i - s;
            while blank(at(t, i)) {
                i += 1;
            }
            run >= 3 && i == t.len()
        }
        _ => false,
    }
}

/// A table's delimiter row: `| --- | :--: |` and its variants.
fn is_table_sep(t: &[char]) -> bool {
    let skip = |mut i: usize| {
        while blank(at(t, i)) {
            i += 1;
        }
        i
    };
    // a cell: :? -{2,} :? [ \t]*
    let cell = |mut i: usize| -> Option<usize> {
        if at(t, i) == Some(':') {
            i += 1;
        }
        let s = i;
        while at(t, i) == Some('-') {
            i += 1;
        }
        if i - s < 2 {
            return None;
        }
        if at(t, i) == Some(':') {
            i += 1;
        }
        Some(skip(i))
    };
    let mut i = skip(0);
    if at(t, i) == Some('|') {
        i += 1;
    }
    let Some(mut i) = cell(skip(i)) else { return false };
    while at(t, i) == Some('|') {
        match cell(skip(i + 1)) {
            Some(next) => i = next,
            None => break,
        }
    }
    if at(t, i) == Some('|') {
        i += 1;
    }
    skip(i) == t.len()
}

fn text_of(cells: &[Cell]) -> Vec<char> {
    cells.iter().map(|c| if c.prot { MASK } else { c.ch }).collect()
}

/// Every match of `find` from left to right, each one replaced with what
/// `build` makes of it.
fn rewrite(
    cells: Vec<Cell>,
    find: impl Fn(&[char], usize) -> Option<M>,
    mut build: impl FnMut(&M, &[Cell], &[char]) -> Vec<Cell>,
) -> Vec<Cell> {
    let t = text_of(&cells);
    let mut out = Vec::with_capacity(cells.len());
    let (mut pos, mut p) = (0, 0);
    while p < t.len() {
        match find(&t, p) {
            Some(m) if m.end > m.start => {
                out.extend_from_slice(&cells[pos..m.start]);
                out.extend(build(&m, &cells, &t));
                pos = m.end;
                p = m.end;
            }
            _ => p += 1,
        }
    }
    out.extend_from_slice(&cells[pos..]);
    out
}

fn group(m: &M, cells: &[Cell], g: usize) -> Vec<Cell> {
    m.groups.get(g).map_or_else(Vec::new, |&(s, e)| cells[s..e].to_vec())
}

fn protect(mut cells: Vec<Cell>) -> Vec<Cell> {
    for c in &mut cells {
        c.prot = true;
    }
    cells
}

fn strip_line(line: &[(String, usize)], styles: &mut Styles) -> Line {
    let mut cells: Vec<Cell> = line
        .iter()
        .flat_map(|(text, style)| text.chars().map(move |ch| Cell { ch, style: *style, prot: false }))
        .collect();
    if cells.is_empty() {
        return Vec::new();
    }
    let raw: Vec<char> = cells.iter().map(|c| c.ch).collect();
    if is_rule_line(&raw) || is_table_sep(&raw) {
        let end = raw.iter().rposition(|&c| !is_space(c)).map_or(0, |e| e + 1);
        let width = end.max(3);
        let rule = vec![Cell { ch: '\u{2500}', style: cells[0].style, prot: true }; width];
        return implode(&rule);
    }
    cells = rewrite(cells, m_escape, |m, c, _| protect(group(m, c, 0)));
    cells = rewrite(cells, m_code, |m, c, _| protect(group(m, c, 1)));
    cells = rewrite(cells, m_autolink, |m, c, _| protect(group(m, c, 0)));
    cells = rewrite(cells, m_image, |m, c, _| group(m, c, 0));
    cells = rewrite(cells, m_link, |m, c, t| {
        let (us, ue) = m.groups[1];
        let mut url: Vec<char> = t[us..ue].to_vec();
        // .strip(): the url holds no space, so this only trims what \s and
        // str.isspace() disagree on
        while url.first().is_some_and(|&c| is_space(c)) {
            url.remove(0);
        }
        while url.last().is_some_and(|&c| is_space(c)) {
            url.pop();
        }
        if url.len() >= 2 && url[0] == '<' && url[url.len() - 1] == '>' {
            url = url[1..url.len() - 1].to_vec();
        }
        let url: String = url.into_iter().collect();
        let mut out = group(m, c, 0);
        if !url.is_empty() {
            for cell in &mut out {
                cell.style = styles.with(cell.style, |s| s.href = Some(url.clone()));
            }
        }
        out
    });
    cells = rewrite(cells, m_reflink, |m, c, _| group(m, c, 0));
    // bare URLs must not be chewed up by the emphasis rules below
    cells = rewrite(cells, m_url, |m, c, _| protect(c[m.start..m.end].to_vec()));

    if m_heading(&text_of(&cells), 0).is_some() {
        cells = rewrite(cells, m_heading, |_, _, _| Vec::new());
        cells = rewrite(cells, m_heading_tail, |_, _, _| Vec::new());
    } else {
        cells = rewrite(cells, m_quote, |m, c, _| group(m, c, 0));
        cells = rewrite(cells, m_bullet, |m, c, _| {
            let mark = m.groups[1].0;
            let style = c.get(mark).map_or(0, |cell| cell.style);
            let mut out = group(m, c, 0);
            out.push(Cell { ch: '\u{2022}', style, prot: true });
            out.push(Cell { ch: ' ', style, prot: true });
            out
        });
    }
    cells = rewrite(cells, m_strike, |m, c, _| group(m, c, 0));
    for (find, bold) in [
        (m_bold_star as fn(&[char], usize) -> Option<M>, true),
        (m_bold_und, true),
        (m_ital_star, false),
        (m_ital_und, false),
    ] {
        cells = rewrite(cells, find, |m, c, _| {
            let mut out = group(m, c, 0);
            for cell in &mut out {
                cell.style = styles.with(cell.style, |s| if bold { s.bold = true } else { s.italic = true });
            }
            out
        });
    }
    implode(&cells)
}

fn implode(cells: &[Cell]) -> Line {
    let mut out: Line = Vec::new();
    for c in cells {
        match out.last_mut() {
            Some((text, style)) if *style == c.style => text.push(c.ch),
            _ => out.push((c.ch.to_string(), c.style)),
        }
    }
    out
}

/// `` ^[ \t]{0,3}(?:`{3,}|~{3,}) ``
fn is_fence(t: &[char]) -> bool {
    let mut i = 0;
    while i < 3 && blank(at(t, i)) {
        i += 1;
    }
    match at(t, i) {
        Some(c @ ('`' | '~')) => at(t, i + 1) == Some(c) && at(t, i + 2) == Some(c),
        _ => false,
    }
}

/// Lightly de-syntax raw Markdown, keeping the run structure: see
/// `readaloud.ansi.strip_markdown`.  `styles` seeds the table the line's style
/// ids index; styles the rewrites make are added to it.
pub fn strip_markdown(styles: Vec<Style>, lines: Vec<Line>) -> (Vec<Style>, Vec<Line>) {
    let mut styles = Styles::new(styles);
    let mut out = Vec::with_capacity(lines.len());
    let mut in_fence = false;
    for line in lines {
        if line.is_empty() {
            out.push(Vec::new());
            continue;
        }
        let raw: Vec<char> = line.iter().flat_map(|(text, _)| text.chars()).collect();
        if is_fence(&raw) {
            in_fence = !in_fence;
            out.push(Vec::new()); // keep the line, drop the delimiter
            continue;
        }
        if in_fence {
            out.push(line);
            continue;
        }
        out.push(strip_line(&line, &mut styles));
    }
    (styles.list, out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plain(lines: &[Line]) -> Vec<String> {
        lines.iter().map(|l| l.iter().map(|(t, _)| t.as_str()).collect()).collect()
    }

    #[test]
    fn escapes_go_and_styles_stay() {
        let (styles, lines) = parse("\x1b[1mbold\x1b[0m\tx\n\x1b]8;;https://a\x07link\x1b]8;;\x07");
        assert_eq!(plain(&lines), vec!["bold    x", "link"]);
        assert!(styles[lines[0][0].1].bold);
        assert_eq!(styles[lines[1][0].1].href.as_deref(), Some("https://a"));
    }

    #[test]
    fn truecolour_quantises() {
        assert_eq!(rgb_to_x256(255, 0, 0), 196);
        assert_eq!(rgb_to_x256(128, 128, 128), 244);
    }

    #[test]
    fn markdown_is_stripped() {
        let line = |s: &str| vec![(s.to_string(), 0)];
        let (_, out) = strip_markdown(
            vec![Style::default()],
            vec![line("# Title #"), line("- **bold** and _it_ `a*b`"), line("[x](u) ---")],
        );
        assert_eq!(plain(&out), vec!["Title", "\u{2022} bold and it a*b", "x ---"]);
    }
}
