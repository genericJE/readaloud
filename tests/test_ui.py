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


# --------------------------------------------------------------------------- #
# follow-mode scroll lead
#
# `top_for_word` used to perform the *smallest* scroll that kept the spoken word
# on screen, so the reading position sat pinned to the bottom margin and nothing
# of what was coming next was visible.  `lead` scrolls that many rows further,
# but only when the view has to scroll *down*, and never so far that the spoken
# word itself would be pushed off the top.
# --------------------------------------------------------------------------- #


def follow_doc(nlines, h=30, w=60):
    """A synthetic document of `nlines` short lines: display row == line index.

    Each line is ``"line <i> alpha bravo"`` — four words, well inside `w`, so
    nothing wraps and ``screen.rows[i].line == i``.  Word ``4 * i`` is the first
    word of line ``i``.
    """
    text = "\n".join(f"line {i} alpha bravo" for i in range(nlines))
    screen, doc, win = make(text, h=h, w=w)
    assert screen.nrows == nlines, "a line wrapped; widen the viewport"
    for i in range(nlines):
        assert screen.row_of_word(4 * i) == i
    return screen, doc, win


def word_on_row(row):
    """Global word index of the first word on display row `row`."""
    return 4 * row


def old_top_for_word(screen, widx, top_row, margin=2):
    """Verbatim copy of the pre-`lead` implementation, as the compat oracle."""
    row = screen.row_of_word(widx)
    if row is None:
        return screen.clamp_top(top_row)
    h = screen.body_height
    if h <= 0:
        return screen.clamp_top(top_row)
    m = min(margin, max(0, (h - 1) // 2))
    if row < top_row + m:
        return screen.clamp_top(row - m)
    if row > top_row + h - 1 - m:
        return screen.clamp_top(row - h + 1 + m)
    return screen.clamp_top(top_row)


# -- the compatibility contract: lead=0 is exactly the old behaviour ---------


@pytest.mark.parametrize("h", [1, 2, 3, 4, 5, 8, 24, 30, 60])
@pytest.mark.parametrize("margin", [0, 1, 2, 5])
def test_lead_zero_reproduces_the_old_behaviour_exactly(h, margin):
    """Exhaustive over every (word, top) pair on a 40-row document."""
    screen, _doc, _ = follow_doc(40, h=h)
    for row in range(40):
        widx = word_on_row(row)
        for top in range(-3, 44):
            assert screen.top_for_word(widx, top, margin, 0) == old_top_for_word(
                screen, widx, top, margin
            ), f"h={h} margin={margin} row={row} top={top}"


def test_lead_defaults_to_zero_and_the_old_call_still_works_positionally():
    """`top_for_word(w, top, FOLLOW_MARGIN)` is how app.py calls it today."""
    screen, _doc, _ = follow_doc(200, h=30)
    for row in range(0, 200, 7):
        widx = word_on_row(row)
        for top in (0, 5, 40, 120, 175):
            assert screen.top_for_word(widx, top, 2) == old_top_for_word(
                screen, widx, top, 2
            )
            assert screen.top_for_word(widx, top) == old_top_for_word(
                screen, widx, top, 2
            )
            assert screen.top_for_word(widx, top, 2) == screen.top_for_word(
                widx, top, 2, 0
            )


# -- what the lead is actually for ------------------------------------------


def test_lead_puts_the_spoken_word_near_the_top_third_on_a_tall_document():
    """30-row terminal, lead=20: the word lands on screen row 7 (1-based)."""
    screen, _doc, _ = follow_doc(400, h=30)
    body = screen.body_height
    assert body == 29
    top = 0
    # walk forward exactly as follow mode does, one word-row at a time
    seen, jumps = [], []
    for row in range(0, 120):
        new_top = screen.top_for_word(word_on_row(row), top, 2, 20)
        if new_top != top:
            jumps.append(row - new_top)
        top = new_top
        assert top <= row < top + body, f"row {row} off screen (top={top})"
        seen.append(row - top)
    # after the very first screenful the word never creeps to the bottom edge
    # and back a row at a time: it cycles between screen offset 6 and 26.
    assert seen[:27] == list(range(27)), "the first screenful needs no scroll"
    assert min(seen[27:]) == 6 and max(seen[27:]) == 26
    # every scroll parks the word 6 rows below the top (screen row 7, 1-based)
    assert jumps == [6] * len(jumps) and len(jumps) >= 4
    # ... and the view jumps in stable strides of 21 rows, not one at a time
    assert sorted(set(seen[27:])) == list(range(6, 27))
    # (well clear of the end of the document, where the clamp takes over)
    for row in (100, 137, 201, 300):
        got = screen.top_for_word(word_on_row(row), row - 27, 2, 20)
        assert row - got == 6, f"row {row} landed at screen offset {row - got}"
        assert body - (row - got) - 1 == 22


def test_lead_scrolls_twenty_rows_further_than_the_minimum():
    screen, _doc, _ = follow_doc(400, h=30)
    for row in (30, 55, 199):
        for top in (row - 27, row - 30, row - 40):
            plain = screen.top_for_word(word_on_row(row), top, 2, 0)
            led = screen.top_for_word(word_on_row(row), top, 2, 20)
            assert led == plain + 20, f"row={row} top={top}"
            assert led <= row - 2, "the spoken word must stay on screen"


def test_lead_on_a_short_document_clamps_to_max_top():
    """Only 25 rows of text on a 30-row terminal: the lead runs into the end."""
    screen, _doc, _ = follow_doc(25, h=30)
    assert screen.body_height == 29
    assert screen.max_top == 0  # 25 rows fit in 29: nothing to scroll
    for row in range(25):
        assert screen.top_for_word(word_on_row(row), 0, 2, 20) == 0

    # a genuinely short viewport instead: 12 body rows over 25 rows of document
    screen, _doc, _ = follow_doc(25, h=13)
    assert screen.body_height == 12 and screen.max_top == 13
    plain = screen.top_for_word(word_on_row(20), 0, 2, 0)
    led = screen.top_for_word(word_on_row(20), 0, 2, 20)
    assert plain == 11
    assert led == 13, "clamped to max_top, not 11 + 20"
    assert led == screen.max_top


def test_lead_clamps_at_the_end_of_the_document():
    screen, _doc, _ = follow_doc(60, h=20)
    body = screen.body_height
    assert (body, screen.max_top) == (19, 41)
    # last row of the document, reached from the very top
    led = screen.top_for_word(word_on_row(59), 0, 2, 20)
    assert led == screen.max_top == 41
    assert 59 - led == 18 <= body - 1, "the word is still on screen"
    # and it never exceeds max_top however big the lead is
    for lead in (0, 1, 20, 500):
        got = screen.top_for_word(word_on_row(59), 0, 2, lead)
        assert 0 <= got <= screen.max_top


# -- the safety cap: the word must never be scrolled off the top -------------


@pytest.mark.parametrize("lead", [29, 30, 100, 10_000])
def test_lead_larger_than_the_viewport_never_pushes_the_word_off_the_top(lead):
    screen, _doc, _ = follow_doc(400, h=30)
    for row in (40, 100, 250, 399):
        for top in (0, row - 27, row - 29, row - 50):
            got = screen.top_for_word(word_on_row(row), top, 2, lead)
            assert got <= row - 2, (
                f"lead={lead} row={row} top={top}: new top {got} scrolls the "
                f"spoken word off the top"
            )
            assert row - got < screen.body_height, "word below the viewport"


def test_a_huge_lead_saturates_at_the_top_margin():
    """Past the cap, more lead changes nothing: the word parks on the margin."""
    screen, _doc, _ = follow_doc(400, h=30)
    row = 200
    saturated = screen.top_for_word(word_on_row(row), 0, 2, 1000)
    assert saturated == row - 2
    assert screen.top_for_word(word_on_row(row), 0, 2, 10_000) == saturated


# -- degenerate viewports ---------------------------------------------------


def test_viewport_shorter_than_twice_the_margin():
    """body_height < margin*2: `m` collapses, and the lead must still be safe."""
    for h, expect_body in ((2, 1), (3, 2), (4, 3), (5, 4)):
        screen, _doc, _ = follow_doc(40, h=h)
        body = screen.body_height
        assert body == expect_body
        m = min(2, max(0, (body - 1) // 2))
        for row in (10, 25, 39):
            got = screen.top_for_word(word_on_row(row), 0, 2, 20)
            assert got <= row - m, f"h={h} row={row}: word pushed off the top"
            assert row - got < body, f"h={h} row={row}: word below the viewport"
            # there is no room to lead at all, so the cap always wins and the
            # word parks exactly on the (collapsed) top margin
            assert got == screen.clamp_top(row - m), f"h={h} row={row}"


def test_zero_height_body_ignores_the_lead():
    screen, _doc, _ = follow_doc(40, h=1)
    assert screen.body_height == 0
    for lead in (0, 20):
        assert screen.top_for_word(word_on_row(10), 7, 2, lead) == screen.clamp_top(7)


def test_unknown_word_ignores_the_lead():
    screen, _doc, _ = follow_doc(40, h=30)
    assert screen.row_of_word(99999) is None
    assert screen.top_for_word(99999, 5, 2, 20) == 5


# -- scrolling up is unchanged ----------------------------------------------


def test_scrolling_up_ignores_the_lead():
    screen, _doc, _ = follow_doc(400, h=30)
    for row in (0, 3, 50, 200):
        for top in (row + 3, row + 40, row + 100):
            if top > screen.max_top:
                continue
            for lead in (0, 20, 500):
                assert screen.top_for_word(word_on_row(row), top, 2, lead) == (
                    old_top_for_word(screen, word_on_row(row), top, 2)
                ), f"row={row} top={top} lead={lead}"


def test_word_already_comfortably_visible_ignores_the_lead():
    screen, _doc, _ = follow_doc(400, h=30)
    top = 100
    for row in range(top + 2, top + 27):  # inside both margins
        for lead in (0, 20, 500):
            assert screen.top_for_word(word_on_row(row), top, 2, lead) == top


# -- a document that fits entirely on screen --------------------------------


def test_lead_on_a_document_that_fits_entirely_on_screen():
    screen, _doc, _ = follow_doc(10, h=30)
    assert screen.nrows == 10 and screen.body_height == 29
    assert screen.max_top == 0
    for row in range(10):
        for top in (0, 5, -4):
            for lead in (0, 20, 500):
                assert screen.top_for_word(word_on_row(row), top, 2, lead) == 0


def test_document_exactly_filling_the_viewport_never_scrolls():
    screen, _doc, _ = follow_doc(29, h=30)
    assert screen.nrows == screen.body_height == 29
    assert screen.max_top == 0
    for row in range(29):
        assert screen.top_for_word(word_on_row(row), 0, 2, 20) == 0
