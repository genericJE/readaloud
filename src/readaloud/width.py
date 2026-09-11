"""Display width of text in terminal cells.

A character index is NOT a terminal column: an East-Asian-wide or emoji glyph
occupies two cells.  Everything that talks to the terminal (paint positions,
wrap decisions, hit-testing a mouse cell) or reasons about a renderer's column
layout (``mdcat``'s table columns) has to go through these helpers, or the
highlight lands on the wrong characters and a click resolves to a word one cell
per wide glyph to the left of the one the user pointed at.

This module imports nothing curses-related, so the Document can measure a
table's columns without depending on the view.
"""

from __future__ import annotations

import unicodedata

__all__ = ["char_width", "text_width", "cell_offsets"]

_WIDE_EAW = frozenset(("W", "F"))
_ZERO_CATEGORIES = frozenset(("Mn", "Me", "Cf"))


def char_width(ch: str) -> int:
    """Terminal cells occupied by one character: 0, 1 or 2.

    Matches what ``wcwidth`` (and hence pyte, ncurses and every terminal that
    follows Unicode TR11) does for the cases that occur in real input:
    combining marks and format characters take no cell of their own, East
    Asian Wide/Fullwidth characters and emoji take two, everything else one.
    """
    if not ch:
        return 0
    o = ord(ch)
    if 0x20 <= o < 0x7F:  # ASCII fast path, by far the common case
        return 1
    if unicodedata.combining(ch) or unicodedata.category(ch) in _ZERO_CATEGORIES:
        return 0
    if unicodedata.east_asian_width(ch) in _WIDE_EAW:
        return 2
    return 1


def text_width(text: str) -> int:
    """Terminal cells occupied by `text`."""
    n = 0
    for ch in text:
        n += char_width(ch)
    return n


def cell_offsets(text: str) -> list[int]:
    """Prefix cell widths: ``out[i]`` is the column of ``text[i]``.

    Length is ``len(text) + 1``; the last entry is the whole line's width.
    """
    out = [0] * (len(text) + 1)
    n = 0
    for i, ch in enumerate(text):
        out[i] = n
        n += char_width(ch)
    out[len(text)] = n
    return out
