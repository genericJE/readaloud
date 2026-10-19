//! The `[pronunciations]` of the config file: finding them in a chunk and
//! respelling it.  The rules are documented in `readaloud/pronounce.py`;
//! this is their implementation.
//!
//! An entry is either literal text (`kubectl = cube control`) or a pattern
//! (`/:(\d+)-(\d+)/ = lines $1 to $2`).  Literal keys are indexed by their
//! first run of letters and digits, or their first symbol, so the text costs
//! a hash probe per word rather than a pass per key.  Each pattern keeps a
//! cursor on its next match, so it scans the text about once.
//!
//! Every position and span counts characters, never bytes.

use std::collections::{BTreeMap, HashMap};

use regex::Regex;
use unicode_normalization::UnicodeNormalization;

use crate::text::{collapse, is_hump, is_space, is_upper, is_word_char, lower, same_folded};

/// The gutter mdcat draws down a blockquote, which may follow a line break
/// inside a phrase.
const GUTTER: char = '│';

/// A match: `(start, end, say)` in characters of the text.
pub type Found = (usize, usize, String);

struct Literal {
    key: String,
    say: String,
    rank: usize,
    /// A capital in the key: match that case only.
    exact: bool,
    /// The key as written (NFC) and with its accents decomposed (NFD), each
    /// split at its spaces.
    forms: Vec<Vec<Vec<char>>>,
}

struct Pattern {
    regex: Regex,
    template: String,
    rank: usize,
}

pub struct Lexicon {
    literals: Vec<Literal>,
    patterns: Vec<Pattern>,
    /// first run of letters and digits -> literals: as written for exact
    /// keys, lowercased for the rest
    runs: HashMap<String, Vec<usize>>,
    folded_runs: HashMap<String, Vec<usize>>,
    lengths: Vec<usize>,
    /// first character of a key that starts with a symbol -> literals
    leads: HashMap<char, Vec<usize>>,
    folded_leads: HashMap<String, Vec<usize>>,
    /// a key without its variation selectors -> what a symbol by that name
    /// says
    symbols: HashMap<String, String>,
}

/// Why a pattern cannot be used, or None.
pub fn check_pattern(pattern: &str) -> Option<String> {
    if let Err(err) = Regex::new(pattern) {
        let text = err.to_string();
        let last = text
            .lines()
            .rev()
            .find(|l| !l.trim().is_empty())
            .unwrap_or(&text)
            .trim();
        return Some(last.trim_start_matches("error: ").to_string());
    }
    match regex_syntax::Parser::new().parse(pattern) {
        Ok(hir) if hir.properties().minimum_len() == Some(0) => {
            Some("it can match empty text".to_string())
        }
        Ok(_) => None,
        Err(err) => Some(err.to_string()),
    }
}

/// The pattern inside a `/.../` key.
pub fn pattern_of(key: &str) -> &str {
    &key[1..key.len() - 1]
}

fn without_variation_selectors(s: &str) -> String {
    s.chars().filter(|&c| c != '\u{fe0e}' && c != '\u{fe0f}').collect()
}

fn push_once(bucket: &mut Vec<usize>, i: usize) {
    if bucket.last() != Some(&i) {
        bucket.push(i);
    }
}

