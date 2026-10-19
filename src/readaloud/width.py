"""Display width of text in terminal cells.

A character index is NOT a terminal column: an East-Asian-wide or emoji glyph
occupies two cells.  Everything that talks to the terminal (paint positions,
wrap decisions, hit-testing a mouse cell) or reasons about a renderer's column
layout (``mdcat``'s table columns) has to go through these helpers, or the
highlight lands on the wrong characters and a click resolves to a word one cell
per wide glyph to the left of the one the user pointed at.

The measuring is done by :mod:`readaloud._core` (``rust/src/width.rs``):
combining marks and format characters take no cell of their own, East Asian
Wide/Fullwidth characters and emoji take two, everything else one, which is
what ``wcwidth`` (and hence pyte, ncurses and every terminal that follows
Unicode TR11) does for the cases that occur in real input.  ASCII text never
leaves Python: every ASCII character is one cell.

This module imports nothing curses-related, so the Document can measure a
table's columns without depending on the view.
"""

from __future__ import annotations

from . import _core

__all__ = ["char_width", "text_width", "cell_offsets"]


def char_width(ch: str) -> int:
    """Terminal cells occupied by one character: 0, 1 or 2."""
    if len(ch) == 1 and " " <= ch < "\x7f":  # by far the common case
        return 1
    return _core.char_width(ch)


def text_width(text: str) -> int:
    """Terminal cells occupied by `text`."""
    if text.isascii():
        return len(text)
    return _core.text_width(text)


def cell_offsets(text: str) -> list[int]:
    """Prefix cell widths: ``out[i]`` is the column of ``text[i]``.

    Length is ``len(text) + 1``; the last entry is the whole line's width.
    """
    if text.isascii():
        return list(range(len(text) + 1))
    return _core.cell_offsets(text)
