"""ANSI escape-sequence parsing for readaloud.

Turns terminal-styled text (primarily ``mdcat --ansi`` output arriving on a pipe)
into logical lines of styled :class:`Run` objects, with every escape sequence
removed and hyperlink URLs preserved in :attr:`Style.href`.

The parser is a character-level state machine.  It tolerates everything a real
terminal producer emits:

* ``CSI`` sequences -- ``ESC [ params intermediates final``.  ``final == 'm'``
  is reduced into the current :class:`Style`; every other final byte (cursor
  moves, erases, private modes such as ``ESC[?1049h``, device attributes
  ``ESC[c``) is consumed and discarded.
* ``OSC`` strings -- ``ESC ] ... ST`` **and** ``ESC ] ... BEL``.  ``OSC 8``
  becomes :attr:`Style.href`; every other OSC (mdcat probes the terminal with
  ``OSC 10`` / ``OSC 11`` when stdout is a tty) is discarded.
* ``DCS`` / ``PM`` / ``APC`` / ``SOS`` strings, charset designations
  (``ESC ( B``, emitted by ncurses itself) and single-character escapes.
* Truncated escapes at end of input, which are dropped rather than crashing.

The scanning and the colour arithmetic are done by :mod:`readaloud._core`
(``rust/src/ansi.rs``), which hands back each line's runs as text and an
index into a table of styles; this module turns those into :class:`Style`
and :class:`Run` objects.

Tabs are expanded to real 4-column tab stops, ``\\r`` is dropped, and the input
is split on ``\\n`` into logical lines -- empty lines are preserved because they
are the paragraph separators the chunker keys off.
"""

from __future__ import annotations

import re
from dataclasses import astuple, dataclass

from . import _core

__all__ = [
    "TAB_WIDTH",
    "Style",
    "Run",
    "DEFAULT_STYLE",
    "parse",
    "has_ansi",
    "strip_markdown",
    "apply_sgr",
    "rgb_to_x256",
    "plain_text",
    "plain_lines",
    "nonspeakable_spans",
    "is_rule_line",
    "is_reference_line",
]

TAB_WIDTH = 4


# --------------------------------------------------------------------------
# contract types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Style:
    """Immutable, hashable rendering state for a stretch of text."""

    fg: int | None = None        # 0-255 palette index, or None = terminal default
    bg: int | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    href: str | None = None      # from OSC 8


@dataclass
class Run:
    """A stretch of text sharing one :class:`Style`.

    ``text`` never contains an escape sequence and never contains a newline.
    """

    text: str
    style: Style


DEFAULT_STYLE = Style()


# --------------------------------------------------------------------------
# colours and SGR
# --------------------------------------------------------------------------


def rgb_to_x256(r: int, g: int, b: int) -> int:
    """Quantise a truecolour triple to the nearest 0-255 xterm palette index.

    ``Style.fg`` / ``Style.bg`` are documented as palette indices, but mdcat's
    non-default themes emit *only* the ``38;2;R;G;B`` truecolour form (~70
    distinct triples for one small document), so every triple is funnelled
    through here before it reaches the style.  That also keeps the curses
    colour-pair budget bounded.  The nearest of the 6x6x6 cube and the grey
    ramp wins, distances weighted 3:6:1 for red, green and blue.
    """
    return _core.rgb_to_x256(int(r), int(g), int(b))


def apply_sgr(style: Style, body: str) -> Style:
    """Apply one ``CSI ... m`` parameter body to ``style``.

    Both colour forms are understood, ``38;5;N`` / ``38;2;R;G;B`` and the
    colon-separated ``38:5:N`` / ``38:2::R:G:B``; an unknown colour space ends
    the body.  ``href`` survives ``SGR 0`` on purpose: it is set by OSC 8, and
    mdcat emits ``ESC[0m`` *inside* a hyperlink.  Strikethrough (SGR 9/29),
    which mdcat does emit, has no field in the Style and is ignored.
    """
    return Style(*_core.apply_sgr(astuple(style), body))


def _styles(tuples: list[tuple]) -> list[Style]:
    """The Style of each tuple _core hands back; the default one is
    :data:`DEFAULT_STYLE` itself."""
    default = astuple(DEFAULT_STYLE)
    return [DEFAULT_STYLE if t == default else Style(*t) for t in tuples]


def _runs(styles: list[Style], lines: list[list[tuple[str, int]]]
          ) -> list[list[Run]]:
    return [[Run(text, styles[k]) for text, k in line] for line in lines]


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------


def parse(data: str) -> list[list[Run]]:
    """Split ``data`` into logical lines, each a list of styled runs.

    Every escape sequence is stripped; hyperlink *text* is kept and its URL is
    recorded in :attr:`Style.href`.  Tabs expand to 4-column tab stops and
    ``\\r``, the other C0 controls, DEL and the C1 controls are dropped.  Empty
    lines survive as empty lists.  Adjacent runs never share a style.
    """
    if not data:
        return []
    styles, lines = _core.parse(data)
    return _runs(_styles(styles), lines)


def has_ansi(data: str) -> bool:
    """True when ``data`` carries at least one plausible escape sequence."""
    return _core.has_ansi(data)


# --------------------------------------------------------------------------
# plain-text helpers
# --------------------------------------------------------------------------


def plain_text(line) -> str:
    """Flatten one logical line (a list of runs, or a plain string) to text."""
    if isinstance(line, str):
        return line
    return "".join(r.text for r in line)