impl Lexicon {
    /// `pairs` are `(written, spoken, is_pattern)` in file order.  A pair with
    /// an empty side, or a pattern that does not compile, is skipped.
    pub fn new(pairs: &[(String, String, bool)]) -> Self {
        let mut lex = Lexicon {
            literals: Vec::new(),
            patterns: Vec::new(),
            runs: HashMap::new(),
            folded_runs: HashMap::new(),
            lengths: Vec::new(),
            leads: HashMap::new(),
            folded_leads: HashMap::new(),
            symbols: HashMap::new(),
        };
        for (rank, (written, spoken, is_pattern)) in pairs.iter().enumerate() {
            let say = collapse(spoken);
            if say.is_empty() {
                continue;
            }
            if *is_pattern {
                let pattern = pattern_of(written);
                if check_pattern(pattern).is_some() {
                    continue;
                }
                if let Ok(regex) = Regex::new(pattern) {
                    lex.patterns.push(Pattern { regex, template: say, rank });
                }
                continue;
            }
            let key = collapse(&written.nfc().collect::<String>());
            if key.is_empty() {
                continue;
            }
            let i = lex.literals.len();
            let exact = key.chars().any(is_upper);
            lex.symbols.insert(without_variation_selectors(&key), say.clone());
            let nfd: String = key.nfd().collect();
            let mut forms = vec![key.clone()];
            if nfd != key {
                forms.push(nfd);
            }
            for form in &forms {
                let run: String = form.chars().take_while(|&c| is_word_char(c)).collect();
                if !run.is_empty() {
                    let n = run.chars().count();
                    if !lex.lengths.contains(&n) {
                        lex.lengths.push(n);
                    }
                    if exact {
                        push_once(lex.runs.entry(run).or_default(), i);
                    } else {
                        push_once(lex.folded_runs.entry(lower(run.chars())).or_default(), i);
                    }
                } else {
                    let lead = form.chars().next().unwrap();
                    if exact {
                        push_once(lex.leads.entry(lead).or_default(), i);
                    } else {
                        push_once(lex.folded_leads.entry(lower([lead])).or_default(), i);
                    }
                }
            }
            let forms = forms
                .iter()
                .map(|f| f.split(' ').map(|w| w.chars().collect()).collect())
                .collect();
            lex.literals.push(Literal { key, say, rank, exact, forms });
        }
        lex.lengths.sort_unstable();
        lex
    }

    pub fn len(&self) -> usize {
        self.literals.len() + self.patterns.len()
    }

    /// Every `(start, end, say)` to respell in `text`: sorted, never empty and
    /// never overlapping.
    ///
    /// `symbols` lists `(start, end, symbol)` for each slot that says a symbol
    /// by its name.  No match may reach into a name; a literal whose key is
    /// the symbol replaces the whole name instead.
    pub fn find(&self, text: &str, symbols: &[(usize, usize, String)]) -> Vec<Found> {
        if self.len() == 0 {
            return Vec::new();
        }
        let chars: Vec<char> = text.chars().collect();
        let mut names: Vec<&(usize, usize, String)> =
            symbols.iter().filter(|(s, e, _)| s < e).collect();
        names.sort();
        let name_ends: Vec<usize> = names.iter().map(|n| n.1).collect();

        let candidates = self.candidates(&chars);
        let mut cursors = Cursors::new(&self.patterns, text);
        let mut found: Vec<Found> = Vec::new();
        let mut pos = 0;
        let mut ci = 0;
        loop {
            while ci < candidates.len() && candidates[ci].0 < pos {
                ci += 1;
            }
            let literal_at = candidates.get(ci).map(|c| c.0);
            let pattern_at = cursors.next_start(pos);
            let p = match (literal_at, pattern_at) {
                (Some(a), Some(b)) => a.min(b),
                (Some(a), None) => a,
                (None, Some(b)) => b,
                (None, None) => break,
            };
            // the first name that ends after p: a match may not reach into it
            let k = name_ends.partition_point(|&e| e <= p);
            let name_at = names.get(k).map_or(chars.len(), |n| n.0);
            // (end, exact, rank), and what to say
            let mut best: Option<((usize, bool, usize), String, bool)> = None;
            if literal_at == Some(p) {
                for &i in &candidates[ci].1 {
                    let lit = &self.literals[i];
                    let Some(end) = match_literal(lit, &chars, p) else { continue };
                    if end <= p || name_at < end {
                        continue;
                    }
                    let rank = (end, lit.exact, lit.rank);
                    if best.as_ref().is_none_or(|b| rank > b.0) {
                        best = Some((rank, lit.say.clone(), lit.say == lit.key));
                    }
                }
                ci += 1;
            }
            for (k, m) in cursors.starting_at(p) {
                if name_at < m.end {
                    continue;
                }
                let rank = (m.end, true, self.patterns[k].rank);
                if best.as_ref().is_none_or(|b| rank > b.0) {
                    best = Some((rank, m.say.clone(), false));
                }
            }
            match best {
                None => cursors.pass(p + 1),
                Some(((end, _, _), say, guard)) => {
                    // a guard, or text already said this way: claimed, not
                    // changed
                    let written: String = chars[p..end].iter().collect();
                    if !guard && written != say {
                        found.push((p, end, say));
                    }
                    pos = end;
                    cursors.pass(pos);
                }
            }
        }
        let mut forced = Vec::new();
        for (s, e, symbol) in names {
            if let Some(say) = self.symbols.get(&without_variation_selectors(symbol)) {
                let written: String = chars[*s..*e].iter().collect();
                if &written != say {
                    forced.push((*s, *e, say.clone()));
                }
            }
        }
        if !forced.is_empty() {
            found.extend(forced);
            found.sort();
        }
        found
    }

