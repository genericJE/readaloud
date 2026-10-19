//! How many terminal cells a character takes: the rules of `readaloud.width`.

use crate::tables::{WIDE, ZERO_WIDTH};

fn in_ranges(table: &[(u32, u32)], c: u32) -> bool {
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

/// 0 for a combining mark or a format character, 2 for an East Asian Wide or
/// Fullwidth character, 1 for everything else.
pub fn char_width(c: char) -> usize {
    let o = c as u32;
    if (0x20..0x7f).contains(&o) {
        return 1;
    }
    if in_ranges(&ZERO_WIDTH, o) {
        0
    } else if in_ranges(&WIDE, o) {
        2
    } else {
        1
    }
}

pub fn text_width(text: &str) -> usize {
    text.chars().map(char_width).sum()
}

/// The column each character starts at, plus the whole width at the end.
pub fn cell_offsets(text: &str) -> Vec<usize> {
    let mut out = Vec::with_capacity(text.len() + 1);
    let mut n = 0;
    for c in text.chars() {
        out.push(n);
        n += char_width(c);
    }
    out.push(n);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn widths() {
        assert_eq!(char_width('a'), 1);
        assert_eq!(char_width('\u{301}'), 0);
        assert_eq!(char_width('漢'), 2);
        assert_eq!(char_width('\u{200b}'), 0);
        assert_eq!(cell_offsets("a漢b"), vec![0, 1, 3, 4]);
    }
}
