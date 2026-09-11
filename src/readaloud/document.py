"""readaloud.document -- logical lines -> words -> speakable chunks.

This module turns the styled logical lines produced by :mod:`readaloud.ansi`
into the two things the rest of the reader needs:

* :class:`Word` -- one highlightable span per spoken word, addressed by
  ``(line, start, end)`` in the *plain* (Run-flattened) coordinate space that
  :mod:`readaloud.ui` hit-tests mouse clicks against.
* :class:`Chunk` -- one unit of synthesis, holding the exact text handed to the
  TTS plus the offset of every one of its words inside that text.

Invariants other modules rely on (all covered by tests/test_document.py):

* ``doc.plain[w.line][w.start:w.end] == w.text`` for every word -- ``Word.text``
  is a *verbatim* slice of the line, never normalised.  ``readaloud.speech``
  derives each word's end offset as ``offset + len(word.text)``, which is only
  correct because of this.
* ``doc.words`` is in reading order and ``doc.words[i].idx == i``.  Reading
  order is line order, except inside a table (see below): there it is row by
  row, cell by cell, and line then start within a cell, so a wrapped cell's
  second line comes before its right-hand neighbour's first.
* ``chunk.text[chunk.offsets[i]:][:len(w.text)] == w.text`` where
  ``w = doc.words[chunk.words[i]]``.
* ``chunk.words`` is contiguous and ascending; concatenating the ``words`` lists
  of all chunks in order reproduces ``range(len(doc.words))`` exactly -- every
  word lives in exactly one chunk.
* ``chunk.text`` is a verbatim slice of ``"\\n".join(doc.plain)``, so a chunk
  that spans several lines keeps its newlines and its indentation -- except a
  table cell, whose text is its trimmed per line segments joined by the
  cell's ``joins`` (a wrapped cell's lines interleave with its neighbours').
* Chunks cover every line of the document.  ``chunk.line_start`` is inclusive,
  ``chunk.line_end`` is **exclusive**.  Consecutive chunks may share one line
  (when a long paragraph is split at a sentence boundary in the middle of a
  line), so ``chunks[k].line_start`` can equal ``chunks[k-1].line_end - 1``.
  Every cell chunk of one table row claims the whole row's line range, so
  consecutive cells of a row share all of it; the next row, or the rule that
  follows, starts exactly at that row's ``line_end``.
* A chunk with no speakable words (blank runs, horizontal rules, table rules,
  empty cells) is *kept* -- so the line coverage above holds -- but is flagged
  ``speakable=False`` and must be skipped by playback.

Tables: a :class:`Table` handed to the Document (the caller maps them from
mdcat's render) is chunked as one ``kind="rule"`` chunk per rule line and
one ``kind="cell"`` chunk per cell, header row included, never split into
sentences.  A Table that does not fit the lines and words is ignored and its
lines read as ordinary text; ``doc.tables`` holds the ones in use.

Word segmentation keeps as ONE word: contractions (``it's``, ``don't``),
hyphenated compounds (``well-known``), money and decimals (``$4.50``,
``12.5%``), ISO dates (``2026-09-10``), times (``3:45pm``), identifiers
(``mlx_audio``), initialisms and abbreviations (``e.g.``, ``U.S.``, ``Dr.``),
URLs and e-mail addresses.  Surrounding quotes, brackets and punctuation are
excluded from the highlighted span but remain in ``chunk.text``, so the TTS
still hears them.

``mdcat``'s degraded (non-tty) output writes links as ``homepage[1]`` plus a
trailing ``[1]: https://...`` block.  Both halves are silenced: the reference
block yields no words, and the inline markers are removed by
:func:`strip_reference_markers` before ``plain`` is derived.  For output
of ``mdcat --ansi``, pass ``references=False``: there ``[1]`` is mostly
a footnote number (removing it would shift the table columns measured on the
render) and ``[1]: ...`` a footnote, and both are read.  ``--ansi`` still
writes that block for an image inside a link (a README badge), because
terminal links cannot nest, and numbers it from 1 like the footnotes.  Its
link gives it away: such a marker sits inside the enclosing link and the
reference's URL is a link itself, while a footnote carries no link at all.
Outside the tables those markers are removed and those references give no
words, so the prose sounds as it does through the pipe.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Container, Iterable, Iterator, Sequence

from .ansi import Run, Style
from .width import cell_offsets

__all__ = [
    "DEFAULT_MAX_SENTENCES",
    "DEFAULT_MAX_CHARS",
    "ABBREVIATIONS",
    "Word",
    "Chunk",
    "Table",
    "TableCell",
    "Document",
    "line_word_spans",
    "split_words",
    "extract_words",
    "sentence_spans",
    "build_chunks",
    "strip_reference_markers",
]

DEFAULT_MAX_SENTENCES = 4
DEFAULT_MAX_CHARS = 380


# ---------------------------------------------------------------------------
# contract types
# ---------------------------------------------------------------------------


@dataclass
class Word:
    """One highlightable, speakable word."""

    text: str            # verbatim slice of Document.plain[line]
    line: int            # index into Document.lines / Document.plain
    start: int           # char offset within that logical line
    end: int             # exclusive
    idx: int             # global word index (position in Document.words)

    @property
    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    def contains(self, line: int, col: int) -> bool:
        return line == self.line and self.start <= col < self.end


@dataclass
class Chunk:
    """One unit of synthesis."""

    idx: int
    words: list[int]           # global word indices, contiguous and ascending
    text: str                  # exact plain text handed to the TTS
    offsets: list[int]         # offsets[i] = char offset of words[i] in `text`
    line_start: int            # inclusive
    line_end: int              # EXCLUSIVE
    # --- additive: defaulted, so the positional order above stays valid ---
    speakable: bool = True     # False => nothing to say; playback skips it
    word_texts: list[str] = field(default_factory=list)  # doc.words[i].text
    #: "para" | "code" | "blank" | "cell" | "rule"; "text" is only the
    #: default, for chunks built by hand
    kind: str = "text"
    #: (table, row, col) when ``kind == "cell"``; row 0 is the header row
    cell: tuple[int, int, int] | None = None
    #: (line, start, end) ranges that make up this chunk on screen, one per
    #: physical line, end exclusive.  Empty means "from the first word to the
    #: last", which is right for prose; a table cell sets it, because its lines
    #: interleave with its neighbours' and that stream would wash them too.
    regions: list[tuple[int, int, int]] = field(default_factory=list)
    #: the last spoken cell of its table row, where the pause between rows
    #: belongs; a row with nothing to say has none
    row_end: bool = False

    @property
    def line_span(self) -> tuple[int, int]:
        """``(line_start, line_end)`` -- end exclusive, usable as a slice."""
        return (self.line_start, self.line_end)

    def spans(self) -> list[tuple[int, int]]:
        """(start, end) of every word slot inside ``self.text``."""
        return [(o, o + len(t)) for o, t in zip(self.offsets, self.word_texts)]


@dataclass
class TableCell:
    """One cell of a rendered table, in display (``Document.plain``) coordinates."""

    row: int                   # 0 is the header row
    col: int
    #: (line, start, end): the cell's column on EVERY physical line of its row,
    #: as char offsets into that line, end exclusive -- blank lines included
    spans: list[tuple[int, int, int]]
    #: joins[i] goes between the text gathered so far and span i's text when
    #: both are non-empty: "" where the renderer wrapped inside a word (a URL,
    #: a hard-split long word, CJK), " " where it wrapped at a space.  Missing
    #: entries mean " ".
    joins: list[str] = field(default_factory=list)


@dataclass
class Table:
    """Where one rendered table sits and how its lines divide into cells."""

    line_start: int            # the top rule, inclusive
    line_end: int              # one past the bottom rule
    ncols: int
    #: (line_start, line_end) of every row, header row first, end exclusive
    rows: list[tuple[int, int]]
    #: every cell of every row, in reading order: row by row, left to right
    cells: list[TableCell]


# ---------------------------------------------------------------------------
# word segmentation
# ---------------------------------------------------------------------------

# Abbreviations whose trailing '.' is part of the word and must NOT be read as
# the end of a sentence.  Stored lowercase, without the dot.
ABBREVIATIONS: frozenset[str] = frozenset(
    """
    mr mrs ms mx dr prof rev fr hon sr jr st gen col sgt capt lt cmdr adm
    gov sen rep pres supt asst
    fig figs eq eqs ref refs sec secs ch chap chaps vol vols pt pts no nos
    pp p par para ln ver rev ed eds trans illus
    al cf viz etc vs approx est esp incl excl min max avg
    inc ltd co corp dept univ assn bros mfg
    jan feb mar apr jun jul aug sep sept oct nov dec
    mon tue tues wed thu thur thurs fri sat sun
    e.g i.e a.m p.m u.s u.k u.n ph.d b.a m.a m.s b.s d.c a.d b.c
    """.split()
)

# A letter or digit (unicode aware, underscore excluded) or a combining mark,
# so "Résumé" stays one word.
_ALNUM = r"(?:[^\W_]|[\u0300-\u036f])"
_LETTER = r"[^\W\d_]"
_CURRENCY = r"[$£€¥₹₩]"
# Characters that may sit *between* two alphanumerics without ending the word:
# apostrophes (it's / don't), hyphens (well-known, 2026-09-10), underscore
# (mlx_audio), dot (e.g. 12.5, www.example.com), colon (3:45pm), slash (TCP/IP)
# and ampersand (AT&T).
_CONNECT = r"['\u2019\u02bc\-\u2010\u2011_./:&]"

_ABBREV_ALT = "|".join(
    re.escape(a) for a in sorted(ABBREVIATIONS, key=len, reverse=True)
    if "." not in a
)

_WORD_RE = re.compile(
    # A URL or an e-mail address is a single word.
    r"(?P<url>(?:https?|ftps?|file|mailto|ssh|git)://[^\s<>\"'|)\]]+)"
    r"|(?P<email>" + _ALNUM + r"[\w.+-]*@" + _ALNUM + r"[\w-]*(?:\." + _ALNUM
    + r"[\w-]*)+)"
    # An initialism keeps its dots: e.g. / i.e. / U.S. / U.S.A / a.m.
    r"|(?P<initialism>(?:" + _LETTER + r"\.){2,}(?:" + _LETTER + r"(?!\.))?)"
    # A known abbreviation keeps its dot: Dr. / Fig. / etc.
    r"|(?P<abbrev>(?i:" + _ABBREV_ALT + r")\.(?!\w))"
    # The general case: alphanumerics joined by single connectors, with an
    # optional leading currency sign and an optional trailing percent sign.
    r"|(?P<word>(?:" + _CURRENCY + r"(?=\d))?" + _ALNUM
    + r"(?:(?:" + _CONNECT + r"|,(?=\d\d\d(?!\d)))?" + _ALNUM + r")*"
    + r"(?:(?<=\d)%)?)"
)

_HAS_ALNUM = re.compile(r"[^\W_]")
# Trailing characters trimmed off a URL / e-mail match (sentence punctuation
# that happens to abut the address).
_URL_TRAIL = ".,;:!?'\"’”"

# mdcat (and plain markdown) emit a trailing reference block when stdout is not
# a tty.  It stays visible but must never be spoken.  Group 1 is the label.
_REF_DEF_RE = re.compile(r"^[ \t]*\[([^\]]{1,40})\]:[ \t]*\S+")
# The other half of that degraded form: the inline `homepage[1]` marker that
# points at the reference block.  Only ever stripped when a matching
# `[1]: https://...` definition really is present (see
# :func:`strip_reference_markers`).
_REF_MARKER_RE = re.compile(r"(?<=[\w\]])\[(\d{1,4})\]")
# The same marker when its link shows what it is (see
# :func:`_image_link_references`): an image without alt text leaves nothing
# before it, and alt text may end in any char ("coverage 90%[2]").
_LINKED_MARKER_RE = re.compile(r"\[(\d{1,4})\]")


def line_word_spans(text: str) -> list[tuple[int, int]]:
    """Char spans of every speakable word in one plain line of text."""
    out: list[tuple[int, int]] = []
    for m in _WORD_RE.finditer(text):
        s, e = m.span()
        if m.lastgroup in ("url", "email"):
            while e > s and text[e - 1] in _URL_TRAIL:
                e -= 1
        if e > s and _HAS_ALNUM.search(text, s, e):
            out.append((s, e))
    return out


def split_words(text: str) -> list[str]:
    """The speakable words of one line, as strings (convenience wrapper)."""
    return [text[s:e] for s, e in line_word_spans(text)]


def extract_words(plain: Sequence[str], *, references: bool = True,
                  silent: Container[int] = ()) -> list[Word]:
    """Every speakable word of a document, in line order.

    With ``references=False`` a ``[1]: ...`` line is read like any other: it is
    a footnote, not the link reference block of mdcat's degraded output.  The
    line numbers in `silent` give no words either (the Document puts the
    references of images inside links there, see the module docstring).
    """
    words: list[Word] = []
    for line_no, text in enumerate(plain):
        if (not text or (references and _REF_DEF_RE.match(text))
                or line_no in silent):
            continue          # a link-reference definition has nothing to say
        for s, e in line_word_spans(text):
            words.append(Word(text=text[s:e], line=line_no, start=s, end=e,
                              idx=len(words)))
    return words


# ---------------------------------------------------------------------------
# sentence segmentation
# ---------------------------------------------------------------------------

# Terminator run, plus any closing quotes/brackets, that is followed by
# whitespace or the end of the paragraph.
_SENT_END_RE = re.compile(
    r"[.!?…‽]+[\"'”’»›)\]}]*(?=\s|$)"
)
# A dotted initialism as it looks *before* its final dot: "e.g", "U.S", "J".
_INITIAL_RE = re.compile(r"(?:" + _LETTER + r"\.)*" + _LETTER)


def _is_sentence_break(text: str, m: re.Match, lo: int, hi: int) -> bool:
    """Decide whether the terminator matched at `m` really ends a sentence."""
    stop = m.start()
    # The whitespace-delimited token immediately before the terminator.
    i = stop
    while i > lo and not text[i - 1].isspace():
        i -= 1
    token = text[i:stop]

    # The first non-space character after the terminator.
    j = m.end()
    while j < hi and text[j].isspace():
        j += 1
    nxt = text[j] if j < hi else ""

    # "he said. and then" -- a lowercase continuation is almost never a new
    # sentence, whatever the punctuation.
    if nxt and nxt.islower():
        return False

    if text[stop] == ".":
        low = token.lower()
        if low in ABBREVIATIONS:
            return False                       # Dr. / Fig. / etc.
        if _INITIAL_RE.fullmatch(token):
            return False                       # U.S. / e.g. / J. R. R.
        if token.isdigit() and len(token) <= 3:
            return False                       # "3." of a numbered list
    return True


def sentence_spans(text: str, lo: int = 0, hi: int | None = None
                   ) -> list[tuple[int, int]]:
    """Sentence spans of ``text[lo:hi]``, as absolute offsets.

    Leading and trailing whitespace is trimmed off each span, so the spans are
    non-overlapping but do not tile the input.  Abbreviations, initialisms and
    numbered-list markers do not end a sentence.
    """
    if hi is None:
        hi = len(text)
    spans: list[tuple[int, int]] = []
    cur = lo
    for m in _SENT_END_RE.finditer(text, lo, hi):
        end = m.end()
        if end <= cur:
            continue
        if not _is_sentence_break(text, m, lo, hi):
            continue
        start = cur
        while start < end and text[start].isspace():
            start += 1
        if start < end:
            spans.append((start, end))
        cur = end
    start, end = cur, hi
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        spans.append((start, end))
    return spans


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LIST_MARK_RE = re.compile(
    r"^(?:[-*+•◦‣⁃·]\s|\d+[.)]\s|[a-zA-Z][.)]\s|>)"
)
# Clause-level fallback split points for a sentence longer than max_chars.
_CLAUSE_RE = re.compile(r"[,;:—–][\"'”’)\]]?(?=\s)")


def _flatten(plain: Sequence[str]) -> tuple[str, list[int]]:
    """The whole document as one string, plus each line's offset into it."""
    offsets: list[int] = []
    pos = 0
    for line in plain:
        offsets.append(pos)
        pos += len(line) + 1
    return "\n".join(plain), offsets


