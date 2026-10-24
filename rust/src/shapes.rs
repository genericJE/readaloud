//! Shapes: text the config file describes with a pattern and says a set way,
//! such as a file reference with its line number or a host with its port.
//!
//! readaloud knows no shape of its own.  Each comes from a `[shapes]` line, a
//! regular expression whose named groups are the parts its wording names
//! (`/(?<file>\S+\.py):(?<line>\d+)/ = $file line $line`).  A shape must
//! start where a word starts and end where a word ends, so it sees the whole
//! of `settings.py:498`, both sides of the colon, and never a piece of a
//! word; it may also end past the punctuation right after a word.  It may
//! span many words and lines, so a whole function signature can be one.  The
//! lines are tried in file order at each word, and the first that fits wins,
//! with its longest fit.
//!
//! A named shape (`param: /pattern/ = wording`) is never found on its own: a
//! wording calls on it as `${group|param}`, which says every match of it in
//! that group its own way, so a list of any length can be said item by item.

use std::collections::HashMap;

use regex::{Captures, Regex, RegexSet};

/// The most words one shape may span.
pub const MAX_WORDS: usize = 48;

/// How deep named shapes may call on one another.
const MAX_DEPTH: usize = 8;

struct Shape {
    /// the pattern anchored at both ends
    full: Regex,
    /// what it says; empty says the text as written
    say: String,
}

/// A named shape: said for each of its matches in a group that names it.
pub struct Sub {
    /// the pattern as written, unanchored
    regex: Regex,
    /// what it says; empty says the match as written
    say: String,
}

/// The named shapes, by name.
pub type Subs = HashMap<String, Sub>;

/// The config file's shapes, ready to be found at word edges.
pub struct Shapes {
    list: Vec<Shape>,
    /// every pattern anchored at its start only: which shapes may begin at a
    /// word, in one pass over the words that follow it
    starts: Option<RegexSet>,
    subs: Subs,
}

/// A shape found: where it starts and ends (characters), what it says, and
/// whether it says the text as written.  `pieces` is what each word slot the
/// shape covers says, in order: each word of the wording goes to the slot it
/// came from, so a slot may say nothing (an empty piece).  Empty when the
/// text is said as written.
pub struct Found {
    pub start: usize,
    pub end: usize,
    pub say: String,
    pub as_written: bool,
    pub pieces: Vec<String>,
}

/// A piece of an expanded wording and where in the matched text it came
/// from: a byte offset, which is each character's own when `verbatim`, and
/// the end of the group or match it belongs to, past which it looks for no
/// word to light.
struct Piece {
    text: String,
    src: usize,
    verbatim: bool,
    until: usize,
}

/// Why `pattern` cannot be a shape, or the names of its groups.
pub fn check_shape(pattern: &str) -> Result<(Vec<String>, usize), String> {
    if let Some(problem) = crate::pronounce::check_pattern(pattern) {
        return Err(problem);
    }
    let regex = Regex::new(&format!("^(?:{pattern})$")).map_err(|e| e.to_string())?;
    let names = regex.capture_names().flatten().map(str::to_string).collect();
    Ok((names, regex.captures_len() - 1))
}

impl Shapes {
    /// `shapes` are `(pattern, wording)` in file order and `subs` the named
    /// ones, `(name, pattern, wording)`; one that cannot be used is skipped
    /// (config.py has reported it).
    pub fn new(shapes: &[(String, String)], subs: &[(String, String, String)]) -> Self {
        let mut list = Vec::new();
        let mut starts = Vec::new();
        for (pattern, say) in shapes {
            if check_shape(pattern).is_err() {
                continue;
            }
            let Ok(full) = Regex::new(&format!("^(?:{pattern})$")) else { continue };
            starts.push(format!("^(?:{pattern})"));
            list.push(Shape { full, say: crate::text::collapse(say) });
        }
        let starts = if list.is_empty() { None } else { RegexSet::new(&starts).ok() };
        let mut named = Subs::new();
        for (name, pattern, say) in subs {
            if check_shape(pattern).is_err() {
                continue;
            }
            let Ok(regex) = Regex::new(pattern) else { continue };
            named.insert(name.clone(), Sub { regex, say: crate::text::collapse(say) });
        }
        Shapes { list, starts, subs: named }
    }

