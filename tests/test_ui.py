"""Tests for `readaloud.ui` — layout, hit-testing and painting geometry.

The subject here is the difference between a *character offset* and a
*terminal column*.  An East-Asian-wide or emoji glyph occupies two cells, so on
any line containing one the two diverge, and every part of the view that
confuses them (wrapping, the current-word highlight, the mouse hit-test) is
wrong by one cell per wide glyph.

`FakeWin` is a cell grid that advances the cursor by the character's display
width, exactly as a real terminal (and pyte, and ncurses) does — so a highlight
painted at the wrong column shows up here the same way it does on screen.
"""

from __future__ import annotations

import curses
import os
import sys
import unicodedata

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from readaloud.ui import (  # noqa: E402
    Screen,
    cell_offsets,
    char_width,
    text_width,
    _wrap_line,
)


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class Run:
    def __init__(self, text, style=None):
        self.text = text
        self.style = style


class Word:
    def __init__(self, text, line, start, end, idx):
        self.text = text
        self.line = line
        self.start = start
        self.end = end
        self.idx = idx


class Chunk:
    def __init__(self, idx, words):
        self.idx = idx
        self.words = list(words)
        self.line_start = 0
        self.line_end = 0


class Doc:
    """Just enough of `document.Document` for the view to lay out."""

    def __init__(self, text):
        self.plain = text.split("\n")
        self.lines = [[Run(s)] for s in self.plain]
        self.words = []
        for li, line in enumerate(self.plain):
            for start, end in _word_spans(line):
                self.words.append(
                    Word(line[start:end], li, start, end, len(self.words))
                )
        self.chunks = [Chunk(0, [w.idx for w in self.words])]


def _word_spans(line):
    spans = []
    i = 0
    while i < len(line):
        if line[i].isspace():
            i += 1
            continue
        j = i
        while j < len(line) and not line[j].isspace():
            j += 1
        spans.append((i, j))
        i = j
    return spans


class FakeWin:
    """A cell grid that honours display width, like a real terminal."""

    def __init__(self, h=10, w=60):
        self.h, self.w = h, w
        self.clear()

    def clear(self):
        self.cells = [[" "] * self.w for _ in range(self.h)]
        self.attrs = [[0] * self.w for _ in range(self.h)]
        self.y = self.x = 0

    # -- curses window surface --
    def getmaxyx(self):
        return (self.h, self.w)

    def move(self, y, x):
        if not (0 <= y < self.h and 0 <= x < self.w):
            raise curses.error("move out of range")
        self.y, self.x = y, x

    def clrtoeol(self):
        for x in range(self.x, self.w):
            self.cells[self.y][x] = " "
            self.attrs[self.y][x] = 0

    def addstr(self, y, x=None, text=None, attr=0):
        if x is None:
            y, x, text = self.y, self.x, y
        for ch in text:
            wid = char_width(ch)
            if wid == 0:
                continue
            if x + wid > self.w:
                raise curses.error("addstr past the right margin")
            self.cells[y][x] = ch
            self.attrs[y][x] = attr
            for k in range(1, wid):  # continuation cell of a wide glyph
                self.cells[y][x + k] = ""
                self.attrs[y][x + k] = attr
            x += wid
        self.y, self.x = y, min(x, self.w - 1)

    insstr = addstr

    def noutrefresh(self):
        pass

    def clearok(self, flag=True):
        pass

    def keypad(self, flag=True):
        pass

    # -- readback --
    def row_text(self, y):
        return "".join(c for c in self.cells[y] if c != "").rstrip()

    def reverse_runs(self, y):
        """``[(first cell, text)]`` for every A_REVERSE run on row `y`."""
        out, run, x0 = [], "", None
        for x in range(self.w):
            if self.attrs[y][x] & curses.A_REVERSE:
                if x0 is None:
                    x0 = x
                run += self.cells[y][x]
            elif run:
                out.append((x0, run))
                run, x0 = "", None
        if run:
            out.append((x0, run))
        return [(x, t) for x, t in out if t.strip()]


@pytest.fixture()
def nodraw(monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)


def make(text, h=10, w=60):
    win = FakeWin(h, w)
    screen = Screen(win)
    doc = Doc(text)
    screen.layout(doc, w)
    return screen, doc, win


