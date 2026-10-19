"""readaloud.document's words, sentences and chunk spans as they were before
their Rust port: the oracle for tests/test_core_words.py.

`prose_spans` is the span half of the old `build_chunks`, which the Rust
`prose_spans` replaced.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from typing import Sequence

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



def prose_spans(plain: Sequence[str], pieces: Sequence[tuple[int, int]],
                word_at: Sequence[int], max_sentences: int, max_chars: int
                ) -> list[list[tuple[int, int, str, int, int]]]:
    """The spans the old build_chunks cut each piece outside the tables into."""
    flat, line_offsets = _flatten(plain)
    nlines = len(plain)
    out = []
    for piece_first, piece_stop in pieces:
        spans = []
        for first, stop, kind in _line_groups(plain, piece_first, piece_stop):
            lo = line_offsets[first]
            hi = line_offsets[stop - 1] + len(plain[stop - 1])
            has_words = bisect_left(word_at, lo) < bisect_left(word_at, hi)
            if kind == "para" and has_words:
                for a, b in _paragraph_spans(flat, lo, hi, max_sentences,
                                             max_chars):
                    spans.append((a, b, kind, None, None))
            else:
                spans.append((lo, hi, kind, first, stop))
        done = []
        for a, b, kind, first, stop in spans:
            if first is not None and stop is not None:
                line_start, line_end = first, stop
            else:
                line_start = bisect_right(line_offsets, a) - 1
                line_end = bisect_right(line_offsets, b - 1 if b > a else a)
            line_start = max(0, min(line_start, nlines - 1))
            line_end = max(line_start + 1, min(line_end, nlines))
            done.append((a, b, kind, line_start, line_end))
        out.append(done)
    return out