    pub fn len(&self) -> usize {
        self.list.len()
    }

    /// The named shapes, which pattern wordings may call on too.
    pub fn subs(&self) -> &Subs {
        &self.subs
    }
}

/// `template` with the groups of `caps` in place.
///
/// `$name` and `${name}` are what the group called `name` matched, `$1` and
/// so on a group by number, `$0` the whole match, and `$$` a dollar sign, as
/// in the regex crate: `$name` takes the longest run of letters, digits and
/// underscores, and a group that did not take part says nothing.
/// `${name|from=to}` is that group with every `from` in it said as the word
/// `to` (with spaces round it); several `|from=to` may follow one another.
#[cfg(test)]
pub fn expand(template: &str, caps: &Captures) -> String {
    expand_in(template, caps, &Subs::new(), 0)
}

/// `expand`, where a `|piece` without `=` names one of `subs`: every match of
/// that named shape in the group is said by its own wording, and the text
/// between the matches goes through the group's `|from=to` replacements.
/// Only the first such name counts, an unknown one is ignored, and past
/// `MAX_DEPTH` named shapes calling on named shapes the name is ignored too.
pub fn expand_in(template: &str, caps: &Captures, subs: &Subs, depth: usize) -> String {
    expand_pieces(template, caps, subs, depth, 0).into_iter().map(|p| p.text).collect()
}

/// `expand_in` as pieces that know where they came from.  A group's text
/// comes from where it matched; the wording's own words come from the text
/// between the groups round them, so "function" in `function $fn` comes
/// from the `def ` before the name.  `base` is where the text `caps`
/// matched starts in the text the shape matched.
fn expand_pieces(template: &str, caps: &Captures, subs: &Subs, depth: usize, base: usize) -> Vec<Piece> {
    let whole = caps.get(0).unwrap();
    // where a byte of the text `caps` matched lies in the matched text
    let at = |b: usize| base + b - whole.start();
    let group = |name: &str| -> Option<regex::Match> {
        match name.parse::<usize>() {
            Ok(i) => caps.get(i),
            Err(_) => caps.name(name),
        }
    };
    let word = |c: char| c.is_ascii_alphanumeric() || c == '_';
    let mut out: Vec<Piece> = Vec::new();
    // the wording's own words: from where the last group said ended, up to
    // the end of the match
    let own = |out: &mut Vec<Piece>, text: &str, src: usize| {
        if !text.is_empty() {
            out.push(Piece { text: text.to_string(), src, verbatim: false, until: at(whole.end()) });
        }
    };
    let mut prev_end = at(whole.start());
    let mut rest = template;
    while let Some(dollar) = rest.find('$') {
        own(&mut out, &rest[..dollar], prev_end);
        let after = &rest[dollar + 1..];
        if let Some(tail) = after.strip_prefix('$') {
            own(&mut out, "$", prev_end);
            rest = tail;
        } else if let Some(inner) = after.strip_prefix('{') {
            let Some(close) = inner.find('}') else {
                own(&mut out, "$", prev_end);
                rest = after;
                continue;
            };
            // as in the regex crate, anything up to the } is a name, and a
            // name no group has says nothing
            let mut pieces = inner[..close].split('|');
            let (text, start) = group(pieces.next().unwrap_or(""))
                .map_or(("", prev_end), |m| (m.as_str(), at(m.start())));
            let until = start + text.len();
            let mut swaps: Vec<(&str, &str)> = Vec::new();
            let mut name = None;
            for piece in pieces {
                match piece.split_once('=') {
                    Some((from, to)) if !from.is_empty() => swaps.push((from, to)),
                    Some(_) => {}
                    None => name = name.or(Some(piece)),
                }
            }
            let sub = name.filter(|_| depth < MAX_DEPTH).and_then(|n| subs.get(n));
            let mut last = 0;
            if let Some(sub) = sub {
                for m in sub.regex.captures_iter(text) {
                    let found = m.get(0).unwrap();
                    if found.is_empty() {
                        continue;
                    }
                    swapped(&text[last..found.start()], &swaps, start + last, until, &mut out);
                    let from = start + found.start();
                    let to = start + found.end();
                    out.push(Piece { text: " ".into(), src: from, verbatim: false, until: to });
                    if sub.say.is_empty() {
                        out.push(Piece { text: found.as_str().to_string(), src: from, verbatim: true, until: to });
                    } else {
                        out.extend(expand_pieces(&sub.say, &m, subs, depth + 1, from));
                    }
                    out.push(Piece { text: " ".into(), src: from, verbatim: false, until: to });
                    last = found.end();
                }
            }
            swapped(&text[last..], &swaps, start + last, until, &mut out);
            prev_end = until;
            rest = &inner[close + 1..];
        } else {
            let n = after.find(|c: char| !word(c)).unwrap_or(after.len());
            if n == 0 {
                own(&mut out, "$", prev_end);
            } else if let Some(m) = group(&after[..n]) {
                out.push(Piece { text: m.as_str().to_string(), src: at(m.start()), verbatim: true, until: at(m.end()) });
                prev_end = at(m.end());
            }
            rest = &after[n..];
        }
    }
    own(&mut out, rest, prev_end);
    out
}