    /// `(position, literals)` of every place a literal key may start, in
    /// order.
    ///
    /// A key that starts with a letter or digit starts at a break, which
    /// before a letter or digit is the start of a run of them or a camelCase
    /// hump inside one, and its first run ends at the end of that run or at a
    /// later hump.  Only the lengths some key has are looked up, so a hostile
    /// run like "aBaBaB..." costs a probe per hump per length.
    fn candidates(&self, chars: &[char]) -> Vec<(usize, Vec<usize>)> {
        let mut found: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        let n = chars.len();
        if !self.lengths.is_empty() {
            let mut a = 0;
            while a < n {
                if !is_word_char(chars[a]) {
                    a += 1;
                    continue;
                }
                let mut b = a;
                while b < n && is_word_char(chars[b]) {
                    b += 1;
                }
                let humps: Vec<usize> =
                    (a + 1..b).filter(|&i| is_hump(chars[i - 1], chars[i])).collect();
                if humps.is_empty() {
                    if self.lengths.binary_search(&(b - a)).is_ok() {
                        self.look(chars, a, b, &mut found);
                    }
                } else {
                    let mut stops = humps.clone();
                    stops.push(b);
                    for p in std::iter::once(a).chain(humps.iter().copied()) {
                        for &len in &self.lengths {
                            let q = p + len;
                            if q > b {
                                break;
                            }
                            if stops.binary_search(&q).is_ok() {
                                self.look(chars, p, q, &mut found);
                            }
                        }
                    }
                }
                a = b;
            }
        }
        if !self.leads.is_empty() || !self.folded_leads.is_empty() {
            for (i, &c) in chars.iter().enumerate() {
                let mut ids: Vec<usize> = Vec::new();
                if let Some(v) = self.leads.get(&c) {
                    ids.extend(v);
                }
                if !self.folded_leads.is_empty() {
                    if let Some(v) = self.folded_leads.get(&lower([c])) {
                        ids.extend(v);
                    }
                }
                if !ids.is_empty() {
                    found.entry(i).or_default().extend(ids);
                }
            }
        }
        found.into_iter().collect()
    }

    /// Add the literals whose first run is `chars[p..q]` at `p`.
    fn look(&self, chars: &[char], p: usize, q: usize, found: &mut BTreeMap<usize, Vec<usize>>) {
        let unit: String = chars[p..q].iter().collect();
        let mut ids: Vec<usize> = Vec::new();
        if let Some(v) = self.runs.get(&unit) {
            ids.extend(v);
        }
        if let Some(v) = self.folded_runs.get(&lower(unit.chars())) {
            ids.extend(v);
        }
        if !ids.is_empty() {
            found.entry(p).or_default().extend(ids);
        }
    }
}

/// Where `lit` ends when it matches `chars` at `p`, else None.  The start
/// break is the candidate's business; this checks the rest: each word as
/// written (or in any case, for a key without a capital), the gap between
/// words, and a break after a key that ends in a letter or digit.
fn match_literal(lit: &Literal, chars: &[char], p: usize) -> Option<usize> {
    lit.forms.iter().find_map(|form| match_form(form, lit.exact, chars, p))
}

