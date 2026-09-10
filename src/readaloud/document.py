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
* ``chunk.text[chunk.offsets[i]:][:len(w.text)] == w.text`` where
  ``w = doc.words[chunk.words[i]]``.
* ``chunk.words`` is contiguous and ascending; concatenating the ``words`` lists
  of all chunks in order reproduces ``range(len(doc.words))`` exactly -- every
  word lives in exactly one chunk.
* ``chunk.text`` is always a verbatim slice of ``"\\n".join(doc.plain)``, so a
  chunk that spans several lines keeps its newlines and its indentation.
* Chunks cover every line of the document.  ``chunk.line_start`` is inclusive,
  ``chunk.line_end`` is **exclusive**.  Consecutive chunks may share one line
  (when a long paragraph is split at a sentence boundary in the middle of a
  line), so ``chunks[k].line_start`` can equal ``chunks[k-1].line_end - 1``.
* A chunk with no speakable words (blank runs, horizontal rules, table
  separators) is *kept* -- so the line coverage above holds -- but is flagged
  ``speakable=False`` and must be skipped by playback.

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
:func:`strip_reference_markers` before ``plain`` is derived.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

from .ansi import Run, Style

__all__ = [
    "DEFAULT_MAX_SENTENCES",
    "DEFAULT_MAX_CHARS",
    "ABBREVIATIONS",
    "Word",
    "Chunk",
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
    kind: str = "text"         # "text" | "code" | "blank"

    @property
    def line_span(self) -> tuple[int, int]:
        """``(line_start, line_end)`` -- end exclusive, usable as a slice."""
        return (self.line_start, self.line_end)

    def spans(self) -> list[tuple[int, int]]:
        """(start, end) of every word slot inside ``self.text``."""
        return [(o, o + len(t)) for o, t in zip(self.offsets, self.word_texts)]


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


def extract_words(plain: Sequence[str]) -> list[Word]:
    """Every speakable word of a document, in reading order."""
    words: list[Word] = []
    for line_no, text in enumerate(plain):
        if not text or _REF_DEF_RE.match(text):
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


def _line_groups(plain: Sequence[str]) -> list[tuple[int, int, str]]:
    """Partition the lines into (start, end_exclusive, kind) groups.

    kind is "blank", "code" (fenced or indented -- never split further) or
    "para".  The groups tile the whole document.
    """
    groups: list[tuple[int, int, str]] = []
    n = len(plain)
    i = 0
    prev_blank = True
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


def build_chunks(plain: Sequence[str], words: Sequence[Word],
                 max_sentences: int = DEFAULT_MAX_SENTENCES,
                 max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Group lines into chunks: paragraphs first, then sentence splits.

    Blank runs, horizontal rules and code blocks are kept as chunks so the
    chunk list still covers every line; the ones with nothing to say come back
    with ``speakable=False``.
    """
    if not plain:
        return []
    flat, line_offsets = _flatten(plain)
    nlines = len(plain)

    word_at: list[int] = [line_offsets[w.line] + w.start for w in words]

    # (flat_start, flat_end, kind, line_start, line_end) -- a line range of
    # None means "derive it from the flat span" (paragraph sub-chunks only).
    spans: list[tuple[int, int, str, int | None, int | None]] = []
    for first, stop, kind in _line_groups(plain):
        lo = line_offsets[first]
        hi = line_offsets[stop - 1] + len(plain[stop - 1])
        # A group with nothing to say (blank run, horizontal rule, table
        # separator) is never split: it exists only to keep line coverage.
        has_words = bisect_left(word_at, lo) < bisect_left(word_at, hi)
        if kind == "para" and has_words:
            for a, b in _paragraph_spans(flat, lo, hi, max_sentences, max_chars):
                spans.append((a, b, kind, None, None))
        else:
            spans.append((lo, hi, kind, first, stop))

    chunks: list[Chunk] = []
    wi = 0
    nwords = len(words)
    for a, b, kind, first, stop in spans:
        picked: list[int] = []
        offsets: list[int] = []
        texts: list[str] = []
        while wi < nwords and word_at[wi] < b:
            if word_at[wi] >= a:
                picked.append(words[wi].idx)
                offsets.append(word_at[wi] - a)
                texts.append(words[wi].text)
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


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


class Document:
    """A parsed document: styled lines, plain lines, words and chunks."""

    def __init__(self, lines: Iterable[Sequence[Run]] | None = None, *,
                 max_sentences: int = DEFAULT_MAX_SENTENCES,
                 max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self.max_sentences = int(max_sentences)
        self.max_chars = int(max_chars)
        self.lines: list[list[Run]] = strip_reference_markers(lines or ())
        self.plain: list[str] = ["".join(r.text for r in line)
                                 for line in self.lines]
        self.words: list[Word] = extract_words(self.plain)
        self.chunks: list[Chunk] = build_chunks(
            self.plain, self.words, self.max_sentences, self.max_chars)

        # word index -> (chunk index, slot inside that chunk)
        self._chunk_of: list[int] = [0] * len(self.words)
        self._slot_of: list[int] = [0] * len(self.words)
        for chunk in self.chunks:
            for slot, widx in enumerate(chunk.words):
                self._chunk_of[widx] = chunk.idx
                self._slot_of[widx] = slot

        # line -> the word indices on it, ascending by start offset
        self._line_words: dict[int, list[int]] = {}
        for w in self.words:
            self._line_words.setdefault(w.line, []).append(w.idx)
        self._line_starts: dict[int, list[int]] = {
            line: [self.words[i].start for i in idxs]
            for line, idxs in self._line_words.items()
        }
        self._chunk_line_starts: list[int] = [c.line_start for c in self.chunks]

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
        """The word at ``(line, col)``, or the closest one on that line."""
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

    def chunk_at_line(self, line: int) -> int | None:
        """The first chunk whose line span covers ``line``."""
        if not self.chunks:
            return None
        k = bisect_right(self._chunk_line_starts, line) - 1
        if k < 0:
            return None
        while k > 0 and self.chunks[k - 1].line_end > line:
            k -= 1
        c = self.chunks[k]
        return c.idx if c.line_start <= line < c.line_end else None

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