def _is_indented(line: str) -> bool:
    return bool(line.strip()) and (line.startswith("    ") or line.startswith("\t"))


def _starts_code(line: str) -> bool:
    """An indented line that is not just a deeply nested list item."""
    return _is_indented(line) and not _LIST_MARK_RE.match(line.strip())


def _line_groups(plain: Sequence[str], lo: int = 0, hi: int | None = None
                 ) -> list[tuple[int, int, str]]:
    """Partition lines ``[lo, hi)`` into (start, end_exclusive, kind) groups.

    kind is "blank", "code" (fenced or indented -- never split further) or
    "para".  The groups tile the range, the whole document by default.  A
    table cuts the document into ranges, so no group (a fence opened by a
    cell that happens to start with backticks, say) runs through one.
    """
    groups: list[tuple[int, int, str]] = []
    n = len(plain) if hi is None else hi
    i = lo
    prev_blank = lo <= 0 or not plain[lo - 1].strip()
    while i < n:
        if not plain[i].strip():
            j = i
            while j < n and not plain[j].strip():
                j += 1
            groups.append((i, j, "blank"))
            i, prev_blank = j, True
            continue

        fence = _FENCE_RE.match(plain[i])
        if fence:
            marker = fence.group(1)[0]
            j = i + 1
            while j < n:
                close = _FENCE_RE.match(plain[j])
                j += 1
                if close and close.group(1)[0] == marker:
                    break
            groups.append((i, j, "code"))
            i, prev_blank = j, False
            continue

        if prev_blank and _starts_code(plain[i]):
            j, last = i, i
            while j < n:
                if _is_indented(plain[j]):
                    last = j
                    j += 1
                elif not plain[j].strip():
                    k = j
                    while k < n and not plain[k].strip():
                        k += 1
                    if k < n and _is_indented(plain[k]):
                        j = k          # blank line *inside* the code block
                    else:
                        break
                else:
                    break
            groups.append((i, last + 1, "code"))
            i, prev_blank = last + 1, False
            continue

        j = i
        while j < n and plain[j].strip() and not _FENCE_RE.match(plain[j]):
            j += 1
        groups.append((i, j, "para"))
        i, prev_blank = j, False
    return groups


