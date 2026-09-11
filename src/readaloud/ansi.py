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

Tabs are expanded to real 4-column tab stops, ``\\r`` is dropped, and the input
is split on ``\\n`` into logical lines -- empty lines are preserved because they
are the paragraph separators the chunker keys off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

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
# colour helpers
# --------------------------------------------------------------------------

_ANSI16_RGB: tuple[tuple[int, int, int], ...] = (
    (0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
    (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229),
    (127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)
_CUBE = (0, 95, 135, 175, 215, 255)


def _xterm_rgb(idx: int) -> tuple[int, int, int]:
    idx = 0 if idx < 0 else (255 if idx > 255 else idx)
    if idx < 16:
        return _ANSI16_RGB[idx]
    if idx < 232:
        n = idx - 16
        return (_CUBE[n // 36], _CUBE[(n // 6) % 6], _CUBE[n % 6])
    v = 8 + 10 * (idx - 232)
    return (v, v, v)


def _dist(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return 3 * dr * dr + 6 * dg * dg + db * db      # BT.601-ish weighting


def rgb_to_x256(r: int, g: int, b: int) -> int:
    """Quantise a truecolour triple to the nearest 0-255 xterm palette index.

    ``Style.fg`` / ``Style.bg`` are documented as palette indices, but mdcat's
    non-default themes emit *only* the ``38;2;R;G;B`` truecolour form (~70
    distinct triples for one small document), so every triple is funnelled
    through here before it reaches the style.  That also keeps the curses
    colour-pair budget bounded.
    """
    def q(v: int) -> int:
        v = 0 if v < 0 else (255 if v > 255 else int(v))
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))

    r, g, b = int(r), int(g), int(b)
    cube = 16 + 36 * q(r) + 6 * q(g) + q(b)
    grey = 232 + max(0, min(23, int(round((((r + g + b) / 3) - 8) / 10))))
    target = (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))
    return min((cube, grey), key=lambda i: _dist(_xterm_rgb(i), target))


# --------------------------------------------------------------------------
# SGR reduction
# --------------------------------------------------------------------------


def _to_int(field: str) -> int:
    # ECMA-48: an omitted (or empty) parameter takes its default value, 0.
    return int(field) if field.isdigit() else 0


def _sgr_ops(body: str) -> list[int | tuple[str, int | None]]:
    """Split an SGR parameter body into ops.

    An op is either a plain integer parameter or a pre-resolved colour
    ``(which, index)`` pair produced by the colon-separated ISO 8613-6 form
    (``38:5:196`` / ``38:2::R:G:B``), which cannot be expressed as a flat
    integer list.
    """
    ops: list[int | tuple[str, int | None]] = []
    for field in body.split(";"):
        if ":" not in field:
            ops.append(_to_int(field))
            continue
        subs = field.split(":")
        head = _to_int(subs[0])
        if head not in (38, 48):
            continue                      # 58 (underline colour) and friends
        which = "fg" if head == 38 else "bg"
        mode = _to_int(subs[1]) if len(subs) > 1 else 0
        if mode == 5 and len(subs) > 2:
            ops.append((which, _to_int(subs[2]) & 0xFF))
        elif mode == 2:
            nums = [_to_int(s) for s in subs[2:]]
            if len(nums) >= 4:            # 38:2:<colour-space-id>:R:G:B
                nums = nums[1:4]
            elif len(nums) == 3:          # 38:2:R:G:B
                pass
            else:
                continue
            ops.append((which, rgb_to_x256(*nums)))
    return ops


def _int_at(ops: list, k: int) -> int | None:
    if 0 <= k < len(ops):
        v = ops[k]
        if isinstance(v, int):
            return v
    return None


def apply_sgr(style: Style, body: str) -> Style:
    """Apply one ``CSI ... m`` parameter body to ``style``.

    ``href`` survives ``SGR 0`` on purpose: it is set by OSC 8, and mdcat emits
    ``ESC[0m`` *inside* a hyperlink.
    """
    ops = _sgr_ops(body) if body else [0]
    if not ops:
        return style
    i = 0
    n = len(ops)
    while i < n:
        op = ops[i]
        if isinstance(op, tuple):
            style = replace(style, **{op[0]: op[1]})
            i += 1
            continue
        p = op
        if p == 0:
            style = Style(href=style.href)
        elif p == 1:
            style = replace(style, bold=True)
        elif p == 2:
            style = replace(style, dim=True)
        elif p == 3:
            style = replace(style, italic=True)
        elif p in (4, 21):                       # 21 = double underline
            style = replace(style, underline=True)
        elif p == 7:
            style = replace(style, reverse=True)
        elif p == 22:
            style = replace(style, bold=False, dim=False)
        elif p == 23:
            style = replace(style, italic=False)
        elif p == 24:
            style = replace(style, underline=False)
        elif p == 27:
            style = replace(style, reverse=False)
        elif 30 <= p <= 37:
            style = replace(style, fg=p - 30)
        elif p == 39:
            style = replace(style, fg=None)
        elif 40 <= p <= 47:
            style = replace(style, bg=p - 40)
        elif p == 49:
            style = replace(style, bg=None)
        elif 90 <= p <= 97:
            style = replace(style, fg=p - 90 + 8)
        elif 100 <= p <= 107:
            style = replace(style, bg=p - 100 + 8)
        elif p in (38, 48):
            which = "fg" if p == 38 else "bg"
            mode = _int_at(ops, i + 1)
            if mode == 5:
                idx = _int_at(ops, i + 2)
                if idx is not None:
                    style = replace(style, **{which: idx & 0xFF})
                i += 2
            elif mode == 2:
                r, g, b = _int_at(ops, i + 2), _int_at(ops, i + 3), _int_at(ops, i + 4)
                if None not in (r, g, b):
                    style = replace(style, **{which: rgb_to_x256(r, g, b)})
                i += 4
            else:
                break                            # unknown colour space: bail out
        # Everything else is deliberately ignored, notably SGR 9/29
        # (strikethrough), which mdcat *does* emit but which has no field in the
        # Style contract, no curses attribute and no terminfo capability here.
        i += 1
    return style


# --------------------------------------------------------------------------
# escape-sequence scanner
# --------------------------------------------------------------------------

_NOCHANGE = object()

# Bytes that introduce a string escape whose payload runs to ST/BEL.
_STRING_INTRO = "]P^_X"
# ESC ( B  and friends: 94/96-character set designation.
_CHARSET_INTRO = "()*+-./%"


def _scan_escape(data: str, i: int) -> tuple[int, str, str]:
    """Consume the escape sequence starting at ``data[i] == ESC``.

    Returns ``(next_index, kind, payload)`` where ``kind`` is ``"sgr"`` (payload
    is the parameter body), ``"osc"`` (payload is the OSC string) or ``""``
    (nothing to do -- the sequence was consumed and discarded).
    """
    n = len(data)
    j = i + 1
    if j >= n:
        return n, "", ""                              # bare ESC at end of input
    c = data[j]

    if c == "[":                                      # CSI
        j += 1
        p_start = j
        while j < n and "\x30" <= data[j] <= "\x3f":   # parameter bytes
            j += 1
        p_end = j
        while j < n and "\x20" <= data[j] <= "\x2f":   # intermediate bytes
            j += 1
        if j >= n:
            return n, "", ""                          # truncated at end of input
        final = data[j]
        j += 1
        if final == "m":
            body = data[p_start:p_end]
            # A private-parameter CSI (ESC[?...m, ESC[>...m) is not SGR.
            if body[:1] in ("<", "=", ">", "?"):
                return j, "", ""
            return j, "sgr", body
        return j, "", ""

    if c in _STRING_INTRO:                            # OSC / DCS / PM / APC / SOS
        kind = "osc" if c == "]" else ""
        j += 1
        start = j
        while j < n:
            ch = data[j]
            if ch == "\x07":                          # BEL terminator
                return j + 1, kind, data[start:j]
            if ch == "\x1b":
                if j + 1 < n and data[j + 1] == "\\":  # ST terminator
                    return j + 2, kind, data[start:j]
                # A fresh escape inside the string: the producer never closed
                # it. End the string here and re-dispatch on the ESC.
                return j, kind, data[start:j]
            j += 1
        return n, "", ""                              # unterminated: swallow it

    if c in _CHARSET_INTRO:
        return min(n, j + 2), "", ""

    return j + 1, "", ""                              # ESC 7, ESC =, ESC c, ...


def _osc8_href(payload: str):
    """Return the href an ``OSC 8`` payload sets, or ``_NOCHANGE``."""
    if not payload.startswith("8;"):
        return _NOCHANGE
    _params, _sep, uri = payload[2:].partition(";")
    uri = uri.strip()
    return uri or None


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------


def parse(data: str) -> list[list[Run]]:
    """Split ``data`` into logical lines, each a list of styled runs.

    Every escape sequence is stripped; hyperlink *text* is kept and its URL is
    recorded in :attr:`Style.href`.  Tabs expand to 4-column tab stops and
    ``\\r`` is dropped.  Empty lines survive as empty lists.
    """
    if not data:
        return []

    lines: list[list[Run]] = []
    cur: list[Run] = []
    buf: list[str] = []
    buf_style = DEFAULT_STYLE
    style = DEFAULT_STYLE
    col = 0
    i = 0
    n = len(data)

    def flush() -> None:
        # `add` only flushes when the style actually changed, so a freshly
        # flushed run can never share a style with the one before it: adjacent
        # runs are guaranteed distinct without any merging here.
        nonlocal buf
        if buf:
            cur.append(Run("".join(buf), buf_style))
            buf = []

    def add(text: str) -> None:
        nonlocal buf_style, col
        if not text:
            return
        if buf and buf_style != style:
            flush()
        if not buf:
            buf_style = style
        buf.append(text)
        col += len(text)

    while i < n:
        ch = data[i]

        if ch == "\x1b":
            i, kind, payload = _scan_escape(data, i)
            if kind == "sgr":
                style = apply_sgr(style, payload)
            elif kind == "osc":
                href = _osc8_href(payload)
                if href is not _NOCHANGE:
                    style = replace(style, href=href)
            continue

        if ch == "\n":
            flush()
            lines.append(cur)
            cur = []
            col = 0
            i += 1
            continue

        if ch == "\t":
            add(" " * (TAB_WIDTH - (col % TAB_WIDTH)))
            i += 1
            continue

        if ch < "\x20" or "\x7f" <= ch <= "\x9f":
            # \r, every other C0 control, DEL and the C1 controls have no
            # printable width.  mdcat pads a table cell holding a NEL as if it
            # were not there, and a terminal may act on an 8-bit CSI.
            i += 1
            continue

        j = i + 1
        while j < n:
            c = data[j]
            if (c == "\x1b" or c == "\n" or c == "\t" or c < "\x20"
                    or "\x7f" <= c <= "\x9f"):
                break
            j += 1
        add(data[i:j])
        i = j

    flush()
    if cur or not data.endswith("\n"):
        lines.append(cur)
    return lines


_HAS_ANSI_RE = re.compile(r"\x1b[\[\]()*+%@-Z\\^_a-z0-9=><]")


def has_ansi(data: str) -> bool:
    """True when ``data`` carries at least one plausible escape sequence."""
    return _HAS_ANSI_RE.search(data) is not None


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

# (char, style, protected).  A protected char is invisible to the syntax
# regexes below -- it is what keeps `*` inside a code span, or `_` inside a
# URL, from being mistaken for emphasis.
_Cell = tuple[str, Style, bool]
_MASK = "\x00"


def _explode(line: list[Run]) -> list[_Cell]:
    return [(ch, r.style, False) for r in line for ch in r.text]


def _implode(cells: list[_Cell]) -> list[Run]:
    out: list[Run] = []
    for ch, st, _prot in cells:
        if out and out[-1].style == st:
            out[-1].text += ch
        else:
            out.append(Run(ch, st))
    return out


def _text_of(cells: list[_Cell]) -> str:
    return "".join(_MASK if prot else ch for ch, _st, prot in cells)


def _rewrite(cells: list[_Cell], pattern: re.Pattern, build) -> list[_Cell]:
    text = _text_of(cells)
    out: list[_Cell] = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() < pos:
            continue
        out.extend(cells[pos:m.start()])
        out.extend(build(m, cells))
        pos = m.end()
    out.extend(cells[pos:])
    return out


def _keep(group: int = 1, mod=None, protect: bool = False):
    def build(m: re.Match, cells: list[_Cell]) -> list[_Cell]:
        s, e = m.span(group)
        if s < 0:
            return []
        seg = cells[s:e]
        if mod is None and not protect:
            return list(seg)
        return [(ch, mod(st) if mod else st, prot or protect) for ch, st, prot in seg]
    return build


def _drop(m: re.Match, cells: list[_Cell]) -> list[_Cell]:
    return []


def _protect_only(m: re.Match, cells: list[_Cell]) -> list[_Cell]:
    return [(ch, st, True) for ch, st, _p in cells[m.start():m.end()]]


_BOLD = lambda st: replace(st, bold=True)          # noqa: E731
_ITAL = lambda st: replace(st, italic=True)        # noqa: E731

_RE_ESCAPE = re.compile(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])")
_RE_CODE = re.compile(r"(`+)(.+?)\1")
_RE_AUTOLINK = re.compile(r"<((?:https?|ftp|file|mailto):[^>\s]+)>")
_RE_IMAGE = re.compile(
    r"!\[(?P<text>[^\]]*)\]\((?P<url>[^)\s]*)(?:\s+[\"'][^\"')]*[\"'])?\)")
_RE_LINK = re.compile(
    r"(?<!!)\[(?P<text>[^\]]*)\]\((?P<url>[^)\s]*)(?:\s+[\"'][^\"')]*[\"'])?\)")
_RE_REFLINK = re.compile(r"\[(?P<text>[^\]]+)\]\[[^\]]*\]")
_RE_HEADING = re.compile(r"^([ \t]{0,3})#{1,6}[ \t]+")
_RE_HEADING_TAIL = re.compile(r"([ \t]+#+[ \t]*)$")
_RE_QUOTE = re.compile(r"^([ \t]*)(?:>[ \t]?)+")
_RE_BULLET = re.compile(r"^([ \t]*)([-*+])([ \t]+)")
_RE_STRIKE = re.compile(r"~~(.+?)~~")
_RE_BOLD_STAR = re.compile(r"\*\*(?!\s)(.+?)(?<!\s)\*\*")
_RE_BOLD_UND = re.compile(r"(?<![\w\\])__(?!\s)(.+?)(?<!\s)__(?!\w)")
_RE_ITAL_STAR = re.compile(r"(?<![\*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?!\*)")
_RE_ITAL_UND = re.compile(r"(?<![\w\\])_(?!\s)([^_]+?)(?<!\s)_(?!\w)")
_RE_FENCE = re.compile(r"^[ \t]{0,3}(?:`{3,}|~{3,})")
_RE_RULE_LINE = re.compile(
    r"^[ \t]*(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|={3,}[ \t]*)$")
_RE_TABLE_SEP = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*$")


def _link_build(m: re.Match, cells: list[_Cell]) -> list[_Cell]:
    url = m.group("url").strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1]
    s, e = m.span("text")
    if s < 0:
        return []
    return [(ch, replace(st, href=url or st.href), prot)
            for ch, st, prot in cells[s:e]]