/// `text`, which starts at byte `at` of the matched text in a group that
/// ends at `until`, onto `out` with every `from` of `swaps` said as ` to `:
/// one pass, so a replacement is never replaced again, and the first that
/// fits wins.
fn swapped(text: &str, swaps: &[(&str, &str)], at: usize, until: usize, out: &mut Vec<Piece>) {
    let mut i = 0;
    let mut kept = 0;
    while i < text.len() {
        match swaps.iter().find(|(from, _)| text[i..].starts_with(from)) {
            Some((from, to)) => {
                if kept < i {
                    out.push(Piece { text: text[kept..i].to_string(), src: at + kept, verbatim: true, until });
                }
                out.push(Piece { text: format!(" {to} "), src: at + i, verbatim: false, until });
                i += from.len();
                kept = i;
            }
            None => i += text[i..].chars().next().unwrap().len_utf8(),
        }
    }
    if kept < text.len() {
        out.push(Piece { text: text[kept..].to_string(), src: at + kept, verbatim: true, until });
    }
}

/// What each of the `covered` slots (character spans in the text the shape
/// matched, which starts at character `start`) says: the words of `pieces`
/// in order, each at the slot its source lies in, or else the next slot of
/// its group, so "takes" from the `(` before `self` lights `self` and "or"
/// from a `|` lights the type after it; a group with no slot of its own, a
/// default `=''`, goes with the slot before it.  A word never goes back to
/// an earlier slot than the word before it, so the wording keeps its order;
/// a slot no word reaches says nothing.
fn shared(pieces: &[Piece], matched: &str, start: usize, covered: &[(usize, usize)]) -> Vec<String> {
    // byte offset in the matched text -> character offset in the chunk
    let mut char_at: Vec<usize> = vec![0; matched.len() + 1];
    for (c, (b, ch)) in matched.char_indices().enumerate() {
        for x in b..b + ch.len_utf8() {
            char_at[x] = start + c;
        }
    }
    char_at[matched.len()] = start + matched.chars().count();
    let slot_of = |src: usize, until: usize| -> usize {
        let x = char_at[src.min(matched.len())];
        let limit = char_at[until.clamp(src, matched.len())];
        covered
            .iter()
            .position(|&(a, b)| a <= x && x < b)
            .or_else(|| covered.iter().position(|&(a, _)| x <= a && a < limit))
            .or_else(|| covered.iter().rposition(|&(_, b)| b <= x))
            .unwrap_or(0)
    };
    // the words, each with where it comes from and whether a space came
    // before it
    let mut words: Vec<(String, usize, usize, bool)> = Vec::new();
    let mut spaced = true;
    for piece in pieces {
        let mut open = false;
        for (b, c) in piece.text.char_indices() {
            if crate::text::is_space(c) {
                (open, spaced) = (false, true);
            } else if open {
                words.last_mut().unwrap().0.push(c);
            } else {
                let src = if piece.verbatim { piece.src + b } else { piece.src };
                words.push((c.to_string(), src, piece.until, spaced));
                (open, spaced) = (true, false);
            }
        }
    }
    let mut said: Vec<String> = vec![String::new(); covered.len()];
    let (mut slot, mut prev) = (0, None);
    for (word, src, until, spaced) in &words {
        // punctuation written up against a word stays with it: "$line,
        // column" says the comma where it says the line
        let sticks = !spaced && prev.is_some() && !word.chars().any(crate::text::is_word_char);
        if !sticks {
            slot = slot.max(slot_of(*src, *until));
        }
        if !said[slot].is_empty() && (*spaced || prev != Some(slot)) {
            said[slot].push(' ');
        }
        said[slot].push_str(word);
        prev = Some(slot);
    }
    said
}