def _rfind_space(text: str, lo: int, hi: int) -> int:
    for k in range(hi - 1, lo - 1, -1):
        if text[k].isspace():
            return k
    return -1


def _find_space(text: str, lo: int, hi: int) -> int:
    for k in range(lo, hi):
        if text[k].isspace():
            return k
    return -1


def _split_long(text: str, lo: int, hi: int, max_chars: int
                ) -> list[tuple[int, int]]:
    """Break an over-long sentence, never inside a word.

    Preference order: a line break, then a clause terminator (``,;:`` + space),
    then any whitespace.  A single unbreakable token longer than ``max_chars``
    is emitted whole -- correctness beats the limit.
    """
    out: list[tuple[int, int]] = []
    cur = lo
    while hi - cur > max_chars:
        window = min(hi, cur + max_chars)
        cut = text.rfind("\n", cur + 1, window + 1)
        if cut <= cur:
            cut = -1
            for m in _CLAUSE_RE.finditer(text, cur + 1, window + 1):
                cut = m.end()
        if cut <= cur:
            cut = _rfind_space(text, cur + 1, window + 1)
        if cut <= cur:
            # Nothing to break on inside the window: run on to the next space
            # rather than splitting a word in half.
            cut = _find_space(text, window, hi)
        if cut <= cur:
            break                      # one giant token: emit the rest whole
        end = cut
        while end > cur and text[end - 1].isspace():
            end -= 1
        if end <= cur:
            break
        out.append((cur, end))
        while cut < hi and text[cut].isspace():
            cut += 1
        cur = cut
    if cur < hi:
        out.append((cur, hi))
    return out


