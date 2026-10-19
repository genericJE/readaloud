//! Character classes as Python 3.12 sees them, and the helpers built on them.
//!
//! Every offset the Python side holds counts code points, so the algorithms
//! here work on `&[char]`.  The classes come from `tables.rs`, generated from
//! Python's own Unicode database, so "a letter or digit" means exactly what
//! `str.isalnum()` means and not what Rust's newer Unicode says.

use crate::tables::{ALNUM, LOWER, SPACE, UPPER};

fn in_ranges(table: &[(u32, u32)], c: char) -> bool {
    let c = c as u32;
    table
        .binary_search_by(|&(a, b)| {
            if b < c {
                std::cmp::Ordering::Less
            } else if a > c {
                std::cmp::Ordering::Greater
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// `str.isalnum()`.
pub fn is_alnum(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_alphanumeric();
    }
    in_ranges(&ALNUM, c)
}

/// A letter or digit, or a combining accent (U+0300..U+036F): what a word is
/// built from, so "Résumé" spelled with combining accents is one word.
pub fn is_word_char(c: char) -> bool {
    is_alnum(c) || ('\u{300}'..='\u{36f}').contains(&c)
}

/// `str.isupper()` of one character.
pub fn is_upper(c: char) -> bool {
    if c.is_ascii() {
        return c.is_ascii_uppercase();
    }
    in_ranges(&UPPER, c)
}

/// `str.isspace()` of one character.
pub fn is_space(c: char) -> bool {
    in_ranges(&SPACE, c)
}

/// An ASCII lowercase letter followed by an ASCII capital: the hump of a
/// camelCase name, which counts as a break between two words.
pub fn is_hump(left: char, right: char) -> bool {
    left.is_ascii_lowercase() && right.is_ascii_uppercase()
}

/// `str.lower()` of one character, appended to `out`.
pub fn push_lower(c: char, out: &mut String) {
    if c.is_ascii() {
        out.push(c.to_ascii_lowercase());
        return;
    }
    match LOWER.binary_search_by_key(&(c as u32), |&(k, _)| k) {
        Ok(i) => out.push_str(LOWER[i].1),
        Err(_) => out.push(c),
    }
}

/// `str.lower()`, character by character (no final-sigma rule).
pub fn lower<I: IntoIterator<Item = char>>(chars: I) -> String {
    let mut out = String::new();
    for c in chars {
        push_lower(c, &mut out);
    }
    out
}

/// Whether two characters are the same letter in any case.
pub fn same_folded(a: char, b: char) -> bool {
    if a == b {
        return true;
    }
    let (mut x, mut y) = (String::new(), String::new());
    push_lower(a, &mut x);
    push_lower(b, &mut y);
    x == y
}

/// `" ".join(s.split())`: whitespace runs collapsed to one space, the ends
/// trimmed.
pub fn collapse(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for word in s.split(is_space).filter(|w| !w.is_empty()) {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(word);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classes_follow_python() {
        assert!(is_alnum('é') && is_alnum('٣') && !is_alnum('_') && !is_alnum('\u{301}'));
        assert!(is_word_char('\u{301}'));
        assert!(is_space('\u{1c}') && is_space('\u{a0}') && !is_space('│'));
        assert!(is_upper('É') && !is_upper('é'));
        assert_eq!(lower("İD".chars()), "i\u{307}d");
        assert!(same_folded('É', 'é') && !same_folded('e', 'é'));
        assert_eq!(collapse("  a \t b\n"), "a b");
    }
}