/// Where each character starts in `text`, plus the end.
fn byte_offsets(text: &str) -> Vec<usize> {
    let mut out: Vec<usize> = text.char_indices().map(|(b, _)| b).collect();
    out.push(text.len());
    out
}

impl Shapes {
    /// Every shape found in `text` at the word `slots` (character spans,
    /// sorted, never overlapping): at most one starting at each word, the
    /// first line that fits there, with its longest fit.
    pub fn found(&self, text: &str, slots: &[(usize, usize)]) -> Vec<Found> {
        let Some(starts) = &self.starts else { return Vec::new() };
        let bytes = byte_offsets(text);
        let n = bytes.len() - 1;
        let mut out = Vec::new();
        let chars: Vec<char> = text.chars().collect();
        for (i, &(a, _)) in slots.iter().enumerate() {
            // where a shape starting here may end: at the end of one of the
            // next few words, or past the punctuation right after it ("x.py(12)"
            // ends after the bracket), never inside a word
            let mut ends = Vec::new();
            for (j, &(_, e)) in slots.iter().enumerate().skip(i).take(MAX_WORDS) {
                if e <= a || e > n {
                    continue;
                }
                ends.push(e);
                let stop = slots.get(j + 1).map_or(n, |s| s.0);
                let mut x = e;
                while x < stop && !crate::text::is_space(chars[x]) {
                    x += 1;
                }
                if x > e {
                    ends.push(x);
                }
            }
            ends.sort_unstable();
            ends.dedup();
            let Some(&far) = ends.last() else { continue };
            let window = &text[bytes[a]..bytes[far]];
            let maybe = starts.matches(window);
            if !maybe.matched_any() {
                continue;
            }
            'shapes: for k in maybe.iter() {
                let shape = &self.list[k];
                for &end in ends.iter().rev() {
                    let piece = &text[bytes[a]..bytes[end]];
                    let Some(caps) = shape.full.captures(piece) else { continue };
                    if shape.say.is_empty() {
                        out.push(Found { start: a, end, say: piece.to_string(), as_written: true, pieces: Vec::new() });
                        break 'shapes;
                    }
                    let expanded = expand_pieces(&shape.say, &caps, &self.subs, 0, 0);
                    let say = crate::text::collapse(&expanded.iter().map(|p| p.text.as_str()).collect::<String>());
                    let covered: Vec<(usize, usize)> =
                        slots[i..].iter().copied().take_while(|&(s, _)| s < end).collect();
                    let pieces = shared(&expanded, piece, a, &covered);
                    out.push(Found { start: a, end, say, as_written: false, pieces });
                    break 'shapes;
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn slots(text: &str) -> Vec<(usize, usize)> {
        // words split at white space only, enough for these tests
        let mut out = Vec::new();
        let mut start = None;
        for (i, c) in text.chars().chain(std::iter::once(' ')).enumerate() {
            match (crate::text::is_space(c), start) {
                (false, None) => start = Some(i),
                (true, Some(s)) => {
                    out.push((s, i));
                    start = None;
                }
                _ => {}
            }
        }
        out
    }

    fn said(shapes: &[(&str, &str)], text: &str) -> Vec<String> {
        said_with(shapes, &[], text)
    }

    fn said_with(shapes: &[(&str, &str)], subs: &[(&str, &str, &str)], text: &str) -> Vec<String> {
        let shapes: Vec<(String, String)> =
            shapes.iter().map(|(p, s)| (p.to_string(), s.to_string())).collect();
        let subs: Vec<(String, String, String)> =
            subs.iter().map(|(n, p, s)| (n.to_string(), p.to_string(), s.to_string())).collect();
        Shapes::new(&shapes, &subs).found(text, &slots(text)).into_iter().map(|f| f.say).collect()
    }

    const IDENT: &str = r"[A-Za-z_]\w*";

    /// A Python `def`: a default and a parameter as named shapes.
    fn def_shapes() -> (Vec<(String, String)>, Vec<(String, String, String)>) {
        let dflt = r"=\s*(?<v>[^,)]+)";
        let param = format!(r"(?<name>{IDENT})\s*(?<type>:\s*[^,=)]+)?\s*(?<default>=\s*[^,)]+)?");
        let def = format!(
            r"def\s+(?<fn>{IDENT})\s*\((?<params>[^()]*?),?\s*\)\s*(?<ret>->\s*[^:]+)?:"
        );
        (
            vec![(def, "function $fn takes ${params|param|,=and} ${ret|->=and returns a}".into())],
            vec![
                ("dflt".into(), dflt.into(), "with a default of $v".into()),
                ("param".into(), param, "$name ${default|dflt} ${type|:=type}".into()),
            ],
        )
    }

    fn said_def(text: &str) -> Vec<String> {
        let (shapes, subs) = def_shapes();
        Shapes::new(&shapes, &subs).found(text, &slots(text)).into_iter().map(|f| f.say).collect()
    }

    /// What each word slot of `text` says under the def shapes.
    fn lit_def(text: &str) -> Vec<String> {
        let (shapes, subs) = def_shapes();
        let found = Shapes::new(&shapes, &subs).found(text, &slots(text));
        found.into_iter().next().map(|f| f.pieces).unwrap_or_default()
    }

    #[test]
    fn each_word_of_a_wording_lights_the_word_it_came_from() {
        // "function" comes from the `def` before the name, "takes" from the
        // `(` before self, so it lights self; `->` is no word, so "and
        // returns a" goes to the word after it
        // (the test slots split at white space only; the document's words
        // leave the punctuation out, see tests/test_shapes.py)
        assert_eq!(
            lit_def("def foo(self, bar: int, baz: str='') -> str:"),
            ["function", "foo takes self and", "bar type", "int and", "baz", "with a default of '' type str", "and returns a", "str"]
        );
    }

    #[test]
    fn a_slot_no_word_reaches_says_nothing_and_the_order_holds() {
        let shapes = [(r"(?<a>\w+) (?<b>\w+) (?<c>\w+)", "$c then $a")];
        let found = Shapes::new(&to_owned(&shapes), &[]).found("x y z", &slots("x y z"));
        // "z" belongs to the last slot, so "x" cannot go back to the first
        assert_eq!(found[0].pieces, ["", "", "z then x"]);
        let shapes = [(r"(?<a>\w+) (?<b>\w+)", "$a: $b")];
        let found = Shapes::new(&to_owned(&shapes), &[]).found("x y", &slots("x y"));
        assert_eq!(found[0].pieces, ["x:", "y"]);
    }

    fn to_owned(shapes: &[(&str, &str)]) -> Vec<(String, String)> {
        shapes.iter().map(|(p, s)| (p.to_string(), s.to_string())).collect()
    }

    #[test]
    fn a_sub_shape_says_each_match_and_swaps_the_rest() {
        assert_eq!(
            said_def("def foo(bar: int, baz: str='') -> str:"),
            vec!["function foo takes bar type int and baz with a default of '' type str and returns a str"]
        );
        assert_eq!(said_def("def f(a, *args):"), vec!["function f takes a and * args"]);
    }

    #[test]
    fn a_shape_may_span_many_words_over_lines() {
        let text = "def foo(\n    bar: int,\n    baz: str = '',\n) -> str:";
        assert_eq!(
            said_def(text),
            vec!["function foo takes bar type int and baz with a default of '' type str and returns a str"]
        );
        let many: Vec<String> = (0..20).map(|i| format!("p{i}: int")).collect();
        let long = format!("def f({}):", many.join(",\n      "));
        assert_eq!(said_def(&long)[0].matches(" type int").count(), 20);
    }

    #[test]
    fn a_sub_shape_nests_and_stops_at_the_depth_cap() {
        // a name calling on itself: each level says the text one word in
        let subs = [("me", r"\w+(?<rest>(?: \w+)*)", "x ${rest|me}")];
        let out = said_with(&[(r"go (?<all>.+)", "${all|me}")], &subs, "go a b c d e f g h i j k l");
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].matches('x').count(), MAX_DEPTH);
    }

    #[test]
    fn an_empty_sub_wording_says_the_match_as_written() {
        let subs = [("n", r"\d+", "")];
        assert_eq!(said_with(&[(r"\[(?<l>[^\]]+)\]", "list ${l|n|,=and}")], &subs, "[1,22]"), vec!["list 1 and 22"]);
    }

    #[test]
    fn an_unknown_name_is_ignored_and_the_first_name_counts() {
        let subs = [("n", r"\d+", "number $0"), ("w", r"\w+", "word $0")];
        let shape = |w: &'static str| [(r"\[(?<l>[^\]]+)\]", w)];
        assert_eq!(said_with(&shape("${l|nope|,=and}"), &subs, "[1,2]"), vec!["1 and 2"]);
        assert_eq!(said_with(&shape("${l|n|w}"), &subs, "[1,2]"), vec!["number 1 , number 2"]);
        // a named shape that could match nothing is never on
        assert_eq!(said_with(&shape("${l|e}"), &[("e", r"\d*", "x")], "[1,2]"), vec!["1,2"]);
    }

    #[test]
    fn a_shape_is_a_whole_word() {
        let file = [(r"(?<file>\S+\.py):(?<line>\d+)", "$file line $line")];
        assert_eq!(said(&file, "see a.py:12 now"), vec!["a.py line 12"]);
        assert!(said(&file, "see a.py:12x now").is_empty());
    }

    #[test]
    fn the_first_line_that_fits_wins() {
        let shapes = [(r"(?<a>\w+):(?<b>\d+)", "first $a"), (r"\w+:\d+", "second")];
        assert_eq!(said(&shapes, "x:1"), vec!["first x"]);
    }

    #[test]
    fn a_shape_may_span_words_with_its_longest_fit() {
        let shapes = [(r"(?<f>\S+\.py) line (?<l>\d+)", "$f at $l")];
        assert_eq!(said(&shapes, "in a.py line 12 ok"), vec!["a.py at 12"]);
    }

    #[test]
    fn an_empty_wording_says_it_as_written() {
        let shapes = Shapes::new(&[(r"\d\d:\d\d".to_string(), String::new())], &[]);
        let found = shapes.found("at 14:35", &slots("at 14:35"));
        assert!(found[0].as_written && found[0].say == "14:35");
    }

    #[test]
    fn wordings_expand_like_the_regex_crate_and_replace() {
        let re = Regex::new(r"(?<path>/[\w/]+)-(?<n>\d+)").unwrap();
        let caps = re.captures("/docs/index-12").unwrap();
        assert_eq!(expand("$n $$ ${n}x $nx $9 $0", &caps), "12 $ 12x   /docs/index-12");
        // without a |, exactly what the regex crate makes of it
        for template in ["$n $$ ${n}x $nx $9 $0", "${path}$n", "$", "a$", "${", "${}", "$1$2$x", "${n}}", "${bad name}", "$ x", "$é"] {
            let mut want = String::new();
            caps.expand(template, &mut want);
            assert_eq!(expand(template, &caps), want, "{template}");
        }
        assert_eq!(expand("${path|/=slash}", &caps), " slash docs slash index");
        assert_eq!(expand("${path|/=slash|s=ess}", &caps), " slash doc ess  slash index");
        assert_eq!(expand("${path|=x} ${bad name} ${n", &caps), "/docs/index  ${n");
    }

    #[test]
    fn patterns_are_checked() {
        assert_eq!(check_shape(r"(?<a>x)(\d)").unwrap(), (vec!["a".to_string()], 2));
        assert!(check_shape(r"(x").is_err());
        assert!(check_shape(r"x*").is_err());
    }
}