fn match_form(words: &[Vec<char>], exact: bool, chars: &[char], p: usize) -> Option<usize> {
    let n = chars.len();
    let mut i = p;
    for (k, word) in words.iter().enumerate() {
        if k > 0 {
            i = gap(chars, i)?;
        }
        if i + word.len() > n {
            return None;
        }
        for (j, &w) in word.iter().enumerate() {
            let t = chars[i + j];
            if t != w && (exact || !same_folded(t, w)) {
                return None;
            }
        }
        i += word.len();
    }
    let last = *words.last()?.last()?;
    if is_word_char(last) && i < n && is_word_char(chars[i]) && !is_hump(chars[i - 1], chars[i]) {
        return None;
    }
    Some(i)
}

/// Past the space between two words of a phrase: any run of blanks holding at
/// most one line break, and after the break the gutter of a blockquote.  None
/// when there is no space at `i`.
fn gap(chars: &[char], mut i: usize) -> Option<usize> {
    let n = chars.len();
    if i >= n || !is_space(chars[i]) {
        return None;
    }
    let blank = |c: char| c != '\n' && is_space(c);
    while i < n && blank(chars[i]) {
        i += 1;
    }
    if i < n && chars[i] == '\n' {
        i += 1;
        while i < n && blank(chars[i]) {
            i += 1;
        }
        while i < n && chars[i] == GUTTER {
            i += 1;
            while i < n && blank(chars[i]) {
                i += 1;
            }
        }
    }
    Some(i)
}

/// A pattern's next match: where it starts and ends, and what it says.
struct Match {
    start: usize,
    end: usize,
    say: String,
}

/// The next match of every pattern at or after some position.
struct Cursors<'a> {
    patterns: &'a [Pattern],
    text: &'a str,
    /// character index -> byte offset, and back
    byte_of: Vec<usize>,
    char_of: HashMap<usize, usize>,
    /// per pattern: the position its match was looked for from, and the match
    next: Vec<Option<(usize, Option<Match>)>>,
}

impl<'a> Cursors<'a> {
    fn new(patterns: &'a [Pattern], text: &'a str) -> Self {
        let (mut byte_of, mut char_of): (Vec<usize>, HashMap<usize, usize>) = (Vec::new(), HashMap::new());
        if !patterns.is_empty() {
            for (c, (b, _)) in text.char_indices().enumerate() {
                byte_of.push(b);
                char_of.insert(b, c);
            }
            char_of.insert(text.len(), byte_of.len());
            byte_of.push(text.len());
        }
        Cursors { patterns, text, byte_of, char_of, next: (0..patterns.len()).map(|_| None).collect() }
    }

    /// The first match of pattern `k` that starts at or after `from` and says
    /// something: not empty, and its respelling holds a letter or digit.
    fn search(&self, k: usize, mut from: usize) -> Option<Match> {
        let pattern = &self.patterns[k];
        while from < self.byte_of.len() {
            let caps = pattern.regex.captures_at(self.text, self.byte_of[from])?;
            let m = caps.get(0)?;
            let start = self.char_of[&m.start()];
            let end = self.char_of[&m.end()];
            if end > start {
                let mut said = String::new();
                caps.expand(&pattern.template, &mut said);
                let say = collapse(&said);
                if say.chars().any(is_word_char) {
                    return Some(Match { start, end, say });
                }
            }
            from = start + 1;
        }
        None
    }

    /// Bring every cursor up to `pos` and return the earliest start.
    fn next_start(&mut self, pos: usize) -> Option<usize> {
        self.pass(pos);
        self.next.iter().filter_map(|n| n.as_ref()?.1.as_ref().map(|m| m.start)).min()
    }

    /// Move every cursor whose match starts before `pos` to its next match.
    fn pass(&mut self, pos: usize) {
        for k in 0..self.patterns.len() {
            let stale = match &self.next[k] {
                None => true,
                Some((_, Some(m))) => m.start < pos,
                Some((_, None)) => false,
            };
            if stale {
                let m = self.search(k, pos);
                self.next[k] = Some((pos, m));
            }
        }
    }