def _paragraph_spans(text: str, lo: int, hi: int, max_sentences: int,
                     max_chars: int) -> list[tuple[int, int]]:
    """Split one paragraph into chunk-sized spans at sentence boundaries."""
    sentences = sentence_spans(text, lo, hi)
    if not sentences:
        return [(lo, hi)]

    units: list[tuple[int, int]] = []
    for s, e in sentences:
        if e - s > max_chars:
            units.extend(_split_long(text, s, e, max_chars))
        else:
            units.append((s, e))
    if not units:
        return [(lo, hi)]

    out: list[tuple[int, int]] = []
    start = end = -1
    count = 0
    for s, e in units:
        if start < 0:
            start, end, count = s, e, 1
        elif count >= max_sentences or (e - start) > max_chars:
            out.append((start, end))
            start, end, count = s, e, 1
        else:
            end, count = e, count + 1
    if start >= 0:
        out.append((start, end))
    return out


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def _gap(col: int, start: int, end: int) -> int:
    """How many chars `col` lies outside ``[start, end)``; 0 inside."""
    if col < start:
        return start - col
    return max(0, col - end + 1)


def _trimmed(plain: Sequence[str], line: int, start: int, end: int
             ) -> tuple[int, str]:
    """Where ``plain[line][start:end]`` starts once trimmed, and its text."""
    seg = plain[line][start:end]
    core = seg.strip()
    return start + len(seg) - len(seg.lstrip()), core


def _claim_table(plain: Sequence[str], by_line: dict[int, list[Word]],
                 table: Table) -> list[list[Word]] | None:
    """The words of every cell of `table`, or None when it does not fit.

    A Table comes from another module's reading of a render, so it is checked
    rather than trusted: rows must tile the lines between the three rules,
    cells must come in reading order with in-range spans that never overlap,
    and every word on a row line must sit inside exactly one cell (none may sit
    on a rule).  Anything else means the render and the map disagree, and
    reading such a table cell by cell would drop or repeat words.
    """
    top, end, ncols = table.line_start, table.line_end, table.ncols
    rows = [(a, b) for a, b in table.rows]
    cells = list(table.cells)
    if not (isinstance(ncols, int) and ncols >= 1 and rows
            and isinstance(top, int) and isinstance(end, int)
            and all(isinstance(a, int) and isinstance(b, int) for a, b in rows)
            and 0 <= top and end <= len(plain)
            and len(cells) == len(rows) * ncols):
        return None

    rules = {top, end - 1}
    expect = top + 1
    for r, (a, b) in enumerate(rows):
        if a != expect or b <= a:
            return None
        expect = b
        if r == 0:                      # the header rule follows the header
            rules.add(b)
            expect = b + 1
    if expect != end - 1:
        return None

    spans_on: dict[int, list[tuple[int, int, int]]] = {}
    for k, cell in enumerate(cells):
        r, c = divmod(k, ncols)
        if cell.row != r or cell.col != c:
            return None
        first, stop = rows[r]
        last = -1
        for line, s, e in cell.spans:
            if not (isinstance(line, int) and isinstance(s, int)
                    and isinstance(e, int) and first <= line < stop
                    and line > last and 0 <= s <= e <= len(plain[line])):
                return None
            last = line
            spans_on.setdefault(line, []).append((s, e, k))

    claimed: list[list[Word]] = [[] for _ in cells]
    for line in range(top, end):
        here = by_line.get(line, ())
        if line in rules:
            if here:
                return None
            continue
        spans = sorted(spans_on.get(line, ()))
        prev_end = 0
        for s, e, _k in spans:
            if s < prev_end:
                return None
            prev_end = e
        starts = [s for s, _e, _k in spans]
        for w in here:
            i = bisect_right(starts, w.start) - 1
            if i < 0 or w.end > spans[i][1]:
                return None
            claimed[spans[i][2]].append(w)
    for words in claimed:
        words.sort(key=lambda w: (w.line, w.start))
    return claimed


