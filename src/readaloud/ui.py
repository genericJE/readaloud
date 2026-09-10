"""readaloud/ui.py — the curses view.

`Screen` owns the curses window and does three things:

* **layout** — wrap the document's styled logical lines to the terminal width,
  word-wrapping on whitespace and never splitting a `Word` across two rows,
  building a ``row -> (line, col_start)`` map and a ``word -> (row, col)`` map;
* **draw** — repaint only the screen rows whose content or highlight actually
  changed (the word highlight moves several times a second, so a full repaint
  every frame would both flicker and burn CPU);
* **hit-test** — turn a mouse click's screen cell back into a global word index.

Colour handling follows what was measured on this ncurses (6.0.20150808):
`curses.A_COLOR` is only 8 bits wide, so despite `COLOR_PAIRS == 32767` only
pairs 1-255 are addressable; `curses.init_pair` raises `ValueError` (not
`curses.error`) for an out-of-range colour; and `A_ITALIC` is bit 31, so any
composed attribute containing it overflows `curses.pair_number()`.

Its only intra-package import is `readaloud.keys` (for the SGR mouse strings
and `read_event`).  It deliberately does *not* import `readaloud.ansi` or
`readaloud.document`: it reads the documented attributes off whatever objects
it is handed (`Run.text`, `Run.style`, `Style.fg`, `Word.line/start/end`,
`Chunk.words` …), so the view can be exercised on its own and cannot be broken
by a change in how those modules construct their objects.
"""

from __future__ import annotations

import bisect
import curses
import os
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Sequence

from readaloud.keys import SGR_MOUSE_OFF, SGR_MOUSE_ON, read_event as _read_event

if TYPE_CHECKING:  # pragma: no cover - typing only
    from readaloud.document import Document

__all__ = [
    "Row",
    "Theme",
    "Status",
    "Screen",
    "ColorAllocator",
    "xterm_rgb",
    "degrade",
    "pair_of",
    "HAVE_A_ITALIC",
    "A_ITALIC",
    "char_width",
    "text_width",
    "cell_offsets",
    "read_stdin_text",
    "reopen_tty",
    "screen_session",
]


# --------------------------------------------------------------------------
# display width
# --------------------------------------------------------------------------

# A character index is NOT a terminal column: an East-Asian-wide or emoji
# glyph occupies two cells.  Everything that talks to the terminal (paint
# positions, wrap decisions, hit-testing a mouse cell) has to go through these
# helpers, or the highlight lands on the wrong characters and a click resolves
# to a word one cell per wide glyph to the left of the one the user pointed at.
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


def _truncate_cells(text: str, cells: int) -> str:
    """Longest prefix of `text` that fits in `cells` columns."""
    if cells <= 0:
        return ""
    n = 0
    for i, ch in enumerate(text):
        w = char_width(ch)
        if n + w > cells:
            return text[:i]
        n += w
    return text


def _pad_cells(text: str, cells: int) -> str:
    """`text` truncated to `cells` columns and space-padded out to them."""
    text = _truncate_cells(text, cells)
    return text + " " * (cells - text_width(text))


# --------------------------------------------------------------------------
# attributes and colour
# --------------------------------------------------------------------------

# A_ITALIC is present on this build (0x80000000) and really emits ESC[3m.
# Where it is missing, underline is the least-bad stand-in: mdcat never emits
# SGR 4, so the underline channel is otherwise unused.
HAVE_A_ITALIC: bool = hasattr(curses, "A_ITALIC")
A_ITALIC: int = getattr(curses, "A_ITALIC", 0) or curses.A_UNDERLINE

# xterm's default RGB for palette entries 0-15.  0-7 are exactly
# curses.COLOR_BLACK..COLOR_WHITE, so a 0-15 index *is* a curses colour number.
_ANSI16_RGB: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)
_CUBE = (0, 95, 135, 175, 215, 255)
_GREYS_8 = (0, 7)
_GREYS_16 = (0, 8, 7, 15)


def _dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return 3 * dr * dr + 6 * dg * dg + db * db  # BT.601-ish weighting


def _lum(rgb: tuple[int, int, int]) -> float:
    return (3 * rgb[0] + 6 * rgb[1] + rgb[2]) / 10.0


def xterm_rgb(idx: int) -> tuple[int, int, int]:
    """RGB for a 0-255 xterm palette index."""
    idx = max(0, min(255, int(idx)))
    if idx < 16:
        return _ANSI16_RGB[idx]
    if idx < 232:
        n = idx - 16
        return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])
    v = 8 + 10 * (idx - 232)
    return (v, v, v)


def degrade(idx: int, ncolors: int, *, is_fg: bool = False) -> int:
    """Map a 0-255 palette index down to the nearest colour the terminal has.

    Achromatic sources are matched against the achromatic basics by luminance:
    a plain nearest-RGB match sends mid grey to *yellow* on an 8-colour
    terminal.  `is_fg` additionally keeps dark foregrounds off COLOR_BLACK,
    where they would vanish on a dark background.
    """
    if ncolors <= 0:
        return -1
    if idx < ncolors:
        return idx
    basics = min(ncolors, 16)  # 88-colour terminals: clamp to the basics
    target = xterm_rgb(idx)
    if max(target) - min(target) <= 24:
        greys = _GREYS_16 if basics > 8 else _GREYS_8
        out = min(greys, key=lambda i: abs(_lum(_ANSI16_RGB[i]) - _lum(target)))
    else:
        out = min(range(basics), key=lambda i: _dist(_ANSI16_RGB[i], target))
    if is_fg and basics <= 8 and out == curses.COLOR_BLACK:
        out = curses.COLOR_WHITE
    return out


