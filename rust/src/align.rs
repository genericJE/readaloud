//! Word timings: the alignment half of `readaloud.speech`.
//!
//! misaki's tokens carry timestamps; the reader needs one span of seconds per
//! word slot.  Everything here is pure arithmetic over char offsets and
//! seconds, in the same order of operations as the Python it replaced, so the
//! floats come out bit for bit the same.  Python's `max(a, b)` and `min(a, b)`
//! keep `a` unless `b` is strictly greater (smaller), which decides what a NaN
//! or a signed zero turns into; `py_max` and `py_min` do the same.

use crate::text::is_space;

/// Source chars misaki may drop inside one token.
const MAX_SKIP: usize = 12;
/// How far ahead of the cursor a token may start.
const SEARCH_WINDOW: usize = 400;

/// Trailing characters that document.py would not include in `Word.text` but
/// that sit inside a chunk's raw text right after the word.
const TRAILING_PUNCT: &str = ".,;:!?)]}>\"'’”…»‘“«*_`~";

/// One misaki token: its text and its timestamps, either of which may be
/// missing.
pub struct Token {
    pub text: Vec<char>,
    pub start: Option<f64>,
    pub end: Option<f64>,
}

/// A word slot's `(slot, start, end)` in seconds.
pub type Timing = (usize, f64, f64);