def rendered_cell_of(win, y, word, occurrence=0):
    """First terminal cell of `word` as the fake terminal really rendered it."""
    visible = [(x, c) for x, c in enumerate(win.cells[y]) if c != ""]
    line = "".join(c for _, c in visible)
    at = -1
    for _ in range(occurrence + 1):
        at = line.find(word, at + 1)
        assert at >= 0, f"{word!r} not on row {y}: {line!r}"
    return visible[at][0]


# --------------------------------------------------------------------------- #
# width primitives
# --------------------------------------------------------------------------- #


def test_char_width_matches_unicode_tr11():
    assert [char_width(c) for c in "abc"] == [1, 1, 1]
    assert [char_width(c) for c in "日本語"] == [2, 2, 2]
    assert char_width("テ") == 2
    assert char_width("✅") == 2
    assert char_width("🚀") == 2
    assert char_width("́") == 0  # combining acute
    assert text_width("日本語 テスト") == 13
    assert text_width("plain words") == 11


def test_cell_offsets_are_prefix_columns():
    assert cell_offsets("") == [0]
    assert cell_offsets("ab") == [0, 1, 2]
    # 'a' at column 0, '日' at 1, '本' at 3, 'b' at 5, end at 6
    assert cell_offsets("a日本b") == [0, 1, 3, 5, 6]


# --------------------------------------------------------------------------- #
# the defect: clicking a word on a line that contains wide characters
# --------------------------------------------------------------------------- #


WIDE = "The team said 日本語 テスト and then alpha bravo charlie delta echo now."
EMOJI = "Build status ✅ passing 🚀 fast alpha bravo charlie delta echo now."


@pytest.mark.parametrize("line", [WIDE, EMOJI])
def test_click_lands_on_the_word_under_the_cursor(line):
    """Every cell a word renders on must hit-test back to that word."""
    screen, doc, _ = make(line, w=100)
    cols = cell_offsets(line)
    for w in doc.words:
        for cell in range(cols[w.start], cols[w.end]):
            got = screen.hit_test(0, cell)
            assert got is not None, f"cell {cell} of {w.text!r} hit nothing"
            assert doc.words[got].text == w.text, (
                f"cell {cell} renders {w.text!r} but hit-tested to "
                f"{doc.words[got].text!r}"
            )


def test_click_on_charlie_is_not_dragged_left_by_wide_chars():
    """The exact reproduction: 6 wide chars earlier on the line."""
    screen, doc, _ = make(WIDE, w=100)
    cols = cell_offsets(WIDE)
    start = WIDE.index("charlie")
    # 'charlie' renders at cells 49..55 even though its chars are 43..49
    assert cols[start] == start + 6
    widx = screen.hit_test(0, cols[start] + 3)  # the 'r' of the rendered word
    assert widx is not None and doc.words[widx].text == "charlie"


def test_hit_test_on_the_second_half_of_a_wide_glyph():
    screen, doc, _ = make(WIDE, w=100)
    cols = cell_offsets(WIDE)
    start = WIDE.index("日本語")
    for cell in range(cols[start], cols[start] + 6):
        widx = screen.hit_test(0, cell)
        assert widx is not None and doc.words[widx].text == "日本語"


def test_hit_test_line_col_reports_character_offsets():
    screen, _doc, _ = make(WIDE, w=100)
    cols = cell_offsets(WIDE)
    start = WIDE.index("charlie")
    assert screen.hit_test_line_col(0, cols[start]) == (0, start)


def test_ascii_hit_testing_is_unchanged():
    line = "The crew said plain words and then alpha bravo charlie delta echo."
    screen, doc, _ = make(line, w=100)
    at = line.index("charlie")
    widx = screen.hit_test(0, at + 3)
    assert widx is not None and doc.words[widx].text == "charlie"
    assert screen.hit_test(0, len(line) + 1) is None


def test_hit_test_past_the_end_of_a_wide_row_is_blank():
    screen, _doc, _ = make(WIDE, w=100)
    assert screen.hit_test_cell(0, text_width(WIDE)) is None
    assert screen.hit_test_cell(0, text_width(WIDE) + 5) is None


# --------------------------------------------------------------------------- #
# word_cells / word_position are terminal columns
# --------------------------------------------------------------------------- #