def pair_of(attr: int) -> int:
    """Overflow-safe `curses.pair_number()` for an *attribute*.

    `A_ITALIC` is bit 31, so `curses.pair_number(attr)` on a composed attribute
    raises ``OverflowError: Python int too large to convert to C int``; masking
    with `A_COLOR` first fixes that.

    Careful when reading the screen back with `inch()`: `A_CHARTEXT` is only
    0xff here, yet `inch()` returns the whole codepoint, so a non-ASCII cell
    leaks its high bits straight into the A_COLOR field — mdcat's ``═``
    (U+2550) reads back as "pair 37", ``•`` (U+2022) as "pair 32".  The two are
    genuinely indistinguishable in one chtype, so only trust `pair_of` on an
    `inch()` value when that cell is known to hold ASCII.  Attribute flags
    (A_REVERSE, A_BOLD …) all live above bit 15 and are unaffected.
    """
    return curses.pair_number(attr & curses.A_COLOR)


class ColorAllocator:
    """Lazily allocates curses colour pairs for (fg, bg) palette indices.

    `fg`/`bg` are 0-255 palette indices or ``None`` for the terminal default.
    Construct after `curses.initscr()`; the constructor calls `start_color()`
    and `use_default_colors()` itself and never raises.
    """

    def __init__(self, *, first_pair: int = 1, max_pairs: int | None = None) -> None:
        self.has_color = False
        self.colors = 0
        self.pairs = 0  # exclusive ceiling on usable pair numbers
        self.default_ok = False
        self.exhausted = False
        self._cache: dict[tuple[int | None, int | None], int] = {}
        self._by_basic: dict[tuple[int, int], int] = {}
        self._next = max(1, first_pair)

        try:
            if not curses.has_colors():
                return
            curses.start_color()
        except curses.error:
            return
        self.has_color = True
        self.colors = getattr(curses, "COLORS", 0) or 0

        try:
            curses.use_default_colors()
            self.default_ok = True
        except curses.error:
            self.default_ok = False

        # COLOR_PAIRS lies about what is *addressable*: color_pair(n) packs n
        # into A_COLOR (0xff00 here), so pairs above 255 alias back onto low
        # ones with no error at all.
        raw = getattr(curses, "COLOR_PAIRS", 0) or 0
        addressable = (curses.A_COLOR >> 8) + 1
        limit = min(raw, addressable)
        if max_pairs is not None:
            limit = min(limit, max_pairs)
        self.pairs = max(0, limit)

    # -- internals --------------------------------------------------------

    def _resolve(self, c: int | None, is_fg: bool) -> int:
        return -1 if c is None else degrade(int(c), self.colors, is_fg=is_fg)

    def _basic(self, c: int | None) -> int:
        return -1 if c is None else degrade(int(c), min(self.colors, 16) or 16)

    # -- public -----------------------------------------------------------

    def pair_attr(self, fg: int | None, bg: int | None) -> int:
        """The curses attribute carrying the colour pair for (fg, bg)."""
        if not self.has_color:
            return 0
        if fg is None and bg is None:
            return 0  # pair 0 is the terminal's own default
        key = (fg, bg)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        cfg, cbg = self._resolve(fg, True), self._resolve(bg, False)
        if cfg == -1 and cbg == -1:
            self._cache[key] = 0
            return 0
        if not self.default_ok:
            if cfg == -1:
                cfg = curses.COLOR_WHITE
            if cbg == -1:
                cbg = curses.COLOR_BLACK

        basic = (self._basic(fg), self._basic(bg))
        if self._next >= self.pairs:
            self.exhausted = True
            n = self._by_basic.get(basic, 0)
            attr = curses.color_pair(n) if n else 0
            self._cache[key] = attr
            return attr

        n = self._next
        try:
            # init_pair raises ValueError (NOT curses.error) past COLORS-1.
            curses.init_pair(n, cfg, cbg)
        except (curses.error, ValueError, OverflowError):
            self.exhausted = True
            self._cache[key] = 0
            return 0
        self._next = n + 1
        attr = curses.color_pair(n)
        self._cache[key] = attr
        self._by_basic.setdefault(basic, n)
        return attr

    def attr(self, style: Any, *, bg_override: int | None = None) -> int:
        """Full curses attribute for an `ansi.Style`.

        `bg_override` supplies a background for runs that do not set one
        themselves — that is how the current chunk gets its subtle wash without
        stamping over a run's own colours.
        """
        bg = getattr(style, "bg", None)
        if bg is None and bg_override is not None:
            bg = bg_override
        a = self.pair_attr(getattr(style, "fg", None), bg)
        if getattr(style, "bold", False):
            a |= curses.A_BOLD
        if getattr(style, "dim", False):
            a |= curses.A_DIM
        if getattr(style, "italic", False):
            a |= A_ITALIC
        if getattr(style, "underline", False):
            a |= curses.A_UNDERLINE
        if getattr(style, "reverse", False):
            a |= curses.A_REVERSE
        return a