/// Python's `max(a, b)`.
fn py_max(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's `min(a, b)`.
fn py_min(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// `text.find(needle, from)`, or None.
fn find(text: &[char], needle: &[char], from: usize) -> Option<usize> {
    if from > text.len() || needle.len() > text.len() - from {
        return None;
    }
    (from..=text.len() - needle.len()).find(|&p| text[p..p + needle.len()] == *needle)
}

/// Whether `text[a:b]` is whitespace only (or empty).
fn blank(text: &[char], a: usize, b: usize) -> bool {
    text[a..b].iter().all(|&c| is_space(c))
}

/// `s.strip()`.
fn strip(s: &[char]) -> &[char] {
    let a = s.iter().position(|&c| !is_space(c)).unwrap_or(s.len());
    let b = s.iter().rposition(|&c| !is_space(c)).map_or(a, |b| b + 1);
    &s[a..b]
}

/// Find `needle`'s char span in `text` at or after `cursor`, tolerating the
/// ways misaki mangles token text (absorbed leading whitespace, dropped
/// quotes).
pub fn locate(text: &[char], needle: &[char], cursor: usize) -> Option<(usize, usize)> {
    if needle.is_empty() {
        return None;
    }
    let n = text.len();

    // 1. exact match separated from the cursor by whitespace only (the normal case).
    let pos = find(text, needle, cursor);
    if let Some(p) = pos.filter(|&p| blank(text, cursor, p)) {
        return Some((p, p + needle.len()));
    }

    // 2. misaki sometimes glues leading whitespace or newlines onto .text; retry trimmed.
    let mut trimmed = strip(needle);
    if !trimmed.is_empty() && trimmed != needle {
        if let Some(p) = find(text, trimmed, cursor).filter(|&p| blank(text, cursor, p)) {
            return Some((p, p + trimmed.len()));
        }
    } else {
        trimmed = needle;
    }

    // 3. subsequence match allowing dropped source characters (e.g. print('hello).
    let limit = n.min(cursor.saturating_add(SEARCH_WINDOW));
    let first = trimmed[0];
    for start in cursor..limit {
        if text[start] != first {
            continue;
        }
        let (mut i, mut j, mut skipped) = (start, 0, 0);
        while i < n && j < trimmed.len() {
            i += 1;
            if text[i - 1] == trimmed[j] {
                j += 1;
            } else {
                skipped += 1;
                if skipped > MAX_SKIP {
                    break;
                }
            }
        }
        if j == trimmed.len() {
            return Some((start, i));
        }
    }

    // 4. last resort: a plain forward exact match, even across a non-whitespace gap.
    if let Some(p) = pos {
        return Some((p, p + needle.len()));
    }
    find(text, trimmed, cursor).map(|p| (p, p + trimmed.len()))
}

/// Map misaki tokens onto word slots.
///
/// `spans` are the char spans of the word slots in `text`, in order.
/// `results` hold one `(tokens, audio seconds)` pair per pipeline Result;
/// each Result's timestamps are offset by the audio of every Result before
/// it.  Returns `(slot, start, end)` for every slot, in slot order, with
/// non-decreasing, non-negative starts.
pub fn align_words(text: &[char], spans: &[(i64, i64)], results: &[(Vec<Token>, f64)]) -> Vec<Timing> {
    let nwords = spans.len();
    if nwords == 0 {
        return Vec::new();
    }
    let mut lo: Vec<Option<f64>> = vec![None; nwords];
    let mut hi: Vec<Option<f64>> = vec![None; nwords];

    let mut cursor = 0;
    let mut time_offset = 0.0;
    let mut total = 0.0;
    let mut wi = 0; // first word slot that may still overlap (spans is sorted)

    for (tokens, duration) in results {
        for tok in tokens {
            let Some((s_char, e_char)) = locate(text, &tok.text, cursor) else {
                continue; // unmatchable token: skip, keep the cursor
            };
            cursor = e_char;
            let (s_char, e_char) = (s_char as i64, e_char as i64);

            let (ts, te) = match (tok.start, tok.end) {
                (None, None) => continue, // e.g. a bare '$': consumes text, no timing
                (Some(ts), None) => (ts, ts),
                (None, Some(te)) => (te, te),
                (Some(ts), Some(te)) => (ts, te),
            };
            let mut ts = py_max(0.0, ts) + time_offset;
            let mut te = py_max(0.0, te) + time_offset;
            if te < ts {
                (ts, te) = (te, ts);
            }

            // advance the word window, then collect every word this token covers
            while wi < nwords && spans[wi].1 <= s_char {
                wi += 1;
            }
            let mut covered: Vec<(usize, i64, i64)> = Vec::new();
            let mut k = wi;
            while k < nwords && spans[k].0 < e_char {
                let (a, b) = spans[k];
                if b.min(e_char) - a.max(s_char) > 0 {
                    covered.push((k, a.max(s_char), b.min(e_char)));
                }
                k += 1;
            }
            if covered.is_empty() {
                continue;
            }

            let mut put = |idx: usize, a_t: f64, b_t: f64| {
                lo[idx] = Some(lo[idx].map_or(a_t, |l| py_min(l, a_t)));
                hi[idx] = Some(hi[idx].map_or(b_t, |h| py_max(h, b_t)));
            };
            if covered.len() == 1 {
                put(covered[0].0, ts, te);
            } else {
                // one token spoke several words ("  spaces\tand"): split its span
                // proportionally to how much of the token each word occupies.
                let base = covered[0].1;
                let width = (covered[covered.len() - 1].2 - base).max(1);
                for &(idx, a, b) in &covered {
                    let f0 = (a - base) as f64 / width as f64;
                    let f1 = (b - base) as f64 / width as f64;
                    put(idx, ts + (te - ts) * f0, ts + (te - ts) * f1);
                }
            }
        }
        time_offset += py_max(0.0, *duration);
        total = time_offset;
    }

    if total <= 0.0 {
        total = hi.iter().flatten().copied().reduce(py_max).unwrap_or(0.0);
    }

    // fill untimed slots by interpolating between the timed anchors around them
    let mut i = 0;
    let mut prev_end = 0.0;
    while i < nwords {
        if lo[i].is_some() {
            prev_end = hi[i].unwrap_or(0.0);
            i += 1;
            continue;
        }
        let mut j = i;
        while j < nwords && lo[j].is_none() {
            j += 1;
        }
        let nxt = if j < nwords { lo[j].unwrap_or(0.0) } else { total };
        let (left, right) = (prev_end, py_max(prev_end, nxt));
        let widths: Vec<i64> = spans[i..j].iter().map(|&(a, b)| (b - a).max(1)).collect();
        let tot_w: i64 = widths.iter().sum();
        let mut t = left;
        for (k, &w) in (i..j).zip(&widths) {
            let step = (right - left) * (w as f64 / tot_w as f64);
            lo[k] = Some(t);
            hi[k] = Some(t + step);
            t += step;
        }
        prev_end = right;
        i = j;
    }

    let mut out = Vec::with_capacity(nwords);
    let mut run = 0.0;
    for k in 0..nwords {
        let a = py_max(run, lo[k].unwrap_or(0.0));
        let b = py_max(a, hi[k].unwrap_or(0.0));
        out.push((k, a, b));
        run = a;
    }
    out
}

/// One `(start, end)` char span per word slot inside `text`.
///
/// `offsets` hold each word's start, None where it was not a number, already
/// clamped into `0..=len(text)`.  The end is the given word text where it
/// matches there, else the next whitespace or the next word's start, with
/// trailing punctuation trimmed.  The spans are sorted and do not overlap.
pub fn word_spans(text: &[char], offsets: &[Option<usize>], word_texts: Option<&[Vec<char>]>) -> Vec<(usize, usize)> {
    let n = text.len();
    let cnt = offsets.len();
    let mut spans = Vec::with_capacity(cnt);
    let mut floor = 0;
    for i in 0..cnt {
        let start = floor.max(n.min(offsets[i].unwrap_or(floor)));
        let limit = if i + 1 < cnt { start.max(n.min(offsets[i + 1].unwrap_or(n))) } else { n };

        let given = word_texts
            .and_then(|w| w.get(i))
            .filter(|wt| !wt.is_empty() && text[start..].starts_with(wt));
        let end = given.map(|wt| start + wt.len()).unwrap_or_else(|| {
            let mut j = start;
            while j < limit && !is_space(text[j]) {
                j += 1;
            }
            while j > start + 1 && TRAILING_PUNCT.contains(text[j - 1]) {
                j -= 1;
            }
            j
        });

        let mut end = start.max(n.min(end));
        if end == start && start < n {
            end = start + 1;
        }
        spans.push((start, end));
        floor = end;
    }
    spans
}

/// Degraded fallback: `duration` spread over `nslots` slots by the width of
/// each span (1 for a slot past the spans).
pub fn even_timings(spans: &[(i64, i64)], duration: f64, nslots: i64) -> Vec<Timing> {
    if nslots <= 0 {
        return Vec::new();
    }
    let n = nslots as usize;
    let dur = py_max(0.0, duration);
    let widths: Vec<i64> = (0..n).map(|i| spans.get(i).map_or(1, |&(a, b)| b - a).max(1)).collect();
    let total_w = widths.iter().sum::<i64>() as f64;
    let mut out = Vec::with_capacity(n);
    let mut t = 0.0;
    for (i, &w) in widths.iter().enumerate() {
        let step = dur * (w as f64 / total_w);
        out.push((i, t, py_min(dur, t + step)));
        t = py_min(dur, t + step);
    }
    out
}

/// `timings` clamped into `[0, duration]` and made monotonic, or why they are
/// structurally wrong (the wrong number of slots, a slot out of order, a NaN).
pub fn sanitize(timings: &[(i64, f64, f64)], nslots: i64, duration: f64) -> Result<Vec<Timing>, String> {
    if timings.len() as i64 != nslots {
        return Err(format!("alignment produced {} timings for {nslots} slots", timings.len()));
    }
    let hi = py_max(0.0, duration);
    let mut run = 0.0;
    let mut out = Vec::with_capacity(timings.len());
    for (i, &(slot, s, e)) in timings.iter().enumerate() {
        if slot != i as i64 {
            return Err(format!("alignment slot {slot} out of order at {i}"));
        }
        if s.is_nan() || e.is_nan() {
            return Err("alignment produced NaN timings".to_owned());
        }
        let (s, e) = if hi > 0.0 {
            (py_min(py_max(0.0, s), hi), py_min(py_max(0.0, e), hi))
        } else {
            (py_max(0.0, s), py_max(0.0, e))
        };
        let s = py_max(run, s);
        let e = py_max(s, e);
        out.push((i, s, e));
        run = s;
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chars(s: &str) -> Vec<char> {
        s.chars().collect()
    }

    fn at(text: &str, needle: &str, cursor: usize) -> Option<(usize, usize)> {
        locate(&chars(text), &chars(needle), cursor)
    }

    #[test]
    fn python_min_and_max_keep_the_first_on_a_tie_or_a_nan() {
        assert!(py_max(0.0, -0.0).is_sign_positive());
        assert!(py_max(-0.0, 0.0).is_sign_negative());
        assert_eq!(py_max(0.0, f64::NAN), 0.0);
        assert!(py_max(f64::NAN, 1.0).is_nan());
        assert_eq!(py_min(1.0, f64::NAN), 1.0);
    }

    #[test]
    fn locate_finds_tokens_the_way_misaki_mangles_them() {
        assert_eq!(at("say  hello", "hello", 3), Some((5, 10)));
        assert_eq!(at("a word", "\nword", 1), Some((2, 6)));
        assert_eq!(at("print('hello')", "print(hello", 0), Some((0, 12)));
        assert_eq!(at("x y", "", 0), None);
        assert_eq!(at("abc", "zz", 0), None);
        assert_eq!(at("abc", "c", 9), None);
    }

    #[test]
    fn strip_and_blank_use_python_whitespace() {
        assert_eq!(strip(&chars("\u{a0} a b\u{2003}")), &chars("a b")[..]);
        assert_eq!(strip(&chars("  ")), &[] as &[char]);
        assert!(blank(&chars("a \u{3000}\tb"), 1, 4));
    }

    #[test]
    fn word_spans_trim_punctuation_but_keep_one_char() {
        let text = chars("Hello, world!! ...");
        let offsets = [Some(0), Some(7), Some(15)];
        assert_eq!(word_spans(&text, &offsets, None), vec![(0, 5), (7, 12), (15, 16)]);
    }

    #[test]
    fn sanitize_refuses_the_wrong_shape() {
        assert!(sanitize(&[(0, 0.0, 1.0)], 2, 1.0).is_err());
        assert!(sanitize(&[(1, 0.0, 1.0)], 1, 1.0).is_err());
        assert!(sanitize(&[(0, f64::NAN, 1.0)], 1, 1.0).is_err());
        assert_eq!(sanitize(&[(0, -1.0, 9.0)], 1, 2.0), Ok(vec![(0, 0.0, 2.0)]));
    }
}