def _place_tables(plain: Sequence[str], words: Sequence[Word],
                  tables: Iterable[Table] | None
                  ) -> tuple[list[Table], list[list[list[Word]]]]:
    """The tables that fit, in document order, with each cell's words.

    Overlapping tables keep the one that starts first.
    """
    fits: list[tuple[Table, list[list[Word]]]] = []
    by_line: dict[int, list[Word]] | None = None
    for table in tables or ():
        if by_line is None:
            by_line = {}
            for w in words:
                by_line.setdefault(w.line, []).append(w)
        try:
            claimed = _claim_table(plain, by_line, table)
        except (AttributeError, TypeError, ValueError, IndexError):
            claimed = None
        if claimed is not None:
            fits.append((table, claimed))
    fits.sort(key=lambda pair: pair[0].line_start)

    used: list[Table] = []
    cells: list[list[list[Word]]] = []
    floor = 0
    for table, claimed in fits:
        if table.line_start >= floor:
            used.append(table)
            cells.append(claimed)
            floor = table.line_end
    return used, cells


def _layout_tables(plain: Sequence[str], words: list[Word],
                   tables: Iterable[Table] | None
                   ) -> tuple[list[Table], list[list[list[int]]]]:
    """Fit `tables` to `words`, then put `words` into reading order in place.

    Sorts every table's words row by row and cell by cell between the words
    around the table, and renumbers ``Word.idx``.  Returns the tables in use
    and, per table, per cell, the global indices of that cell's words.
    """
    used, claimed = _place_tables(plain, words, tables)
    if not used:
        return [], []
    order: dict[int, tuple[int, int]] = {}      # id(word) -> (row line, col)
    for table, cells in zip(used, claimed):
        for k, cell_words in enumerate(cells):
            row, col = divmod(k, table.ncols)
            for w in cell_words:
                order[id(w)] = (table.rows[row][0], col)

    def reading(w: Word) -> tuple[int, int, int, int]:
        at = order.get(id(w))
        return (at[0], at[1], w.line, w.start) if at else (w.line, 0, w.line,
                                                           w.start)

    words.sort(key=reading)
    for i, w in enumerate(words):
        w.idx = i
    return used, [[[w.idx for w in cw] for cw in cells] for cells in claimed]


def _cell_text(plain: Sequence[str], words: Sequence[Word], cell: TableCell,
               widxs: Sequence[int]) -> tuple[str, list[int], list[str]]:
    """(text, offsets, word_texts) of one cell chunk.

    The text is the cell's trimmed segments, line by line, joined by the
    cell's ``joins`` -- "" rejoins a word the renderer broke across lines, so
    the TTS hears one word where the screen shows two pieces.
    """
    ws = [words[i] for i in widxs]
    joins = cell.joins if isinstance(cell.joins, (list, tuple)) else ()
    parts: list[str] = []
    size = 0
    landed: dict[int, tuple[int, int]] = {}   # line -> (start, pos in text)
    for i, (line, s, e) in enumerate(cell.spans):
        start, core = _trimmed(plain, line, s, e)
        if not core:
            continue
        if parts:
            join = joins[i] if i < len(joins) else " "
            join = join if isinstance(join, str) else " "
            parts.append(join)
            size += len(join)
        landed[line] = (start, size)
        parts.append(core)
        size += len(core)
    text = "".join(parts)
    offsets = [landed[w.line][1] + w.start - landed[w.line][0] for w in ws]
    return text, offsets, [w.text for w in ws]


def _table_chunks(plain: Sequence[str], words: Sequence[Word], table: Table,
                  ti: int, cells: Sequence[Sequence[int]], idx: int
                  ) -> list[Chunk]:
    """Rule, header cells, rule, body cells row by row, rule."""
    out: list[Chunk] = []

    def rule(line: int) -> None:
        out.append(Chunk(idx=idx + len(out), words=[], text=plain[line],
                         offsets=[], line_start=line, line_end=line + 1,
                         speakable=False, word_texts=[], kind="rule"))

    ncols = table.ncols
    rule(table.line_start)
    for r, (first, stop) in enumerate(table.rows):
        if r == 1:
            rule(table.rows[0][1])
        # the beat between rows goes after the row's last cell that is read
        last = max((c for c in range(ncols) if cells[r * ncols + c]),
                   default=-1)
        for c in range(ncols):
            k = r * ncols + c
            cell = table.cells[k]
            widxs = list(cells[k])
            text, offsets, texts = _cell_text(plain, words, cell, widxs)
            out.append(Chunk(
                idx=idx + len(out), words=widxs, text=text, offsets=offsets,
                line_start=first, line_end=stop, speakable=bool(widxs),
                word_texts=texts, kind="cell", cell=(ti, r, c),
                regions=[(line, s, e) for line, s, e in cell.spans],
                row_end=c == last,
            ))
    if len(table.rows) == 1:
        rule(table.rows[0][1])
    rule(table.line_end - 1)
    return out