def test_word_cells_are_terminal_columns_not_char_offsets():
    screen, doc, _ = make(WIDE, w=100)
    cols = cell_offsets(WIDE)
    for w in doc.words:
        cells = screen.word_cells(w.idx)
        assert cells == [(0, cols[w.start], cols[w.end] - cols[w.start])]
        assert screen.word_position(w.idx) == (0, cols[w.start])


# --------------------------------------------------------------------------- #
# painting: the highlight must cover the word it belongs to
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("line", [WIDE, EMOJI])
def test_highlight_covers_the_rendered_word(line, nodraw):
    screen, doc, win = make(line, w=100)
    for w in doc.words:
        screen.invalidate()
        screen.draw(doc, 0, current_word=w.idx, current_chunk=None, status="")
        runs = win.reverse_runs(0)
        assert runs, f"nothing highlighted for {w.text!r}"
        x0, text = runs[0]
        assert text == w.text
        assert x0 == rendered_cell_of(win, 0, w.text)


def test_painting_a_wide_line_leaves_it_intact(nodraw):
    screen, doc, win = make(WIDE, w=100)
    for w in doc.words:
        screen.invalidate()
        screen.draw(doc, 0, current_word=w.idx, current_chunk=0, status="")
        assert win.row_text(0) == WIDE, f"row corrupted while highlighting {w.text!r}"


def test_wide_line_wider_than_the_terminal_is_not_overrun(nodraw):
    # 40 wide glyphs = 80 cells on a 30-column screen.
    screen, doc, win = make("日" * 40, h=6, w=30)
    screen.draw(doc, 0, current_word=0, current_chunk=0, status="")
    for y in range(screen.body_height):
        assert text_width(win.row_text(y)) <= 30


# --------------------------------------------------------------------------- #
# wrapping happens on cells, not characters
# --------------------------------------------------------------------------- #


def test_wrap_line_breaks_on_display_width():
    text = "日本 " * 6  # 5 cells per group
    spans = _word_spans(text)
    segs = _wrap_line(text, 12, spans)
    cols = cell_offsets(text)
    assert len(segs) > 1
    for cs, ce in segs:
        assert cols[ce] - cols[cs] <= 12


def test_every_row_fits_the_terminal_width():
    line = "日本語テスト alpha bravo 中文 charlie delta 한국어 echo foxtrot golf"
    screen, _doc, _ = make(line, w=24)
    cols = [cell_offsets(p) for p in screen._plain]
    for row in screen.rows:
        assert cols[row.line][row.col_end] - cols[row.line][row.col_start] <= 24


def test_wrapped_wide_word_still_hit_tests_on_its_own_row():
    line = "alpha bravo 日本語テスト charlie delta echo foxtrot golf hotel india"
    screen, doc, _ = make(line, w=20)
    cols = cell_offsets(line)
    for w in doc.words:
        for (row, col, length) in screen.word_cells(w.idx):
            r = screen.rows[row]
            row_cells = cols[r.col_end] - cols[r.col_start]
            # the cell run must sit inside its row and map back to the word
            assert col >= 0 and col + length <= row_cells
            screen._top_row = 0
            got = screen.hit_test(row, col)
            assert got is not None and doc.words[got].text == w.text


def test_ascii_wrapping_is_unchanged():
    text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
    spans = _word_spans(text)
    assert _wrap_line(text, 20, spans) == _wrap_line(text, 20, spans, cell_offsets(text))
    for cs, ce in _wrap_line(text, 20, spans):
        assert ce - cs <= 20
        assert not text[cs:ce].strip() or text[cs] != " "


# --------------------------------------------------------------------------- #
# status bar
# --------------------------------------------------------------------------- #


def test_status_bar_is_not_overrun_by_a_wide_message(nodraw):
    from readaloud.ui import Status

    screen, doc, win = make("hello world", h=6, w=40)
    st = Status(voice="af_heart", nchunks=3, message="検索: 日本語テスト found")
    screen.draw(doc, 0, current_word=0, current_chunk=0, status=st)
    assert text_width(win.row_text(screen.height - 1)) <= 40


def test_unicodedata_agrees_with_char_width():
    for cp in (0x4E00, 0x30C6, 0x1F680, 0x2705, 0xFF21):
        ch = chr(cp)
        assert char_width(ch) == 2, unicodedata.name(ch, hex(cp))