def plain_lines(lines: list[list[Run]]) -> list[str]:
    """Flatten a parsed document to one plain string per logical line."""
    return [plain_text(ln) for ln in lines]


# --------------------------------------------------------------------------
# strip_markdown
# --------------------------------------------------------------------------


def strip_markdown(lines: list[list[Run]]) -> list[list[Run]]:
    """Lightly de-syntax raw markdown, keeping run structure.

    Only meaningful when :func:`has_ansi` is False.  Drops ``#``/``*``/``_``/
    ```` ` ````/``>`` syntax characters, unwraps ``[text](url)`` to ``text``
    (recording the URL in :attr:`Style.href`), turns list markers into ``\u2022``
    and thematic breaks / table separators into box-drawing rules, and blanks
    code-fence delimiters while leaving fenced content verbatim.  Text inside a
    code span, an escape, an autolink or a bare URL is never taken for
    emphasis.

    ``mdcat``'s degraded (non-tty, no ``--ansi``) output -- ``text[1]`` markers
    plus a trailing ``[1]: https://...`` block -- passes through untouched and
    stays visible; use :func:`nonspeakable_spans` to keep it out of the audio.
    """
    index: dict[Style, int] = {}
    order: list[Style] = []
    coded = []
    for line in lines:
        runs = []
        for r in line:
            k = index.get(r.style)
            if k is None:
                k = index[r.style] = len(order)
                order.append(r.style)
            runs.append((r.text, k))
        coded.append(runs)
    tuples, out = _core.strip_markdown([astuple(st) for st in order], coded)
    styles = order + _styles(tuples[len(order):])
    return _runs(styles, out)


# --------------------------------------------------------------------------
# speakability helpers (the "companion the chunker can use")
#
# These are deliberately module-level functions rather than extra fields on
# Run/Style, so the Run/Style contract is untouched.
# --------------------------------------------------------------------------

_DECOR_CHARS = (
    "\u2022\u00b7\u2023\u25aa\u25ab\u25e6\u2219\u2043\u2219"     # bullets
    "\u2502\u2503\u2506\u2507\u250a\u250b\u254e\u254f\u2551"     # vertical bars
    "\u2500\u2501\u2504\u2505\u2508\u2509\u254c\u254d\u2550"     # horizontals
    "\u250c\u250d\u250e\u250f\u2510\u2513\u2514\u2517\u2518\u251b"
    "\u251c\u2523\u2524\u252b\u252c\u2533\u2534\u253b\u253c\u254b"
    "\u256d\u256e\u256f\u2570\u2554\u2557\u255a\u255d\u2560\u2563"
    "\u2566\u2569\u256c"
    "\u2580\u2584\u2588\u258c\u2590\u2591\u2592\u2593\u2594\u2581"
    "\u25cf\u25cb\u25a0\u25a1\u2500"
    "|"
)
_RULE_EXTRA = "-=_*~+ \t"

_RE_DECOR_RUN = re.compile("[" + re.escape(_DECOR_CHARS) + "]+")
_RE_REF_MARKER = re.compile(r"\[\d{1,4}\]")
_RE_REF_DEF = re.compile(r"^[ \t]*\[[^\]]{1,40}\]:[ \t]*\S+")
_RE_URL = re.compile(
    r"(?:https?|ftp|file|data):\S+|\bmailto:\S+|\bwww\.[^\s]+")


def is_reference_line(line) -> bool:
    """True for a link-reference definition such as ``[1]: https://example.com``.

    These are what ``mdcat`` appends when it is not writing to a tty.  They
    should stay visible but must never be spoken.
    """
    return _RE_REF_DEF.match(plain_text(line)) is not None


def is_rule_line(line) -> bool:
    """True for a line that is purely decoration (a horizontal rule, a table
    separator, a run of blockquote bars) and so has nothing to say."""
    text = plain_text(line).strip()
    if not text:
        return False
    has_decor = False
    for ch in text:
        if ch in _DECOR_CHARS:
            has_decor = True
        elif ch in _RULE_EXTRA:
            continue
        else:
            return False
    if has_decor:
        return True
    # A raw-markdown thematic break: --- / *** / ___ / === (3 or more).
    stripped = "".join(text.split())
    return len(stripped) >= 3 and len(set(stripped)) == 1


def nonspeakable_spans(line) -> list[tuple[int, int]]:
    """Char spans of one logical line that the TTS should skip.

    Offsets index the flattened plain text of the line (the same coordinate
    space as ``Document.plain`` / ``Word.start``).  Covered: box-drawing rules,
    bullets and quote bars, table pipes, bare URLs, ``mdcat``'s ``[1]`` link
    markers, and the whole of a ``[1]: https://...`` reference definition.

    Returned spans are sorted and non-overlapping.
    """
    text = plain_text(line)
    if not text:
        return []
    if is_reference_line(line):
        return [(0, len(text))]

    spans: list[tuple[int, int]] = []
    for rx in (_RE_DECOR_RUN, _RE_URL, _RE_REF_MARKER):
        for m in rx.finditer(text):
            if m.end() > m.start():
                spans.append((m.start(), m.end()))
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for s, e in spans[1:]:
        ls, le = merged[-1]
        if s <= le:
            if e > le:
                merged[-1] = (ls, e)
        else:
            merged.append((s, e))
    return merged