def build_chunks(plain: Sequence[str], words: Sequence[Word],
                 max_sentences: int = DEFAULT_MAX_SENTENCES,
                 max_chars: int = DEFAULT_MAX_CHARS, *,
                 tables: Sequence[Table] = (),
                 cells: Sequence[Sequence[Sequence[int]]] = ()
                 ) -> list[Chunk]:
    """Group lines into chunks: paragraphs first, then sentence splits.

    Blank runs, horizontal rules and code blocks are kept as chunks so the
    chunk list still covers every line; the ones with nothing to say come back
    with ``speakable=False``.

    Each table becomes rule and cell chunks instead (see :class:`Document`).
    `tables`, `words` and `cells` then come from the Document's table layout:
    the tables that fit, the words in reading order and, per table, per cell,
    the indices of its words.  Raises ValueError when `cells` does not have
    one entry per table.
    """
    if len(cells) != len(tables):
        raise ValueError(f"build_chunks: {len(tables)} tables but cells for "
                         f"{len(cells)}")
    if not plain:
        return []
    flat, line_offsets = _flatten(plain)
    nlines = len(plain)

    # The document outside the tables, as (first, stop, table index) pieces.
    pieces: list[tuple[int, int, int | None]] = []
    loose: Sequence[Word] = words
    cur = 0
    for ti, table in enumerate(tables):
        if table.line_start > cur:
            pieces.append((cur, table.line_start, None))
        pieces.append((table.line_start, table.line_end, ti))
        cur = table.line_end
    if cur < nlines:
        pieces.append((cur, nlines, None))
    if tables:
        inside = bytearray(nlines)
        for table in tables:
            inside[table.line_start:table.line_end] = (
                b"\x01" * (table.line_end - table.line_start))
        loose = [w for w in words if not inside[w.line]]

    word_at: list[int] = [line_offsets[w.line] + w.start for w in loose]

    chunks: list[Chunk] = []
    wi = 0
    nwords = len(loose)
    for piece_first, piece_stop, ti in pieces:
        if ti is not None:
            chunks.extend(_table_chunks(plain, words, tables[ti], ti, cells[ti],
                                        len(chunks)))
            continue

        # (flat_start, flat_end, kind, line_start, line_end) -- a line range of
        # None means "derive it from the flat span" (paragraph sub-chunks only).
        spans: list[tuple[int, int, str, int | None, int | None]] = []
        for first, stop, kind in _line_groups(plain, piece_first, piece_stop):
            lo = line_offsets[first]
            hi = line_offsets[stop - 1] + len(plain[stop - 1])
            # A group with nothing to say (blank run, horizontal rule, table
            # separator) is never split: it exists only to keep line coverage.
            has_words = bisect_left(word_at, lo) < bisect_left(word_at, hi)
            if kind == "para" and has_words:
                for a, b in _paragraph_spans(flat, lo, hi, max_sentences,
                                             max_chars):
                    spans.append((a, b, kind, None, None))
            else:
                spans.append((lo, hi, kind, first, stop))

        for a, b, kind, first, stop in spans:
            picked: list[int] = []
            offsets: list[int] = []
            texts: list[str] = []
            while wi < nwords and word_at[wi] < b:
                if word_at[wi] >= a:
                    picked.append(loose[wi].idx)
                    offsets.append(word_at[wi] - a)
                    texts.append(loose[wi].text)
                wi += 1
            if first is not None and stop is not None:
                line_start, line_end = first, stop
            else:
                line_start = bisect_right(line_offsets, a) - 1
                line_end = bisect_right(line_offsets, b - 1 if b > a else a)
            line_start = max(0, min(line_start, nlines - 1))
            line_end = max(line_start + 1, min(line_end, nlines))
            chunks.append(Chunk(
                idx=len(chunks),
                words=picked,
                text=flat[a:b],
                offsets=offsets,
                line_start=line_start,
                line_end=line_end,
                speakable=bool(picked),
                word_texts=texts,
                kind="blank" if kind == "blank" else kind,
            ))
    return chunks


# ---------------------------------------------------------------------------
# mdcat's degraded (non-tty) link markers
# ---------------------------------------------------------------------------


def _cut_spans(runs: Sequence[Run], cuts: Sequence[tuple[int, int]]
               ) -> list[Run]:
    """Delete ``cuts`` (offsets into the line's flat text) from ``runs``.

    Styling is preserved: every surviving character keeps the style of the run
    it came from, and runs left empty are dropped.
    """
    out: list[Run] = []
    pos = 0
    for run in runs:
        start, end = pos, pos + len(run.text)
        pos = end
        kept: list[str] = []
        prev = start
        for cut_start, cut_end in cuts:
            if cut_end <= start or cut_start >= end:
                continue
            cut_start, cut_end = max(cut_start, start), min(cut_end, end)
            kept.append(run.text[prev - start:cut_start - start])
            prev = cut_end
        kept.append(run.text[prev - start:])
        text = "".join(kept)
        if text:
            out.append(Run(text, run.style))
    return out


def strip_reference_markers(lines: Iterable[Sequence[Run]]) -> list[list[Run]]:
    """Drop ``mdcat``'s inline ``homepage[1]`` link markers from ``lines``.

    When ``mdcat`` writes to a pipe rather than a tty it degrades to plain text
    and turns every link into ``text[1]`` plus a trailing ``[1]: https://...``
    reference block.  The reference block already produces no words, but the
    inline markers used to segment into a bare digit that the TTS pronounced
    ("...reference manual two instead").

    The markers are removed here, before :attr:`Document.plain` is derived, so
    word offsets, ``chunk.text`` and the mouse hit-test all stay consistent --
    and so the digit is neither spoken nor displayed.

    Deliberately conservative: a marker is only removed when the document
    actually contains a reference definition carrying that exact label, and
    never inside a fenced or indented code block (where ``buf[0]`` is real).
    """
    out = [list(line) for line in lines]
    plain = ["".join(r.text for r in line) for line in out]

    labels: set[str] = set()
    for text in plain:
        m = _REF_DEF_RE.match(text)
        if m:
            labels.add(m.group(1).strip())
    if not labels:
        return out

    in_code: set[int] = set()
    for first, stop, kind in _line_groups(plain):
        if kind == "code":
            in_code.update(range(first, stop))

    for i, text in enumerate(plain):
        if "[" not in text or i in in_code or _REF_DEF_RE.match(text):
            continue
        cuts = [m.span() for m in _REF_MARKER_RE.finditer(text)
                if m.group(1) in labels]
        if cuts:
            out[i] = _cut_spans(out[i], cuts)
    return out