    fn starting_at(&self, p: usize) -> Vec<(usize, &Match)> {
        self.next
            .iter()
            .enumerate()
            .filter_map(|(k, n)| match n {
                Some((_, Some(m))) if m.start == p => Some((k, m)),
                _ => None,
            })
            .collect()
    }
}

/// A letter or digit, or a combining accent.
fn alnum(c: Option<char>) -> bool {
    c.is_some_and(is_word_char)
}

/// Whether `c` would swallow a respelling written up against it: a letter, a
/// digit or an underscore.
fn glues(c: Option<char>) -> bool {
    alnum(c) || c == Some('_')
}

/// How `k` slots divide `say`: each slot's (start, end) inside it, and how
/// many padding spaces to append so that every slot has a character.
///
/// With a word for each slot, each takes one and the last takes the rest.
/// With fewer words, the last word is cut into characters for the slots left
/// over, longer parts first, and a slot with no character left gets a space.
fn share(say: &[char], k: usize) -> (Vec<(usize, usize)>, usize) {
    if k == 0 {
        return (Vec::new(), 0);
    }
    let mut pieces = Vec::new();
    let mut i = 0;
    while i < say.len() {
        if is_space(say[i]) {
            i += 1;
            continue;
        }
        let s = i;
        while i < say.len() && !is_space(say[i]) {
            i += 1;
        }
        pieces.push((s, i));
    }
    let m = pieces.len();
    if m >= k {
        let mut spans = pieces[..k - 1].to_vec();
        spans.push((pieces[k - 1].0, pieces[m - 1].1));
        return (spans, 0);
    }
    let mut spans: Vec<(usize, usize)> = if m > 0 { pieces[..m - 1].to_vec() } else { Vec::new() };
    let (mut p, q) = if m > 0 { pieces[m - 1] } else { (say.len(), say.len()) };
    let c = k - spans.len();
    if q - p >= c {
        let (width, extra) = ((q - p) / c, (q - p) % c);
        for t in 0..c {
            let w = width + usize::from(t < extra);
            spans.push((p, p + w));
            p += w;
        }
        return (spans, 0);
    }
    spans.extend((p..q).map(|x| (x, x + 1)));
    let pad = c - (q - p);
    spans.extend((0..pad).map(|t| (say.len() + t, say.len() + t + 1)));
    (spans, pad)
}

