r"""readaloud.pronounce -- say a word or phrase the way the config file says.

The ``[pronunciations]`` section of the config file pairs text as written
with how to say it (``id = ID``, ``kubectl = cube control``).  :class:`Lexicon`
finds that text in a chunk and :func:`respell` rewrites the chunk, so the TTS
hears the respelling while every word slot still highlights its own word.

A respelling is plain text, never phonemes: misaki reads ``id`` as Freud's id
and ``ID`` as "eye dee", so ``id = ID`` is all it takes.  misaki's
``[word](/phonemes/)`` links are not used, because one lands on the wrong word
after an earlier newline, indentation or double space in the chunk.

Matching (:meth:`Lexicon.find`):

* A key is literal text, unless it is a pattern (below).
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
space goes in where a respelling would run into a letter, a digit or an
underscore.

A key between slashes is a pattern, a regular expression for the Rust
``regex`` crate, and its pronunciation may name what the pattern captured:
``/:(\d+)-(\d+)/ = lines $1 to $2`` says ``settings.py:281-283`` as
"settings.py lines 281 to 283".  ``$1``, ``${1}`` and ``${name}`` are a group,
``$$`` a dollar sign.  A pattern matches as written: no break rule, no smart
case and no accent forms, so it says what it means with ``\b`` and ``(?i)``.
It takes part in the same left to right scan as the literal keys and ranks
like a key with a capital.  A match that says nothing (no letter or digit
once its groups are filled in) is passed over, and one that says exactly what
it matched is a guard.  The ``regex`` crate never backtracks, so no pattern
can stall the reader; it has no lookaround and no backreferences.  A pattern
that does not compile, or that could match empty text, is skipped (config.py
reports it).  A key that only looks like a pattern is kept literal by making
it a :class:`Literal`, which is what config.py does for a quoted key.

Shapes are text the config file's ``[shapes]`` describes with a pattern and
says a set way: a file with its line (``settings.py:498``), a host with its
port.  readaloud has none of its own.  A shape must start where a word starts
and end where a word ends, so it sees both sides of the colon, and it may
span a few words.  It says its wording with the pattern's named groups in
place (``$file line $line``).  The lines are tried in file order at each
word, and the first that fits wins.  It loses to any entry of the
same length.  An empty wording says the word as written, which keeps a
pattern off it.

What a shape or a pattern says goes through the literal entries once more,
so ``.py = dot pie`` still applies inside ``$file line $line``.  What the
literal entries say is final, so nothing is read more than twice.

The work is done by :mod:`readaloud._core` (``rust/src/pronounce.rs`` and
``rust/src/shapes.rs``).
Every literal key is indexed by its first run of letters and digits, or by
its first character when that is a symbol, so the text costs a hash probe per
word rather than a pass per key, and each pattern keeps a cursor on its next
match, so it scans the text about once.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from . import _core

__all__ = ["Lexicon", "Literal", "check_pattern", "check_regex",
           "check_shape", "is_pattern", "respell"]


class Literal(str):
    """A key that is literal text even though it looks like a pattern: the
    quoted ``"/usr/"`` of ``"/usr/" = user``."""

    __slots__ = ()


def is_pattern(key: str) -> bool:
    """Whether `key` is a pattern: ``/.../`` with something between the
    slashes, and not a :class:`Literal`."""
    return (not isinstance(key, Literal) and len(key) >= 3
            and key[0] == "/" and key[-1] == "/")


def check_pattern(pattern: str) -> str | None:
    """Why `pattern` (without its slashes) cannot be used, or None."""
    return _core.check_pattern(pattern)


def check_regex(pattern: str) -> str | None:
    """Why `pattern` does not compile, or None; it may match empty text."""
    return _core.check_regex(pattern)


def check_shape(pattern: str) -> tuple[str | None, tuple[list[str], int] | None]:
    """``(problem, None)`` when `pattern` cannot be a shape, else ``(None,
    (group names, group count))``."""
    return _core.check_shape(pattern)


class Lexicon:
    """The pronunciations from the config file, ready to be found in text.

    `pairs` are (written, spoken) in file order.  config.py has already
    checked them; a pair with an empty side, or a pattern that does not
    compile, is skipped all the same.  `shapes` are (pattern, wording) in
    file order, the patterns without slashes and with their parts filled in;
    none by default.  A shape is only looked for at the word slots handed to
    :meth:`find`.  `subshapes` are the named shapes, (name, pattern,
    wording): never found on their own, they say each of their matches in
    a group whose wording calls on them as ``${group|name}``.  `grammar`
    (a `readaloud.syntax.Grammar`) and `rules`, (node kind, wording), say a
    source file's syntax tree, once :meth:`parse` has been given the file.
    """

    def __init__(self, pairs: Iterable[tuple[str, str]] = (),
                 shapes: Iterable[tuple[str, str]] = (),
                 subshapes: Iterable[tuple[str, str, str]] = (),
                 grammar=None, rules: Iterable[tuple[str, str]] = ()) -> None:
        self._core = _core.Lexicon(
            [(written, spoken, is_pattern(written)) for written, spoken in pairs],
            [(pattern, say) for pattern, say in shapes],
            [(name, pattern, say) for name, pattern, say in subshapes],
            grammar, [(kind, say) for kind, say in rules])

    @property
    def shapes(self) -> int:
        """How many shapes are on."""
        return self._core.shape_count()

    @property
    def uses_slots(self) -> bool:
        """Whether :meth:`find` wants the word slots: there are shapes, or
        a grammar."""
        return self._core.uses_slots()

    def parse(self, text: str) -> bool:
        """Parse `text`, the whole document, with the grammar, for the chunks
        :meth:`find` is then given with their place in it (`at`).  False
        without a grammar, or when it could not parse."""
        return self._core.parse(text)

    def __bool__(self) -> bool:
        return len(self._core) > 0 or self.shapes > 0

    def __len__(self) -> int:
        return len(self._core)

    def find(self, text: str, symbols: Sequence[tuple[int, int, str]] = (),
             slots: Sequence[tuple[int, int]] = (), at: int | None = None
             ) -> list[tuple[int, int, str]]:
        """Every ``(start, end, say)`` to respell in `text`.

        The matches are sorted, never empty and never overlap.

        `symbols` lists ``(start, end, symbol)`` for each slot that says a
        symbol by its name: where the name sits in `text` and the symbol shown
        on screen, in order and never overlapping.  No match of text may
        overlap a name; an entry whose key is the symbol replaces the whole
        name instead.

        `slots` are the ``(start, end)`` of the word slots in `text`; a shape
        is matched against a whole slot, so without slots there is none.
        `at` is where `text` starts in the text :meth:`parse` was given,
        without which the grammar's rules say nothing.
        """
        return self._core.find(text, list(symbols), list(slots), at)


def respell(text: str, slots: Sequence[tuple[int, int]],
            matches: Sequence[tuple[int, int, str]]
            ) -> tuple[str, list[int], list[str]]:
    """`text` with every match said its way: ``(text, offsets, word_texts)``.

    `slots` are the ``(start, end)`` of the chunk's word slots in `text`, in
    order, never empty and never overlapping; `matches` come from
    :meth:`Lexicon.find`.  Every slot comes back the same way: never empty,
    in order, and ``new[offsets[i]:][:len(word_texts[i])] == word_texts[i]``
    in the new text.  ValueError when the slots and matches do not fit.

    * A slot that overlaps no match keeps its exact text.
    * The slots a match overlaps share its respelling a word each, the last
      one taking the rest ("kubectl" is one slot saying "cube control").  With
      more slots than words the last word is cut ("New York City = NYC" says
      N, Y and C); a slot with nothing left says a space.
    * A slot keeps what lies outside the match: "foo.id" says "foo.ID".
    * A space goes in where a respelling would run into a letter, a digit or
      an underscore, so "ASP.NET" with ``.NET = dot net`` says "ASP dot net",
      "userId" with ``user = yoozer`` and ``id = ID`` says "yoozer ID", and
      "subscription_id" says "subscription_ ID" rather than one long token
      misaki reads as Freud's id.
    """
    return _core.respell(text, list(slots), list(matches))