def _bullet_build(m: re.Match, cells: list[_Cell]) -> list[_Cell]:
    st = cells[m.start(2)][1] if m.start(2) < len(cells) else DEFAULT_STYLE
    out = list(cells[m.start(1):m.end(1)])
    out.append(("\u2022", st, True))
    out.append((" ", st, True))
    return out


def _rule_cells(cells: list[_Cell]) -> list[_Cell]:
    """Replace a thematic break / table separator with a real box-drawing rule."""
    text = "".join(ch for ch, _st, _p in cells)
    st = cells[0][1] if cells else DEFAULT_STYLE
    width = max(3, len(text.rstrip()))
    return [("\u2500", st, True)] * width


def _strip_line(line: list[Run]) -> list[Run]:
    cells = _explode(line)
    if not cells:
        return []
    raw = "".join(ch for ch, _st, _p in cells)

    if _RE_RULE_LINE.match(raw) or _RE_TABLE_SEP.match(raw):
        return _implode(_rule_cells(cells))

    cells = _rewrite(cells, _RE_ESCAPE, _keep(1, protect=True))
    cells = _rewrite(cells, _RE_CODE, _keep(2, protect=True))
    cells = _rewrite(cells, _RE_AUTOLINK, _keep(1, protect=True))
    cells = _rewrite(cells, _RE_IMAGE, _keep("text"))
    cells = _rewrite(cells, _RE_LINK, _link_build)
    cells = _rewrite(cells, _RE_REFLINK, _keep("text"))
    # Bare URLs must not be chewed up by the emphasis rules below.
    cells = _rewrite(cells, _RE_URL, _protect_only)

    text = _text_of(cells)
    if _RE_HEADING.match(text):
        cells = _rewrite(cells, _RE_HEADING, _drop)
        cells = _rewrite(cells, _RE_HEADING_TAIL, _drop)
    else:
        cells = _rewrite(cells, _RE_QUOTE, _keep(1))
        cells = _rewrite(cells, _RE_BULLET, _bullet_build)

    cells = _rewrite(cells, _RE_STRIKE, _keep(1))
    cells = _rewrite(cells, _RE_BOLD_STAR, _keep(1, mod=_BOLD))
    cells = _rewrite(cells, _RE_BOLD_UND, _keep(1, mod=_BOLD))
    cells = _rewrite(cells, _RE_ITAL_STAR, _keep(1, mod=_ITAL))
    cells = _rewrite(cells, _RE_ITAL_UND, _keep(1, mod=_ITAL))
    return _implode(cells)


def strip_markdown(lines: list[list[Run]]) -> list[list[Run]]:
    """Lightly de-syntax raw markdown, keeping run structure.

    Only meaningful when :func:`has_ansi` is False.  Drops ``#``/``*``/``_``/
    ```` ` ````/``>`` syntax characters, unwraps ``[text](url)`` to ``text``
    (recording the URL in :attr:`Style.href`), turns list markers into ``\u2022``
    and thematic breaks / table separators into box-drawing rules, and blanks
    code-fence delimiters while leaving fenced content verbatim.

    ``mdcat``'s degraded (non-tty, no ``--ansi``) output -- ``text[1]`` markers
    plus a trailing ``[1]: https://...`` block -- passes through untouched and
    stays visible; use :func:`nonspeakable_spans` to keep it out of the audio.
    """
    out: list[list[Run]] = []
    in_fence = False
    for line in lines:
        if not line:
            out.append([])
            continue
        raw = plain_text(line)
        if _RE_FENCE.match(raw):
            in_fence = not in_fence
            out.append([])                 # keep the line, drop the delimiter
            continue
        if in_fence:
            out.append([Run(r.text, r.style) for r in line])
            continue
        out.append(_strip_line(line))
    return out


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