/// `text` with every match said its way: `(text, offsets, word_texts)`.
///
/// `slots` are the `(start, end)` of the chunk's word slots, in order, never
/// empty and never overlapping; `matches` come from `Lexicon::find`.  Every
/// slot comes back non-empty, in order, and found at its offset in the new
/// text.  Err when the slots and the matches do not fit together.
pub fn respell(
    text: &str,
    slots: &[(usize, usize)],
    matches: &[(usize, usize, String)],
) -> Result<(String, Vec<usize>, Vec<String>), String> {
    let chars: Vec<char> = text.chars().collect();
    let piece = |a: usize, b: usize| -> Result<String, String> {
        chars.get(a..b).map(|s| s.iter().collect()).ok_or_else(|| format!("no text at {a}..{b}"))
    };
    if matches.is_empty() {
        let texts = slots.iter().map(|&(a, b)| piece(a, b)).collect::<Result<_, _>>()?;
        return Ok((text.to_string(), slots.iter().map(|s| s.0).collect(), texts));
    }
    let starts: Vec<usize> = matches.iter().map(|m| m.0).collect();
    let ends: Vec<usize> = matches.iter().map(|m| m.1).collect();
    let slot_starts: Vec<usize> = slots.iter().map(|s| s.0).collect();
    let slot_ends: Vec<usize> = slots.iter().map(|s| s.1).collect();

    let mut new: Vec<char> = Vec::with_capacity(chars.len() + 32);
    let mut after = Vec::new(); // where the text after each match lands
    let mut shares: Vec<(usize, Vec<(usize, usize)>)> = Vec::new();
    let mut cursor = 0;
    for (j, (s, e, say)) in matches.iter().enumerate() {
        let (s, e) = (*s, *e);
        let gap = chars.get(cursor..s).ok_or("matches out of order")?;
        new.extend_from_slice(gap);
        let first = slot_ends.partition_point(|&x| x <= s);
        let upto = first + slot_starts[first.min(slot_starts.len())..].partition_point(|&x| x < e);
        let say: Vec<char> = say.chars().collect();
        let (mut spans, pad) = share(&say, upto - first);
        let mut said = say.clone();
        said.extend(std::iter::repeat_n(' ', pad));
        // right after another match there is no gap: that match's own look
        // at this respelling already put in the space
        if alnum(said.first().copied()) && glues(gap.last().copied()) {
            said.insert(0, ' ');
            for span in &mut spans {
                *span = (span.0 + 1, span.1 + 1);
            }
        }
        let follows = if j + 1 < matches.len() && starts[j + 1] == e {
            matches[j + 1].2.chars().next()
        } else {
            chars.get(e).copied()
        };
        if alnum(said.last().copied()) && glues(follows) {
            said.push(' ');
        }
        let size = new.len();
        shares.push((first, spans.iter().map(|&(x, y)| (size + x, size + y)).collect()));
        new.extend_from_slice(&said);
        after.push(new.len());
        cursor = e;
    }
    new.extend_from_slice(chars.get(cursor..).ok_or("match past the end")?);

    // where position x, outside every match, lands in the new text
    let moved = |x: usize| -> usize {
        let j = ends.partition_point(|&e| e <= x);
        if j == 0 { x } else { after[j - 1] + x - ends[j - 1] }
    };
    let span_of = |j: usize, i: usize| -> Result<(usize, usize), String> {
        let (first, spans) = &shares[j];
        i.checked_sub(*first)
            .and_then(|t| spans.get(t))
            .copied()
            .ok_or_else(|| format!("slot {i} is not among match {j}'s"))
    };
    let mut offsets = Vec::with_capacity(slots.len());
    let mut texts = Vec::with_capacity(slots.len());
    for (i, &(a, b)) in slots.iter().enumerate() {
        let j = starts.partition_point(|&s| s <= a);
        let x = if j > 0 && a < ends[j - 1] { span_of(j - 1, i)?.0 } else { moved(a) };
        let j = starts.partition_point(|&s| s < b);
        let y = if j > 0 && b <= ends[j - 1] { span_of(j - 1, i)?.1 } else { moved(b) };
        if x > y || y > new.len() {
            return Err(format!("slot {i} lands outside the new text"));
        }
        offsets.push(x);
        texts.push(new[x..y].iter().collect());
    }
    Ok((new.into_iter().collect(), offsets, texts))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex(pairs: &[(&str, &str)]) -> Lexicon {
        let pairs: Vec<(String, String, bool)> = pairs
            .iter()
            .map(|(k, s)| {
                let pattern = k.len() >= 3 && k.starts_with('/') && k.ends_with('/');
                (k.to_string(), s.to_string(), pattern)
            })
            .collect();
        Lexicon::new(&pairs)
    }

    fn said(l: &Lexicon, text: &str) -> String {
        let n = text.chars().count();
        let found = l.find(text, &[]);
        respell(text, &[(0, n)], &found).unwrap().0
    }

    #[test]
    fn line_numbers() {
        let l = lex(&[(".py", "dot pie"), (r"/:(\d+)-(\d+)/", "lines $1 to $2"), (r"/:(\d+)/", "line $1")]);
        assert_eq!(said(&l, "settings.py:281-283"), "settings dot pie lines 281 to 283");
        assert_eq!(said(&l, "settings.py:498"), "settings dot pie line 498");
    }

    #[test]
    fn a_literal_is_found_by_its_break_rules() {
        let l = lex(&[("id", "ID")]);
        let f = |t: &str| l.find(t, &[]).iter().map(|m| (m.0, m.1)).collect::<Vec<_>>();
        assert_eq!(f("user.id userId idle"), vec![(5, 7), (12, 14)]);
    }

    #[test]
    fn patterns_are_checked() {
        assert!(check_pattern(r":(\d+)").is_none());
        assert!(check_pattern(r":(\d+").is_some());
        assert_eq!(check_pattern(r"\d*").as_deref(), Some("it can match empty text"));
    }
}