# --------------------------------------------------------------------------
# layout types
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    """One display row: a slice ``[col_start, col_end)`` of logical `line`."""

    line: int
    col_start: int
    col_end: int

    @property
    def width(self) -> int:
        return self.col_end - self.col_start


@dataclass
class Theme:
    """Everything about how the view looks, in one place."""

    #: attribute OR-ed onto the word currently being spoken
    word_attr: int = curses.A_REVERSE
    #: background palette index washed over the current chunk (256-colour
    #: terminals); ``None`` disables the chunk wash entirely
    chunk_bg: int | None = 236
    #: fallback for the current chunk on terminals with < 256 colours
    chunk_attr_lowcolor: int = curses.A_BOLD
    #: attribute for the status bar
    status_attr: int = curses.A_REVERSE
    #: attribute for the search / message prompt line
    prompt_attr: int = curses.A_NORMAL
    #: attribute for search-match highlighting
    match_attr: int = curses.A_UNDERLINE | curses.A_BOLD


@dataclass
class Status:
    """What the status bar shows.  `Screen.draw` also accepts a plain string."""

    voice: str = ""
    speed: float = 1.0
    playing: bool = False
    loading: bool = False
    chunk: int = 0  # 0-based
    nchunks: int = 0
    follow: bool = True
    message: str = ""
    #: when set, the whole bar becomes this input line (``/pattern``) and a
    #: cursor is parked at its end
    prompt: str | None = None


# --------------------------------------------------------------------------
# terminal bootstrap
# --------------------------------------------------------------------------


def read_stdin_text(encoding: str = "utf-8") -> str:
    """Drain piped stdin.  Returns ``''`` when stdin is a tty (nothing piped)."""
    if os.isatty(0):
        return ""
    data = sys.stdin.buffer.read()
    return data.decode(encoding, errors="replace")


def reopen_tty() -> None:
    """Point fd 0 (and fd 1, if redirected) at the controlling terminal.

    Must run *after* stdin has been drained and *before* `curses.initscr()`.
    Skipping it fails silently: `initscr()` only needs stdout, so it succeeds
    and every `getch()` then returns -1 forever.

    Raises `OSError` (ENXIO, "Device not configured") when the process has no
    controlling terminal — callers should turn that into a readable message.
    """
    fd = os.open("/dev/tty", os.O_RDWR)
    os.dup2(fd, 0)
    if not os.isatty(1):
        os.dup2(fd, 1)
    if fd > 2:
        os.close(fd)
    # sys.stdin still wraps the now-exhausted pipe; rebind both so later
    # Python-level writes go to the terminal.
    sys.stdin = open(0, "r", closefd=False)
    sys.stdout = open(1, "w", closefd=False)


@contextmanager
def screen_session(
    theme: Theme | None = None, *, mouse: bool = True, escdelay_ms: int = 25
) -> Iterator["Screen"]:
    """initscr + sane modes + mouse, yielding a `Screen`; always restores the tty."""
    # The default ESCDELAY is 1000 ms, which makes the first Escape-prefixed
    # key look like a one-second freeze.
    curses.set_escdelay(escdelay_ms)
    stdscr = curses.initscr()
    mouse_on = False
    try:
        curses.noecho()
        curses.cbreak()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        if mouse:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            try:
                curses.mouseinterval(0)  # no 200 ms click-synthesis delay
            except curses.error:
                pass
            # ncurses only ever writes \e[?1000h, and its mouse-v1 encoding
            # cannot express wheel-down; request SGR (1006) ourselves.
            sys.stdout.write(SGR_MOUSE_ON)
            sys.stdout.flush()
            mouse_on = True
        yield Screen(stdscr, theme)
    finally:
        if mouse_on:
            try:
                sys.stdout.write(SGR_MOUSE_OFF)
                sys.stdout.flush()
            except Exception:
                pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            stdscr.keypad(False)
        except curses.error:
            pass
        curses.echo()
        curses.nocbreak()
        curses.endwin()


# --------------------------------------------------------------------------
# the view
# --------------------------------------------------------------------------