def _hrefs(line: Sequence[Run]) -> list[str | None]:
    """The link of every char of `line`, None where there is none."""
    return [run.style.href for run in line for _ in run.text]


def _image_link_references(lines: Iterable[Sequence[Run]],
                           tables: Iterable[Table] = ()
                           ) -> tuple[list[list[Run]], set[int]]:
    """``mdcat --ansi``'s markers of images inside links cut from `lines`.

    Terminal links cannot nest, so ``--ansi`` writes an image inside a link as
    ``Build status[1]`` and, after the paragraph, ``[1]: <image URL>``: the
    shapes of a footnote, and numbered from 1 like the footnotes.  A reference
    is a ``[n]: `` line whose target is a link (its URL, or its path resolved
    to a file URL), which a footnote's text never is.  Its marker is the first
    ``[n]`` before it every char of which is inside a link: mdcat writes each
    number once, so a later ``[1]`` in the text of a link stays.  Neither is
    looked for on the lines of `tables`, whose cells were measured on the
    render as it is.

    Returns the lines, the markers cut out as :func:`strip_reference_markers`
    cuts them, and the line numbers of the references, which say nothing.
    """
    out = [list(line) for line in lines]
    plain = ["".join(run.text for run in line) for line in out]
    tabled: set[int] = set()
    for table in tables:
        try:
            tabled.update(range(max(0, table.line_start),
                                min(len(out), table.line_end)))
        except (AttributeError, TypeError):
            continue                    # a Table that does not fit anyway

    silent: set[int] = set()
    unmarked: dict[str, int] = {}       # label -> its reference line
    for i, text in enumerate(plain):
        m = _REF_DEF_RE.match(text)
        if i in tabled or not m or not m.group(1).isdigit():
            continue
        hrefs = _hrefs(out[i])
        target = m.end(1) + 2           # past "]:"
        while text[target] in " \t":
            target += 1
        if hrefs[target] and not any(hrefs[:target]):
            silent.add(i)
            unmarked.setdefault(m.group(1), i)

    for i, text in enumerate(plain):
        if not unmarked:
            break
        if i in tabled or i in silent or "[" not in text:
            continue
        hrefs = _hrefs(out[i])
        cuts = []
        for m in _LINKED_MARKER_RE.finditer(text):
            if (unmarked.get(m.group(1), -1) > i
                    and all(hrefs[m.start():m.end()])):
                cuts.append(m.span())
                del unmarked[m.group(1)]
        if cuts:
            out[i] = _cut_spans(out[i], cuts)
    return out, silent


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class Document:
    """A parsed document: styled lines, plain lines, words and chunks.

    `tables` maps rendered tables onto `lines` (the caller builds them);
    each one that fits is read one cell at a time, see :class:`Table`.
    ``references=False`` turns off the handling of mdcat's degraded link
    reference output and handles only the references ``mdcat --ansi`` writes
    for images inside links, see the module docstring.
    """

    def __init__(self, lines: Iterable[Sequence[Run]] | None = None, *,
                 max_sentences: int = DEFAULT_MAX_SENTENCES,
                 max_chars: int = DEFAULT_MAX_CHARS,
                 tables: Sequence[Table] = (),
                 references: bool = True) -> None:
        self.max_sentences = int(max_sentences)
        self.max_chars = int(max_chars)
        self.references = bool(references)
        source = lines or ()
        tables = list(tables or ())
        self.lines: list[list[Run]]
        silent: set[int] = set()
        if self.references:
            self.lines = strip_reference_markers(source)
        else:
            self.lines, silent = _image_link_references(source, tables)
        self.plain: list[str] = ["".join(r.text for r in line)
                                 for line in self.lines]
        self.words: list[Word] = extract_words(
            self.plain, references=self.references, silent=silent)
        self.tables: list[Table]
        self.tables, cells = _layout_tables(self.plain, self.words, tables)
        self.chunks: list[Chunk] = build_chunks(
            self.plain, self.words, self.max_sentences, self.max_chars,
            tables=self.tables, cells=cells)

        # word index -> (chunk index, slot inside that chunk)
        self._chunk_of: list[int] = [0] * len(self.words)
        self._slot_of: list[int] = [0] * len(self.words)
        for chunk in self.chunks:
            for slot, widx in enumerate(chunk.words):
                self._chunk_of[widx] = chunk.idx
                self._slot_of[widx] = slot

        # line -> the word indices on it, ascending by start offset.  Sorted
        # outright: word_at bisects, and inside a table the global order is
        # reading order, not line order.
        self._line_words: dict[int, list[int]] = {}
        for w in self.words:
            self._line_words.setdefault(w.line, []).append(w.idx)
        for idxs in self._line_words.values():
            idxs.sort(key=lambda i: self.words[i].start)
        self._line_starts: dict[int, list[int]] = {
            line: [self.words[i].start for i in idxs]
            for line, idxs in self._line_words.items()
        }

        # line -> the first chunk covering it (every cell of a row covers the
        # row's lines, so on a row that is its first cell)
        self._line_chunk: list[int] = [-1] * len(self.plain)
        for c in self.chunks:
            for li in range(c.line_start, c.line_end):
                if self._line_chunk[li] < 0:
                    self._line_chunk[li] = c.idx

        # line -> (start, end, chunk index) of every cell region on it
        self._cell_regions: dict[int, list[tuple[int, int, int]]] = {}
        for c in self.chunks:
            if c.kind != "cell":
                continue
            for line, s, e in c.regions:
                self._cell_regions.setdefault(line, []).append((s, e, c.idx))
        for regions in self._cell_regions.values():
            regions.sort()

    # -- construction helpers ------------------------------------------------

    @classmethod
    def from_text(cls, text: str, **kwargs) -> "Document":
        """Build a document from unstyled plain text."""
        flat = text.replace("\r\n", "\n").replace("\r", "\n")
        style = Style()
        lines = [[Run(part.expandtabs(4), style)] for part in flat.split("\n")]
        return cls(lines, **kwargs)

    @classmethod
    def from_runs(cls, lines: Iterable[Sequence[Run]], **kwargs) -> "Document":
        """Build a document from :func:`readaloud.ansi.parse` output."""
        return cls(lines, **kwargs)

    # -- contract API --------------------------------------------------------

    def word_at(self, line: int, col: int) -> int | None:
        """Global index of the word covering ``(line, col)``, else None."""
        idxs = self._line_words.get(line)
        if not idxs:
            return None
        starts = self._line_starts[line]
        k = bisect_right(starts, col) - 1
        if k < 0:
            return None
        widx = idxs[k]
        w = self.words[widx]
        return widx if w.start <= col < w.end else None

    def chunk_of_word(self, widx: int) -> int:
        """Index of the chunk that owns word ``widx``."""
        return self._chunk_of[widx]

    # -- additive helpers ----------------------------------------------------

    def slot_of_word(self, widx: int) -> int:
        """Position of word ``widx`` inside ``chunk.words`` (a ``Timed.word_slot``)."""
        return self._slot_of[widx]

    def nearest_word(self, line: int, col: int) -> int | None:
        """The word at ``(line, col)``, or the closest one on that line.

        On a table row line the answer never leaves the cell: a click in a
        cell, or in the gutter or prefix beside it (the nearest cell on that
        line), picks that cell's closest word on any line of the row, and None
        when the cell is empty -- a neighbour's word would play the wrong cell.
        """
        regions = self._cell_regions.get(line)
        if regions:
            cidx = self.cell_at(line, col)
            if cidx is None:
                cidx = min(regions, key=lambda r: _gap(col, r[0], r[1]))[2]
            return self._nearest_in_cell(cidx, line, col)
        hit = self.word_at(line, col)
        if hit is not None:
            return hit
        idxs = self._line_words.get(line)
        if not idxs:
            return None
        best, best_d = None, None
        for widx in idxs:
            w = self.words[widx]
            d = 0 if w.start <= col < w.end else min(abs(col - w.start),
                                                     abs(col - (w.end - 1)))
            if best_d is None or d < best_d:
                best, best_d = widx, d
        return best

    def _nearest_in_cell(self, cidx: int, line: int, col: int) -> int | None:
        """The word of cell chunk `cidx` closest to ``(line, col)``.

        The same line wins, then the nearest line, then the nearest column.
        Columns are compared in terminal cells: the row's lines are laid out
        on one grid, but a wide glyph earlier on one line shifts its chars.
        """
        cols: dict[int, list[int]] = {}

        def columns(li: int) -> list[int]:
            if li not in cols:
                cols[li] = cell_offsets(self.plain[li])
            return cols[li]

        here = columns(line)
        x = here[max(0, min(col, len(here) - 1))]
        best, best_key = None, None
        for widx in self.chunks[cidx].words:
            w = self.words[widx]
            x0, x1 = columns(w.line)[w.start], columns(w.line)[w.end]
            dx = 0 if x0 <= x < x1 else min(abs(x - x0), abs(x - (x1 - 1)))
            key = (abs(w.line - line), dx)
            if best_key is None or key < best_key:
                best, best_key = widx, key
        return best

    def cell_at(self, line: int, col: int) -> int | None:
        """The cell chunk whose region on ``line`` holds char ``col``, or None."""
        for s, e, cidx in self._cell_regions.get(line, ()):
            if s <= col < e:
                return cidx
        return None

    def chunk_at_line(self, line: int) -> int | None:
        """The first chunk whose line span covers ``line``."""
        if 0 <= line < len(self._line_chunk) and self._line_chunk[line] >= 0:
            return self._line_chunk[line]
        return None

    def word_span_in_chunk(self, widx: int) -> tuple[int, int]:
        """``(start, end)`` of word ``widx`` inside its own chunk's text."""
        chunk = self.chunks[self._chunk_of[widx]]
        slot = self._slot_of[widx]
        start = chunk.offsets[slot]
        return (start, start + len(self.words[widx].text))

    @property
    def speakable_chunks(self) -> list[int]:
        return [c.idx for c in self.chunks if c.speakable]

    def next_speakable_chunk(self, idx: int, wrap: bool = False) -> int | None:
        """The first speakable chunk after ``idx``."""
        for c in self.chunks[max(0, idx + 1):]:
            if c.speakable:
                return c.idx
        if wrap:
            return self.first_speakable_chunk()
        return None

    def prev_speakable_chunk(self, idx: int, wrap: bool = False) -> int | None:
        """The last speakable chunk before ``idx``."""
        for c in reversed(self.chunks[:max(0, idx)]):
            if c.speakable:
                return c.idx
        if wrap:
            speakable = self.speakable_chunks
            return speakable[-1] if speakable else None
        return None

    def first_speakable_chunk(self) -> int | None:
        for c in self.chunks:
            if c.speakable:
                return c.idx
        return None

    def words_of_line(self, line: int) -> list[int]:
        return list(self._line_words.get(line, ()))

    def iter_words(self) -> Iterator[Word]:
        return iter(self.words)

    def __len__(self) -> int:
        return len(self.lines)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<Document lines={len(self.lines)} words={len(self.words)} "
                f"chunks={len(self.chunks)} "
                f"speakable={len(self.speakable_chunks)}>")
