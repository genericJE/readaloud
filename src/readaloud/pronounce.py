"""readaloud.pronounce -- say a word or phrase the way the config file says.

The ``[pronunciations]`` section of the config file pairs text as written
with how to say it (``id = ID``, ``kubectl = cube control``).  :class:`Lexicon`
finds that text in a chunk and :func:`respell` rewrites the chunk, so the TTS
hears the respelling while every word slot still highlights its own word.

A respelling is plain text, never phonemes: misaki reads ``id`` as Freud's id
and ``ID`` as "eye dee", so ``id = ID`` is all it takes.  misaki's
``[word](/phonemes/)`` links are not used, because one lands on the wrong word
after an earlier newline, indentation or double space in the chunk.

Matching (:meth:`Lexicon.find`):

* A key is literal text, never a regex.
* A key that starts with a letter or digit starts at a break, and one that
  ends with a letter or digit ends at one.  A break is the edge of the text, a
  neighbour that is not a letter or digit (underscore, dot, hyphen, slash,
  space and every other punctuation mark count), or an ASCII camelCase hump.
  So ``id`` matches ``foo.id``, ``user_id``, ``userId``, ``idToken`` and
  ``(id)``, but not ``idle``, ``grid``, ``ids`` or ``id2``.  A key edge that is
  a symbol needs no break: ``.NET`` matches in ``ASP.NET``, ``->`` in ``a->b``.
* Smart case, like the reader's search: a key with a capital matches that
  case only, a key without one matches any case.
* Each space in a key matches any run of spaces and tabs in the text holding
  at most one line break, so a phrase may wrap but never spans a paragraph.
  The │ gutter of a blockquote rendered by mdcat may follow the break.
  Punctuation inside a key must be there as written.
* Keys are NFC; text spelled with combining accents (NFD) matches too.
* Scanning goes left to right.  Where anything matches, the longest match
  wins, then a key with a capital, then the later pair.  The scan resumes
  after it, so a respelling is never read again ("id = id card" is safe).
* An entry that says its own text (``macOS = macOS``) is a guard: it claims
  what it matches but changes nothing, which keeps a shorter key (``OS = O
  S``) off it.  A lowercase guard keeps every case it matches as written, so
  ``macos = macos`` leaves "macOS" alone rather than lowering it.
* A symbol a table cell says by name (a tick said "yes") is matched by what is
  on screen: the name is never matched as text, so ``yes = yep`` leaves ticks
  alone, while ``✓ = check`` replaces the whole name.

Respelling (:func:`respell`) keeps the chunk's word slots usable: each stays
non-empty and in order, the slots a match covers share its respelling, and a
space goes in where a respelling would run into a letter or digit.

Every key is indexed by its first run of letters and digits, or by its first
character when that is a symbol, so the text costs a dict probe per word
rather than a pass per key.  One alternation of every key took 20 s for 500
keys over a 5000 line document; the index takes under 0.2 s.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = ["Lexicon", "respell"]

# A letter or digit (unicode aware, underscore excluded) or a combining mark:
# what document.py builds its words from.
_ALNUM = r"(?:[^\W_]|[\u0300-\u036f])"
_IS_ALNUM = re.compile(_ALNUM)
_RUN = re.compile(_ALNUM + "+")
# the [A-Z] of a camelCase hump: userId breaks before the I
_HUMP = re.compile(r"(?<=[a-z])[A-Z]")
# a run of spaces and tabs holding at most one line break, and after the break
# the │ gutter mdcat draws down a blockquote; atomic, so a failed phrase never
# backtracks through a long run of blanks
_GAP = r"(?=\s)(?>[^\S\n]*)(?:\n(?>[^\S\n]*(?:│[^\S\n]*)*))?"
# after a key that ends in a letter or digit: no letter or digit follows, or a
# camelCase hump does
_END = r"(?:(?![^\W_]|[\u0300-\u036f])|(?<=[a-z])(?=[A-Z]))"
_PIECE = re.compile(r"\S+")
# text (U+FE0E) and emoji (U+FE0F) presentation: the same symbol either way
_VARIATION_SELECTORS = str.maketrans("", "", "\ufe0e\ufe0f")


@dataclass(frozen=True)
class _Entry:
    key: str             # the text as written, NFC, spaces collapsed
    say: str             # how to say it
    rank: int            # position among the pairs: the later pair wins a tie
    exact: bool          # a capital in the key: match that case only


def _alnum(ch: str) -> bool:
    """Whether `ch`, one character or none, is a letter or digit."""
    return bool(ch) and _IS_ALNUM.match(ch) is not None


def _forms(key: str) -> list[str]:
    """The key as written (NFC) and with its accents decomposed (NFD)."""
    return list(dict.fromkeys((key, unicodedata.normalize("NFD", key))))


class Lexicon:
    """The pronunciations from the config file, ready to be found in text.

    `pairs` are (written, spoken) in file order.  config.py has already
    checked them; a pair with an empty side is skipped all the same.
    """

    def __init__(self, pairs: Iterable[tuple[str, str]] = ()) -> None:
        self._entries: list[_Entry] = []
        # the first run of letters and digits of every form of every key that
        # starts with one: as written for exact keys, lowercased for the rest
        self._runs: dict[str, list[int]] = {}
        self._folded_runs: dict[str, list[int]] = {}
        self._lengths: set[int] = set()
        # the first character of every form that starts with a symbol
        self._leads: dict[str, list[int]] = {}
        self._folded_leads: dict[str, list[int]] = {}
        # a key without its variation selectors -> the entry a symbol forces
        self._symbols: dict[str, _Entry] = {}
        self._patterns: dict[int, re.Pattern[str]] = {}
        for written, spoken in pairs:
            key = " ".join(unicodedata.normalize("NFC", written).split())
            say = " ".join(spoken.split())
            if not key or not say:
                continue
            i = len(self._entries)
            entry = _Entry(key, say, i, any(ch.isupper() for ch in key))
            self._entries.append(entry)
            self._symbols[key.translate(_VARIATION_SELECTORS)] = entry
            for form in _forms(key):
                run = _RUN.match(form)
                if run:
                    unit = run.group()
                    self._lengths.add(len(unit))
                    index = self._runs if entry.exact else self._folded_runs
                else:
                    unit = form[0]
                    index = self._leads if entry.exact else self._folded_leads
                bucket = index.setdefault(
                    unit if entry.exact else unit.lower(), [])
                if not bucket or bucket[-1] != i:
                    bucket.append(i)
        # every character a symbol led key may start with, in any case the
        # key allows
        leads = set(self._leads)
        for ch in self._folded_leads:
            leads.update(v for v in (ch, ch.upper(), ch.title())
                         if len(v) == 1)
        self._lead = (re.compile("[" + "".join(map(re.escape, sorted(leads)))
                                 + "]") if leads else None)
        self._sorted_lengths = sorted(self._lengths)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def find(self, text: str, symbols: Sequence[tuple[int, int, str]] = ()
             ) -> list[tuple[int, int, str]]:
        """Every ``(start, end, say)`` to respell in `text`.

        The matches are sorted, never empty and never overlap.

        `symbols` lists ``(start, end, symbol)`` for each slot that says a
        symbol by its name: where the name sits in `text` and the symbol shown
        on screen, in order and never overlapping.  No match of text may
        overlap a name; an entry whose key is the symbol replaces the whole
        name instead.
        """
        if not self._entries:
            return []
        names = sorted((s, e, symbol) for s, e, symbol in symbols if s < e)
        name_ends = [e for _s, e, _symbol in names]
        found: list[tuple[int, int, str]] = []
        pos = 0
        for p, ids in self._candidates(text):
            if p < pos:
                continue
            # the first name that ends after p: a match may not reach into it
            k = bisect_right(name_ends, p)
            name_at = names[k][0] if k < len(names) else len(text)
            best: tuple[tuple[int, bool, int], _Entry] | None = None
            for i in ids:
                m = self._pattern(i).match(text, p)
                if m is None or m.end() <= p or name_at < m.end():
                    continue
                entry = self._entries[i]
                rank = (m.end(), entry.exact, entry.rank)
                if best is None or rank > best[0]:
                    best = (rank, entry)
            if best is None:
                continue
            end, entry = best[0][0], best[1]
            # a guard, or text already said this way: claimed, not changed
            if entry.say != entry.key and text[p:end] != entry.say:
                found.append((p, end, entry.say))
            pos = end
        forced = []
        for s, e, symbol in names:
            entry = self._symbols.get(symbol.translate(_VARIATION_SELECTORS))
            if entry is not None and text[s:e] != entry.say:
                forced.append((s, e, entry.say))
        return sorted(found + forced) if forced else found

    def _candidates(self, text: str) -> list[tuple[int, list[int]]]:
        """(position, entries) of every place a key may start, in order.

        A key that starts with a letter or digit starts at a break, which
        before a letter or digit is the start of a run of them or a camelCase
        hump inside one, and the first run of the key ends at the end of that
        run or at a later hump.  Only the lengths some key has are looked up,
        so a hostile run like "aBaBaB..." costs a probe per hump per length.
        """
        found: dict[int, list[int]] = {}
        if self._lengths:
            humps = [m.start() for m in _HUMP.finditer(text)]
            h, nh = 0, len(humps)
            for run in _RUN.finditer(text):
                a, b = run.span()
                if h == nh or humps[h] >= b:
                    if b - a in self._lengths:
                        self._look(text, a, b, found)
                    continue
                k = h
                while k < nh and humps[k] < b:
                    k += 1
                inner, h = humps[h:k], k
                stops = set(inner)
                stops.add(b)
                for p in [a] + inner:
                    for n in self._sorted_lengths:
                        q = p + n
                        if q > b:
                            break
                        if q in stops:
                            self._look(text, p, q, found)
        if self._lead is not None:
            for m in self._lead.finditer(text):
                ch = m.group()
                ids = (self._leads.get(ch, [])
                       + self._folded_leads.get(ch.lower(), []))
                if ids:
                    found.setdefault(m.start(), []).extend(ids)
        return sorted(found.items())

    def _look(self, text: str, p: int, q: int,
              found: dict[int, list[int]]) -> None:
        """Add the entries whose first run is ``text[p:q]`` at `p`."""
        unit = text[p:q]
        ids = (self._runs.get(unit, [])
               + self._folded_runs.get(unit.lower(), []))
        if ids:
            found.setdefault(p, []).extend(ids)

    def _pattern(self, i: int) -> re.Pattern[str]:
        """Entry `i` as a regex, compiled the first time it is needed."""
        rx = self._patterns.get(i)
        if rx is None:
            entry = self._entries[i]
            alternatives = []
            for form in _forms(entry.key):
                body = _GAP.join(map(re.escape, form.split()))
                if not entry.exact:
                    body = "(?i:" + body + ")"
                if _IS_ALNUM.match(form[-1]):
                    body += _END
                alternatives.append(body)
            rx = self._patterns[i] = re.compile("|".join(alternatives))
        return rx


def _share(say: str, k: int) -> tuple[list[tuple[int, int]], int]:
    """How `k` slots divide `say`: each slot's (start, end) inside it, and how
    many padding spaces to append so that every slot has a character.

    With a word for each slot, each takes one and the last takes the rest.
    With fewer words, the last word is cut into characters for the slots left
    over, longer parts first, and a slot with no character left gets a space.
    """
    if k == 0:
        return [], 0
    pieces = [m.span() for m in _PIECE.finditer(say)]
    m = len(pieces)
    if m >= k:
        return pieces[:k - 1] + [(pieces[k - 1][0], pieces[-1][1])], 0
    spans = pieces[:m - 1] if m else []
    p, q = pieces[-1] if m else (len(say), len(say))
    c = k - len(spans)
    if q - p >= c:
        width, extra = divmod(q - p, c)
        for t in range(c):
            w = width + (t < extra)
            spans.append((p, p + w))
            p += w
        return spans, 0
    spans += [(x, x + 1) for x in range(p, q)]
    pad = c - (q - p)
    spans += [(len(say) + t, len(say) + t + 1) for t in range(pad)]
    return spans, pad


def respell(text: str, slots: Sequence[tuple[int, int]],
            matches: Sequence[tuple[int, int, str]]
            ) -> tuple[str, list[int], list[str]]:
    """`text` with every match said its way: ``(text, offsets, word_texts)``.

    `slots` are the ``(start, end)`` of the chunk's word slots in `text`, in
    order, never empty and never overlapping; `matches` come from
    :meth:`Lexicon.find`.  Every slot comes back the same way: never empty,
    in order, and ``new[offsets[i]:][:len(word_texts[i])] == word_texts[i]``
    in the new text.

    * A slot that overlaps no match keeps its exact text.
    * The slots a match overlaps share its respelling a word each, the last
      one taking the rest ("kubectl" is one slot saying "cube control").  With
      more slots than words the last word is cut ("New York City = NYC" says
      N, Y and C); a slot with nothing left says a space.
    * A slot keeps what lies outside the match: "foo.id" says "foo.ID".
    * A space goes in where a respelling would run into a letter or digit, so
      "ASP.NET" with ``.NET = dot net`` says "ASP dot net", and "userId" with
      ``user = yoozer`` and ``id = ID`` says "yoozer ID".
    """
    if not matches:
        return text, [a for a, _b in slots], [text[a:b] for a, b in slots]
    starts = [s for s, _e, _say in matches]
    ends = [e for _s, e, _say in matches]
    slot_starts = [a for a, _b in slots]
    slot_ends = [b for _a, b in slots]

    parts: list[str] = []
    size = 0
    after: list[int] = []     # where the text after each match lands
    shares: list[tuple[int, list[tuple[int, int]]]] = []
    cursor = 0
    for j, (s, e, say) in enumerate(matches):
        gap = text[cursor:s]
        parts.append(gap)
        size += len(gap)
        first = bisect_right(slot_ends, s)
        spans, pad = _share(say, bisect_left(slot_starts, e, first) - first)
        said = say + " " * pad
        # right after another match there is no gap: that match's own look
        # at this respelling already put in the space
        if _alnum(said[:1]) and _alnum(gap[-1:]):
            said = " " + said
            spans = [(x + 1, y + 1) for x, y in spans]
        if j + 1 < len(matches) and starts[j + 1] == e:
            follows = matches[j + 1][2][:1]     # the next respelling
        else:
            follows = text[e:e + 1]
        if _alnum(said[-1:]) and _alnum(follows):
            said += " "
        shares.append((first, [(size + x, size + y) for x, y in spans]))
        parts.append(said)
        size += len(said)
        after.append(size)
        cursor = e
    parts.append(text[cursor:])
    new = "".join(parts)

    def moved(x: int) -> int:
        """Where position `x`, outside every match, lands in the new text."""
        j = bisect_right(ends, x) - 1
        return x if j < 0 else after[j] + x - ends[j]

    offsets: list[int] = []
    word_texts: list[str] = []
    for i, (a, b) in enumerate(slots):
        j = bisect_right(starts, a) - 1
        if j >= 0 and a < ends[j]:
            first, spans = shares[j]
            x = spans[i - first][0]
        else:
            x = moved(a)
        j = bisect_left(starts, b) - 1
        if j >= 0 and b <= ends[j]:
            first, spans = shares[j]
            y = spans[i - first][1]
        else:
            y = moved(b)
        offsets.append(x)
        word_texts.append(new[x:y])
    return new, offsets, word_texts