class Screen:
    """The curses view over a `Document`."""

    def __init__(self, stdscr, theme: Theme | None = None) -> None:
        self.stdscr = stdscr
        self.theme = theme or Theme()
        self.colors = ColorAllocator()

        h, w = stdscr.getmaxyx()
        self._h = h
        self._w = w

        self._doc: Any = None
        self._layout_width = 0
        self._rows: list[Row] = []
        self._plain: list[str] = []
        self._line_runs: list[list[tuple[int, int, Any]]] = []
        self._line_run_starts: list[list[int]] = []
        #: per logical line, ``cell_offsets(plain[line])`` — char index -> column
        self._line_cols: list[list[int]] = []
        self._line_first_row: list[int] = []
        self._row_words: list[list[tuple[int, int, int]]] = []
        self._word_rc: dict[int, tuple[int, int]] = {}
        self._word_cells: dict[int, list[tuple[int, int, int]]] = {}

        self._top_row = 0
        self._painted: list[Any] = []
        self._painted_status: Any = None
        self._attr_cache: dict[tuple[int, int | None], int] = {}
        self._cursor_visible = False

        # counters, for tests and for proving the partial-repaint behaviour
        self.rows_painted = 0
        self.rows_painted_last = 0
        self.status_painted_last = 0
        self.full_repaints = 0
        self.frames = 0

    # -- geometry ---------------------------------------------------------

    @property
    def height(self) -> int:
        """Terminal height in rows, including the status bar."""
        return self._h

    @property
    def width(self) -> int:
        """Terminal width in columns."""
        return self._w

    @property
    def body_height(self) -> int:
        """Rows available for document text (everything above the status bar)."""
        return max(0, self._h - 1)

    @property
    def rows(self) -> list[Row]:
        """The laid-out display rows (``row -> (line, col_start, col_end)``)."""
        return self._rows

    @property
    def nrows(self) -> int:
        """Number of display rows in the whole document."""
        return len(self._rows)

    #: alias, for callers that read ``screen.total_rows`` as a count
    @property
    def total_rows(self) -> int:
        return len(self._rows)

    @property
    def max_top(self) -> int:
        """Largest legal `top_row`."""
        return max(0, len(self._rows) - self.body_height)

    @property
    def top_row(self) -> int:
        """The `top_row` used by the most recent `draw`."""
        return self._top_row

    def clamp_top(self, top_row: int) -> int:
        """Clamp a candidate top row into ``[0, max_top]``."""
        if top_row < 0:
            return 0
        m = self.max_top
        return m if top_row > m else top_row

    # -- layout -----------------------------------------------------------

    def layout(self, doc: "Document | None", width: int | None = None) -> list[Row]:
        """Wrap `doc` to `width` columns and rebuild every map.

        Word-wraps on whitespace, never splits a `Word` across two rows (a word
        wider than the whole terminal is the only exception — it is hard-broken
        because nothing else is possible), and builds both the
        ``row -> (line, col_start)`` map (`rows`) and the ``word -> (row, col)``
        map (`word_position`).
        """
        if width is None:
            width = self._w
        width = max(1, int(width))
        self._doc = doc
        self._layout_width = width

        lines: Sequence[Sequence[Any]] = getattr(doc, "lines", None) or []
        words: Sequence[Any] = getattr(doc, "words", None) or []

        nlines = len(lines)
        plain: list[str] = []
        line_runs: list[list[tuple[int, int, Any]]] = []
        line_run_starts: list[list[int]] = []
        line_cols: list[list[int]] = []
        for runs in lines:
            spans: list[tuple[int, int, Any]] = []
            starts: list[int] = []
            pos = 0
            buf: list[str] = []
            for run in runs:
                text = _sanitise(getattr(run, "text", "") or "")
                if not text:
                    continue
                buf.append(text)
                spans.append((pos, pos + len(text), getattr(run, "style", None)))
                starts.append(pos)
                pos += len(text)
            joined = "".join(buf)
            plain.append(joined)
            line_runs.append(spans)
            line_run_starts.append(starts)
            line_cols.append(cell_offsets(joined))

        # Words grouped by line, sorted by start offset.  The position in
        # `doc.words` is the authoritative global index (`Word.idx` is
        # documented to be exactly that), so carry it alongside.
        by_line: list[list[tuple[int, Any]]] = [[] for _ in range(nlines)]
        for gi, w in enumerate(words):
            li = getattr(w, "line", -1)
            if 0 <= li < nlines:
                by_line[li].append((gi, w))
        for lst in by_line:
            lst.sort(key=lambda pair: getattr(pair[1], "start", 0))

        rows: list[Row] = []
        line_first_row: list[int] = []
        row_words: list[list[tuple[int, int, int]]] = []
        word_rc: dict[int, tuple[int, int]] = {}
        word_cells: dict[int, list[tuple[int, int, int]]] = {}

        for li in range(nlines):
            line_first_row.append(len(rows))
            text = plain[li]
            cols = line_cols[li]
            nchars = len(text)
            lwords = [
                (gi, max(0, int(getattr(w, "start", 0))), min(nchars, int(getattr(w, "end", 0))))
                for gi, w in by_line[li]
            ]
            lwords = [t for t in lwords if t[1] < t[2]]
            spans = [(s, e) for _gi, s, e in lwords]
            segments = _wrap_line(text, width, spans, cols)

            wi = 0
            for cs, ce in segments:
                r = len(rows)
                rows.append(Row(li, cs, ce))
                here: list[tuple[int, int, int]] = []
                # words are sorted, so walk them alongside the rows
                k = wi
                while k < len(lwords):
                    idx, s, e = lwords[k]
                    if s >= ce:
                        break
                    if e > cs and s < ce:
                        a, b = max(s, cs), min(e, ce)
                        here.append((a, b, idx))
                        # (row, column, length) are all in *cells*: a caller
                        # positioning a highlight needs terminal columns, not
                        # character offsets.
                        x0 = cols[a] - cols[cs]
                        word_cells.setdefault(idx, []).append(
                            (r, x0, cols[b] - cols[a])
                        )
                        if idx not in word_rc:
                            word_rc[idx] = (r, x0)
                    if e <= ce:
                        k += 1
                        wi = k
                    else:
                        break
                row_words.append(here)

        self._plain = plain
        self._line_runs = line_runs
        self._line_run_starts = line_run_starts
        self._line_cols = line_cols
        self._rows = rows
        self._line_first_row = line_first_row
        self._row_words = row_words
        self._word_rc = word_rc
        self._word_cells = word_cells
        self.invalidate()
        return rows

    # -- maps -------------------------------------------------------------

    def row_line_col(self, row: int) -> tuple[int, int] | None:
        """``row -> (line, col_start)``, or ``None`` past the end."""
        if 0 <= row < len(self._rows):
            r = self._rows[row]
            return (r.line, r.col_start)
        return None

    def first_row_of_line(self, line: int) -> int:
        """First display row of a logical line (clamped)."""
        if not self._line_first_row:
            return 0
        if line < 0:
            return 0
        if line >= len(self._line_first_row):
            return max(0, len(self._rows) - 1)
        return self._line_first_row[line]

    def row_for(self, line: int, col: int = 0) -> int:
        """``(line, col) -> row``.  The inverse of `row_line_col`.

        Use it to keep the viewport anchored across a resize::

            anchor = screen.row_line_col(top_row)      # before
            screen.handle_resize(doc)                  # re-wraps
            top_row = screen.row_for(*anchor)          # after
        """
        if not self._rows:
            return 0
        n = len(self._line_first_row)
        if line < 0:
            return 0
        if line >= n:
            return len(self._rows) - 1
        lo = self._line_first_row[line]
        hi = self._line_first_row[line + 1] if line + 1 < n else len(self._rows)
        best = lo
        for r in range(lo, hi):
            row = self._rows[r]
            if row.col_start > col:
                break
            best = r
            if col < row.col_end:
                break
        return best

    def word_position(self, widx: int | None) -> tuple[int, int] | None:
        """``word -> (row, col)``, or ``None`` if the word is not laid out."""
        if widx is None:
            return None
        return self._word_rc.get(int(widx))

    def row_of_word(self, widx: int | None) -> int | None:
        rc = self.word_position(widx)
        return None if rc is None else rc[0]

    def word_cells(self, widx: int) -> list[tuple[int, int, int]]:
        """``[(row, col, length)]`` — every cell run a word occupies."""
        return self._word_cells.get(int(widx), [])

    # -- hit testing ------------------------------------------------------

    def hit_test(self, mouse_y: int, mouse_x: int) -> int | None:
        """Screen cell -> global word index, or ``None`` (blank cell / status bar)."""
        loc = self.hit_test_cell(mouse_y, mouse_x)
        if loc is None:
            return None
        row, col = loc
        for a, b, widx in self._row_words[row]:
            if a <= col < b:
                return widx
        return None

    def hit_test_cell(self, mouse_y: int, mouse_x: int) -> tuple[int, int] | None:
        """Screen cell -> ``(display row, column within the logical line)``.

        `mouse_x` is a terminal *cell*; the column returned is a *character*
        offset into the logical line.  The two only coincide on lines made of
        single-width characters, so translate through the line's cell table —
        clicking the second half of a wide glyph lands on that glyph.
        """
        if mouse_y < 0 or mouse_x < 0 or mouse_y >= self.body_height:
            return None
        row = self._top_row + mouse_y
        if row >= len(self._rows):
            return None
        r = self._rows[row]
        cols = self._line_cols[r.line] if r.line < len(self._line_cols) else None
        if not cols:
            col = r.col_start + mouse_x
            return None if col >= r.col_end else (row, col)
        target = cols[r.col_start] + mouse_x
        if target >= cols[r.col_end]:
            return None
        col = bisect.bisect_right(cols, target, r.col_start, r.col_end) - 1
        if col < r.col_start or col >= r.col_end:
            return None
        return (row, col)

    def hit_test_line_col(self, mouse_y: int, mouse_x: int) -> tuple[int, int] | None:
        """Screen cell -> ``(logical line, column)``, per the contract."""
        loc = self.hit_test_cell(mouse_y, mouse_x)
        if loc is None:
            return None
        row, col = loc
        return (self._rows[row].line, col)

    # -- scrolling helpers ------------------------------------------------

    def top_for_word(
        self, widx: int, top_row: int, margin: int = 2, lead: int = 0
    ) -> int:
        """Scroll `widx` into view, keeping `margin` rows spare at both edges.

        With ``lead == 0`` this is the *smallest* scroll that brings the word
        back on screen, which pins the reading position to the bottom margin and
        hides everything that is about to be spoken.

        A positive `lead` applies only when the view has to scroll **down** (the
        word has reached the bottom margin): the new top goes `lead` rows past
        that minimum, so the upcoming text lands around the middle of the
        viewport and the view advances in stable jumps rather than creeping one
        row at a time.  The `min` caps the lead so the spoken word can never be
        pushed off the top: the new top always stays ``<= row - m``.

        Scrolling *up*, and the case where the word is already comfortably
        visible, ignore `lead` entirely.
        """
        row = self.row_of_word(widx)
        if row is None:
            return self.clamp_top(top_row)
        h = self.body_height
        if h <= 0:
            return self.clamp_top(top_row)
        m = min(margin, max(0, (h - 1) // 2))
        if row < top_row + m:
            return self.clamp_top(row - m)
        if row > top_row + h - 1 - m:
            return self.clamp_top(min(row - m, row - h + 1 + m + lead))
        return self.clamp_top(top_row)

    def follow_top_for_word(self, widx: int, margin: int = 2, lead: int = 0) -> int:
        """Where follow mode would park the view for `widx`, unconditionally.

        `top_for_word` is a *reaction*: if the word is already on screen it
        leaves the view alone.  That is right for the auto-scroll tick and wrong
        for the `c` key, which is a deliberate "put me back where the reading
        is" and must reposition even when the word happens to be visible.

        So this always takes the scroll-down branch, giving the same placement
        the next auto-scroll would produce -- the word `lead` rows below the top
        with the upcoming text underneath -- and pressing `c` never causes a
        second jump a moment later.
        """
        row = self.row_of_word(widx)
        if row is None:
            return self.clamp_top(self._top_row)
        h = self.body_height
        if h <= 0:
            return self.clamp_top(self._top_row)
        m = min(margin, max(0, (h - 1) // 2))
        return self.clamp_top(min(row - m, row - h + 1 + m + lead))

    def follow_top_for_row(self, row: int, margin: int = 2, lead: int = 0) -> int:
        """`follow_top_for_word` for a bare display row (no word is highlighted yet)."""
        h = self.body_height
        if h <= 0:
            return self.clamp_top(self._top_row)
        m = min(margin, max(0, (h - 1) // 2))
        return self.clamp_top(min(row - m, row - h + 1 + m + lead))

    def center_on_word(self, widx: int) -> int:
        """Top row that puts `widx` in the middle of the viewport."""
        row = self.row_of_word(widx)
        if row is None:
            return self._top_row
        return self.clamp_top(row - self.body_height // 2)

    def center_on_row(self, row: int) -> int:
        return self.clamp_top(row - self.body_height // 2)

    # -- painting ---------------------------------------------------------

    def invalidate(self) -> None:
        """Force the next `draw` to repaint every row."""
        self._painted = [_NEVER] * max(0, self.body_height)
        self._painted_status = _NEVER

    def handle_resize(self, doc: "Document | None" = None) -> bool:
        """Re-read the terminal size and re-lay-out if the width changed.

        `curses.LINES`/`curses.COLS` stay stale after `KEY_RESIZE`; only
        `getmaxyx()` is updated, so refresh the module globals too.
        """
        try:
            curses.update_lines_cols()
        except (curses.error, AttributeError):
            pass
        h, w = self.stdscr.getmaxyx()
        changed_size = (h, w) != (self._h, self._w)
        self._h, self._w = h, w
        target = doc if doc is not None else self._doc
        if w != self._layout_width:
            self.layout(target, w)
        else:
            self.invalidate()
        try:
            self.stdscr.clearok(True)
        except curses.error:
            pass
        return changed_size

    def draw(
        self,
        doc: "Document | None",
        top_row: int,
        current_word: int | None = None,
        current_chunk: Any = None,
        status: Status | str | None = None,
        matches: Sequence[tuple[int, int, int]] | None = None,
    ) -> int:
        """Paint the frame; returns the number of screen rows actually repainted.

        `current_chunk` may be a chunk index or a `Chunk` object.  `matches` is
        an optional list of ``(line, start, end)`` search hits to underline.
        """
        h, w = self.stdscr.getmaxyx()
        if (h, w) != (self._h, self._w):
            self._h, self._w = h, w
            if w != self._layout_width:
                self.layout(doc if doc is not None else self._doc, w)
            else:
                self.invalidate()
        if doc is not None and doc is not self._doc:
            self.layout(doc, w)

        body = self.body_height
        if len(self._painted) != body:
            self._painted = [_NEVER] * body

        top_row = self.clamp_top(top_row)
        self._top_row = top_row

        chunk_region = self._chunk_region(doc, current_chunk)
        word_line, word_span = self._word_span(doc, current_word)

        painted = 0
        for y in range(body):
            r = top_row + y
            if r >= len(self._rows):
                sig: Any = None
            else:
                row = self._rows[r]
                wseg = (
                    _clip(word_span, row.col_start, row.col_end)
                    if word_line == row.line
                    else None
                )
                cseg = self._chunk_seg(row, chunk_region)
                mseg = self._match_segs(row, matches)
                sig = (r, wseg, cseg, mseg)
            if sig == self._painted[y]:
                continue
            self._paint_row(y, sig)
            self._painted[y] = sig
            painted += 1

        self.status_painted_last = self._paint_status(status, top_row)

        self.stdscr.noutrefresh()
        curses.doupdate()

        self.frames += 1
        self.rows_painted += painted
        self.rows_painted_last = painted
        if painted >= body and body:
            self.full_repaints += 1
        return painted

    # -- painting internals ----------------------------------------------

    def _paint_row(self, y: int, sig: Any) -> None:
        win = self.stdscr
        try:
            win.move(y, 0)
            win.clrtoeol()
        except curses.error:
            return
        if sig is None:
            return
        r, wseg, cseg, mseg = sig
        row = self._rows[r]
        line = row.line
        text = self._plain[line]
        cols = self._line_cols[line]
        cs = row.col_start
        # clip on *cells*, not characters: the row may hold fewer characters
        # than columns when it contains wide glyphs.
        ce = row.col_end
        if cols[ce] - cols[cs] > self._w:
            ce = bisect.bisect_right(cols, cols[cs] + self._w, cs, ce + 1) - 1
        if ce <= cs:
            return

        chunk_bg = self._chunk_bg()
        # chunk_bg None because the *terminal* cannot manage it -> fall back to
        # an attribute; None because the *theme* switched it off -> no wash.
        chunk_extra = (
            self.theme.chunk_attr_lowcolor
            if chunk_bg is None and self.theme.chunk_bg is not None
            else 0
        )
        bounds = {cs, ce}
        for s, e, _style in self._line_runs[line]:
            if e <= cs or s >= ce:
                continue
            bounds.add(max(s, cs))
            bounds.add(min(e, ce))
        for seg in (wseg, cseg):
            if seg:
                bounds.add(seg[0])
                bounds.add(seg[1])
        for seg in mseg or ():
            bounds.add(seg[0])
            bounds.add(seg[1])
        pts = sorted(bounds)

        starts = self._line_run_starts[line]
        runs = self._line_runs[line]
        for a, b in zip(pts, pts[1:]):
            if b <= a:
                continue
            style = None
            if starts:
                i = bisect.bisect_right(starts, a) - 1
                if 0 <= i < len(runs) and runs[i][0] <= a < runs[i][1]:
                    style = runs[i][2]
            in_chunk = bool(cseg and cseg[0] <= a < cseg[1])
            attr = self._attr(style, chunk_bg if in_chunk else None)
            if in_chunk:
                attr |= chunk_extra
            if mseg:
                for m0, m1 in mseg:
                    if m0 <= a < m1:
                        attr |= self.theme.match_attr
                        break
            if wseg and wseg[0] <= a < wseg[1]:
                attr |= self.theme.word_attr
            self._put(y, cols[a] - cols[cs], text[a:b], attr)

    def _paint_status(self, status: Status | str | None, top_row: int) -> int:
        if self._h <= 0:
            return 0
        y = self._h - 1
        text, attr, cursor_x = self._status_line(status, top_row)
        sig = (text, attr, cursor_x, self._w)
        drawn = 0
        if sig != self._painted_status:
            try:
                self.stdscr.move(y, 0)
                self.stdscr.clrtoeol()
                self._put(y, 0, _pad_cells(text, self._w), attr)
                self._painted_status = sig
                drawn = 1
            except curses.error:
                pass
        # The cursor must be re-parked every frame: painting the body rows
        # leaves it wherever the last addstr ended.
        want_cursor = cursor_x is not None
        if want_cursor != self._cursor_visible:
            try:
                curses.curs_set(1 if want_cursor else 0)
                self._cursor_visible = want_cursor
            except curses.error:
                pass
        try:
            self.stdscr.move(y, min(cursor_x or 0, max(0, self._w - 1)))
        except curses.error:
            pass
        return drawn

    def _status_line(
        self, status: Status | str | None, top_row: int
    ) -> tuple[str, int, int | None]:
        if status is None:
            status = Status()
        if isinstance(status, str):
            return (status, self.theme.status_attr, None)

        if status.prompt is not None:
            return (status.prompt, self.theme.prompt_attr, text_width(status.prompt))

        if status.loading:
            state = "LOADING"
        elif status.playing:
            state = "PLAY   "
        else:
            state = "PAUSE  "
        bits = [state]
        if status.voice:
            bits.append(status.voice)
        bits.append(f"{status.speed:.2f}x")
        if status.nchunks:
            bits.append(f"chunk {min(status.chunk + 1, status.nchunks)}/{status.nchunks}")
        else:
            bits.append("chunk -/-")
        bits.append("follow " + ("on" if status.follow else "off"))
        left = "  ".join(bits)
        if status.message:
            left += "  |  " + status.message

        total = len(self._rows)
        if total <= self.body_height:
            pos = "ALL"
        elif top_row >= self.max_top:
            pos = "END"
        else:
            pos = f"{int(round(100.0 * top_row / max(1, self.max_top)))}%"
        right = f"{pos} "
        pad = self._w - text_width(left) - text_width(right)
        if pad < 1:
            line = _truncate_cells(left + " " + right, self._w)
        else:
            line = left + " " * pad + right
        return (line, self.theme.status_attr, None)

    def _put(self, y: int, x: int, text: str, attr: int) -> None:
        """Write `text` with `attr` at *cell* column `x`."""
        if not text or y < 0 or y >= self._h or x < 0 or x >= self._w:
            return
        # Truncate on columns: a wide glyph that only half fits would wrap and
        # shove the rest of the row along by a cell.
        text = _truncate_cells(text, self._w - x)
        if not text:
            return
        win = self.stdscr
        if y == self._h - 1 and x + text_width(text) >= self._w:
            head, tail = text[:-1], text[-1]
            if head:
                try:
                    win.addstr(y, x, head, attr)
                except curses.error:
                    pass
            try:
                win.insstr(y, x + text_width(head), tail, attr)
            except curses.error:
                pass
            return
        try:
            win.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _chunk_bg(self) -> int | None:
        bg = self.theme.chunk_bg
        if bg is None or not self.colors.has_color or self.colors.colors < 256:
            return None
        return bg

    def _attr(self, style: Any, chunk_bg: int | None) -> int:
        key = (id(style), chunk_bg)
        hit = self._attr_cache.get(key)
        if hit is not None:
            return hit
        if style is None:
            a = 0 if chunk_bg is None else self.colors.pair_attr(None, chunk_bg)
        else:
            a = self.colors.attr(style, bg_override=chunk_bg)
        self._attr_cache[key] = a
        return a

    # -- highlight geometry ----------------------------------------------

    def _word_span(
        self, doc: Any, widx: int | None
    ) -> tuple[int, tuple[int, int] | None]:
        if widx is None or doc is None:
            return (-1, None)
        words = getattr(doc, "words", None) or ()
        if not (0 <= widx < len(words)):
            return (-1, None)
        w = words[widx]
        return (int(getattr(w, "line", -1)), (int(w.start), int(w.end)))

    def _chunk_region(
        self, doc: Any, chunk: Any
    ) -> tuple[int, int, int, int] | None:
        if chunk is None or doc is None:
            return None
        if isinstance(chunk, int):
            chunks = getattr(doc, "chunks", None) or ()
            if not (0 <= chunk < len(chunks)):
                return None
            chunk = chunks[chunk]
        words = getattr(doc, "words", None) or ()
        cw = getattr(chunk, "words", None)
        if cw:
            try:
                w0, w1 = words[cw[0]], words[cw[-1]]
            except (IndexError, TypeError):
                return None
            return (int(w0.line), int(w0.start), int(w1.line), int(w1.end))
        ls = int(getattr(chunk, "line_start", 0))
        le = int(getattr(chunk, "line_end", ls))
        last = max(ls, min(le, len(self._plain) - 1))
        if not self._plain:
            return None
        return (ls, 0, last, len(self._plain[last]))

    def _chunk_seg(
        self, row: Row, region: tuple[int, int, int, int] | None
    ) -> tuple[int, int] | None:
        if region is None:
            return None
        l0, c0, l1, c1 = region
        if row.line < l0 or row.line > l1:
            return None
        a = c0 if row.line == l0 else row.col_start
        b = c1 if row.line == l1 else row.col_end
        return _clip((a, b), row.col_start, row.col_end)

    def _match_segs(
        self, row: Row, matches: Sequence[tuple[int, int, int]] | None
    ) -> tuple[tuple[int, int], ...] | None:
        if not matches:
            return None
        out = []
        for line, s, e in matches:
            if line != row.line:
                continue
            seg = _clip((s, e), row.col_start, row.col_end)
            if seg:
                out.append(seg)
        return tuple(out) if out else None

    # -- input ------------------------------------------------------------

    def read_event(self, timeout_ms: int = 50):
        """Convenience wrapper around `readaloud.keys.read_event`."""
        return _read_event(self.stdscr, timeout_ms)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class _Never:
    """Sentinel that compares equal to nothing, so the first draw paints all."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:  # pragma: no cover - trivial
        return False

    def __ne__(self, other: object) -> bool:  # pragma: no cover - trivial
        return True

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return 0


_NEVER = _Never()


def _clip(span: tuple[int, int] | None, lo: int, hi: int) -> tuple[int, int] | None:
    if span is None:
        return None
    a = max(span[0], lo)
    b = min(span[1], hi)
    return (a, b) if b > a else None


def _sanitise(text: str) -> str:
    """Replace control characters with spaces, 1:1 so char offsets survive."""
    if text.isprintable():
        return text
    return "".join(c if (c.isprintable() or c == " ") else " " for c in text)


def _wrap_line(
    text: str,
    width: int,
    word_spans: Sequence[tuple[int, int]],
    cols: Sequence[int] | None = None,
) -> list[tuple[int, int]]:
    """Greedy word wrap of one logical line.

    Returns ``[(col_start, col_end)]`` in *character* offsets, but every fit
    decision is made in terminal *cells* (`cols` is `cell_offsets(text)`, and
    is computed here when the caller does not supply it): a row of CJK text
    holds half as many characters as a row of ASCII.

    Breaks after whitespace where possible; otherwise breaks at the start of
    the word that straddles the margin; only hard-breaks mid-word when the word
    is wider than the whole terminal.  Whitespace that falls at a break point is
    dropped from both rows, so a continuation never begins with stray
    indentation.
    """
    n = len(text)
    if n == 0:
        return [(0, 0)]
    if cols is None:
        cols = cell_offsets(text)
    if cols[n] <= width:
        return [(0, n)]

    # inside[p] is True when p falls strictly inside a word, i.e. breaking there
    # would split the word in half.
    inside = bytearray(n + 1)
    starts = [s for s, _ in word_spans]
    for s, e in word_spans:
        for p in range(s + 1, min(e, n)):
            inside[p] = 1

    out: list[tuple[int, int]] = []
    start = 0
    while start < n:
        if cols[n] - cols[start] <= width:
            out.append((start, n))
            break
        # `limit` is the first character that would not fit on this row.
        limit = bisect.bisect_right(cols, cols[start] + width, start, n + 1) - 1
        if cols[limit] - cols[start] > width:  # pragma: no cover - defensive
            limit -= 1
        if limit <= start:
            limit = start + 1
        brk = -1
        p = limit
        while p > start:
            if text[p - 1].isspace() and not inside[p]:
                brk = p
                break
            p -= 1
        if brk < 0:
            # no usable whitespace on this row
            i = bisect.bisect_right(starts, limit) - 1
            wstart = -1
            if 0 <= i < len(word_spans):
                ws, we = word_spans[i]
                if ws < limit < we:
                    wstart = ws
            if wstart > start:
                brk = wstart
            else:
                brk = limit  # word wider than the terminal: nothing else to do
        out.append((start, brk))
        nxt = brk
        while nxt < n and text[nxt].isspace() and not inside[nxt]:
            nxt += 1
        start = nxt if nxt > brk else brk
    if not out:
        out.append((0, n))
    return out
