"""readaloud.markdown -- ``-md``: render Markdown with mdcat, find its tables' cells.

``readaloud -md notes.md`` shows exactly what ``mdcat --ansi notes.md`` shows,
but reads every table one cell at a time.  mdcat draws a table without pipes:
it pads columns with spaces and wraps a long cell onto extra physical lines that
interleave with its neighbours.  Neither row nor column boundaries can be
recovered from that output alone (a wrapped row is byte identical to a row
whose first cell is empty, and cells keep their inner double spaces).

So the document is rendered twice:

* the DISPLAY render is the source as the user wrote it, and is what they see;
* the GEOMETRY render is the same source with an invisible U+2060 WORD JOINER
  where every table cell's visible content begins, and every column left
  aligned.  The joiner has no width and no line break opportunity, and
  alignment only moves padding inside a column, so both renders have the same
  physical lines.  In the geometry render each joiner sits on its column's
  start, and the lines that carry joiners are exactly the first lines of rows.
  The first header cell of source table ``k`` gets ``k + 2`` joiners, which is
  how a rendered block names the source table it came from.

A table is only handed to the Document after its geometry has been checked
against the display render and its cells' text against the source.  One that
fails any check is left out -- it is read line by line, as before -- and gets a
one line notice.  The source side follows pulldown-cmark (mdcat's parser) where
that is cheap; the checks catch the disagreements that remain.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

from markdown_it import MarkdownIt

from . import ansi
from .ansi import Run
from .document import Table, TableCell
from .width import cell_offsets, char_width, text_width

__all__ = [
    "MdcatError",
    "Rendered",
    "find_mdcat",
    "render_width",
    "render_markdown",
]

#: U+2060 WORD JOINER: zero width, and it forbids a line break on both sides
SENTINEL = "\u2060"

_TIMEOUT = 60
_MAX_WIDTH = 80          # mdcat's own cap when its stdout is not a terminal
_MIN_WIDTH = 20
_MAX_NOTICES = 3

_GEOMETRY_FAILED = (
    "-md: tables are read line by line (mdcat could not render the table map)")
_MAPPING_FAILED = "-md: tables are read line by line (mapping them failed: {})"


class MdcatError(Exception):
    """mdcat could not render the document; the message is one user-facing line."""


@dataclass
class Rendered:
    """What ``-md`` hands to the Document."""

    #: mdcat's display render of the document, parsed by ``ansi.parse``
    lines: list[list[Run]]
    #: the tables whose cells were mapped, in document order
    tables: list[Table] = field(default_factory=list)
    #: one short line per table that will be read line by line instead, and why
    notices: list[str] = field(default_factory=list)


def find_mdcat() -> str | None:
    """Absolute path of the mdcat binary, or None when it is not installed."""
    return shutil.which("mdcat")


def render_width(default: int = 80) -> int:
    """The width to render at: the terminal's, capped at 80 like mdcat's own default.

    ``COLUMNS`` wins when it is set, because curses honours it and the table
    must fit the screen curses draws.  Otherwise the controlling terminal is
    asked directly, since stdin or stdout may well be a pipe.
    """
    width = 0
    try:
        width = int(os.environ.get("COLUMNS", ""))
    except ValueError:
        pass
    if width <= 0:
        width = _tty_width()
    if width <= 0:
        try:
            width = os.get_terminal_size(sys.__stdout__.fileno()).columns
        except (AttributeError, OSError, ValueError):
            width = 0
    if width <= 0:
        width = default
    return max(_MIN_WIDTH, min(width, _MAX_WIDTH))


def _tty_width() -> int:
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | getattr(os, "O_NOCTTY", 0))
    except OSError:
        return 0
    try:
        return os.get_terminal_size(fd).columns
    except OSError:
        return 0
    finally:
        os.close(fd)


def render_markdown(text: str, *, mdcat: str, columns: int) -> Rendered:
    """Render Markdown source `text` with mdcat and map its tables' cells.

    Raises `MdcatError` when mdcat cannot render the document at all.
    """
    source = _prepare(text)
    status, out, err = _run_mdcat(mdcat, source, columns)
    if status != 0:
        raise MdcatError(_failure(status, err))
    lines = ansi.parse(out)
    try:
        tables = _source_tables(source)
        if not tables:
            return Rendered(lines)
        try:
            status, geo, _err = _run_mdcat(mdcat, _geometry_source(source, tables),
                                           columns)
        except MdcatError:
            status = -1
        if status != 0:
            return Rendered(lines, [], [_GEOMETRY_FAILED])
        mapped, reasons = _map_tables(
            tables, ansi.plain_lines(lines), ansi.plain_lines(ansi.parse(geo)))
    except Exception as exc:  # noqa: BLE001 - the cells are a nicety; reading is not
        return Rendered(lines, [], [_MAPPING_FAILED.format(type(exc).__name__)])
    return Rendered(lines, [t for _k, t in mapped], _notices(reasons))


def _notices(reasons: dict[int, str]) -> list[str]:
    if len(reasons) > _MAX_NOTICES:
        return [f"-md: {len(reasons)} tables are read line by line"]
    return [f"-md: table {k + 1} is read line by line ({reasons[k]})"
            for k in sorted(reasons)]


# ---------------------------------------------------------------------------
# running mdcat
# ---------------------------------------------------------------------------


def _prepare(text: str) -> str:
    """The source both renders start from.

    Tabs are expanded here, before any sentinel is inserted: mdcat counts a tab
    as zero columns while ``ansi.parse`` expands it to a 4-column stop, so a
    tab left in a cell would put every later column in the wrong place.  A
    U+2060 the author typed would be taken for a sentinel, so it goes too.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace(SENTINEL, "").expandtabs(4)


def _run_mdcat(mdcat: str, source: str, columns: int) -> tuple[int, str, str]:
    """(exit status, stdout, stderr) of one render.

    The environment is inherited on purpose: the user's mdcat theme is part of
    "display it the way mdcat does".  stdout is a pipe, so mdcat never probes
    the terminal.
    """
    cmd = [mdcat, "--ansi", "--columns", str(columns),
           "--image-protocol", "none", "--local", "-"]
    try:
        proc = subprocess.run(cmd, input=source.encode("utf-8", errors="replace"),
                              capture_output=True, timeout=_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise MdcatError(f"mdcat did not finish within {_TIMEOUT} seconds") from None
    except OSError as exc:
        raise MdcatError(f"could not run mdcat: {exc.strerror or exc}") from None
    return (proc.returncode, proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


def _failure(status: int, stderr: str) -> str:
    """The one line an `MdcatError` says, from mdcat's exit status and stderr.

    Exit status 101 is a Rust panic.  Its stderr starts with the source path
    of the panic and goes on with an internal detail (at worst a long state
    dump), neither of which helps, so it gets a fixed line instead.
    """
    if status == 101:
        return "mdcat crashed on this document; readaloud -f FILE reads it without mdcat"
    line = next((ln.strip() for ln in stderr.splitlines() if ln.strip()), "")
    return f"mdcat failed: {line}" if line else f"mdcat failed with exit status {status}"


# ---------------------------------------------------------------------------
# the source's tables
# ---------------------------------------------------------------------------

_WS = " \t\x0b\x0c"
_LIST_MARKER_RE = re.compile(r"(?:[-*+]|\d{1,9}[.)])[ \t]+")
#: a row pulldown-cmark ends the table on (markdown-it reads it as a row):
#: a lone pipe, a definition list's ``:``, a footnote definition
_ROW_END_RE = re.compile(r"\|[ \t]*$|:|\[\^[^\]]+\]:")
#: a line that starts another block, so pulldown-cmark does not read it as a row
#: (markdown-it agrees unless the line is indented four spaces)
_INTERRUPT_RE = re.compile(
    r"#{1,6}(?:[ \t]|$)|>|`{3}|~{3}|<[A-Za-z/!?]"
    r"|(?:[-*+]|\d{1,9}[.)])(?:[ \t]|$)"
    r"|(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
#: one cell of a delimiter row, once trimmed
_DELIMITER_CELL_RE = re.compile(r":?-+:?")
#: a pipe that splits cells (no backslash before it)
_PIPE_RE = re.compile(r"(?<!\\)\|")
#: a definition list's marker, with the spaces pulldown-cmark counts as part of it
_DEFINITION_RE = re.compile(r"( *):( *)")
_OPENERS = frozenset(("em_open", "strong_open", "s_open"))


@dataclass
class _SourceCell:
    """One cell of a source row."""

    mark: int                  # where the cell's sentinel goes in its source line
    plain: str                 # the text mdcat shows for it, "\n" for a <br>


@dataclass
class _SourceTable:
    """A GFM table as pulldown-cmark will see it."""

    #: (source line, cells) for the header row, then every body row
    rows: list[tuple[int, list[_SourceCell]]]
    #: the delimiter row's source line and where its content starts
    delimiter: tuple[int, int]

    @property
    def ncols(self) -> int:
        return len(self.rows[0][1])


def _markdown_it() -> MarkdownIt:
    return MarkdownIt("commonmark").enable(["table", "strikethrough"])


def _frontmatter_lines(source: str) -> int:
    """How many leading lines mdcat drops as YAML front matter (0 for none)."""
    if not source.startswith("---\n"):
        return 0
    lines = source.split("\n")
    for i in range(1, len(lines)):
        if lines[i] in ("---", "..."):
            return i + 1
    return 0


def _source_tables(source: str) -> list[_SourceTable]:
    """Every GFM table in `source`, in document order.

    markdown-it finds most of them.  The few that pulldown-cmark draws and
    markdown-it does not see -- a delimiter row starting ``- |``, a table on a
    definition list's ``:`` line -- are looked for where markdown-it sees a
    paragraph.
    """
    lines = source.split("\n")
    skip = _frontmatter_lines(source)
    md = _markdown_it()
    env: dict = {}
    tokens = md.parse("\n".join(lines[skip:]), env)
    # markdown-it knows no footnotes, so it takes "[^1]: https://..." for a link
    # reference definition and "[^1]" in a cell for a link.  pulldown-cmark
    # never does (mdcat shows "[1]"), so the label has to stay literal text.
    env["references"] = {label: ref for label, ref in env.get("references", {}).items()
                         if not label.startswith("^")}
    paragraph_ends = {tok.map[1] + skip for tok in tokens
                      if tok.type == "paragraph_open" and tok.map}
    tables: list[_SourceTable] = []
    taken: set[int] = set()    # lines already part of a table
    items: list[int] = []      # first line of every enclosing list item
    for i, tok in enumerate(tokens):
        if tok.type == "list_item_open":
            items.append(tok.map[0] + skip)
        elif tok.type == "list_item_close":
            items.pop()
        elif tok.type in ("table_open", "paragraph_open") and tok.map:
            if tok.type == "table_open":
                table = _source_table(md, env, lines, tokens, i, skip, items)
            else:
                table = _paragraph_table(md, env, lines, tok, skip, items,
                                         paragraph_ends)
            # a table pulldown-cmark reads on into already holds this one's lines
            if table is not None and not {table.rows[0][0], table.delimiter[0]} & taken:
                tables.append(table)
                taken.update(ln for ln, _cells in table.rows)
                taken.add(table.delimiter[0])
    return tables


def _source_table(md, env, lines, tokens, i, skip, items) -> _SourceTable | None:
    head = tokens[i].map[0] + skip
    row_lines = []
    j = i + 1
    while tokens[j].type != "table_close":
        if tokens[j].type == "tr_open":
            row_lines.append(tokens[j].map[0] + skip)
        j += 1
    after = tokens[j + 1] if j + 1 < len(tokens) else None
    indented = []
    end = tokens[i].map[1] + skip
    if (after is not None and after.type == "code_block"
            and after.map[0] == tokens[i].map[1]):
        # markdown-it ends the table at a line indented four spaces; pulldown-cmark
        # strips any indentation from a row and reads on
        indented = list(range(after.map[0] + skip, after.map[1] + skip))
        end = after.map[1] + skip
    content = _content_start(lines[head], sum(1 for first in items if first == head))
    header = lines[head][content:]
    prev = tokens[i - 3] if i >= 3 and tokens[i - 1].type == "paragraph_close" else None
    if prev is not None and prev.map and prev.map[1] + skip == head \
            and not header.startswith("|"):
        return None            # pulldown-cmark keeps it part of the paragraph
    delim_start = _content_start(lines[head + 1], 0)
    delim = lines[head + 1][delim_start:]
    spans = _split_cells(header)
    if "|" not in delim or len(_split_cells(delim)) != len(spans):
        return None            # not a delimiter row to pulldown-cmark
    ncols = len(spans)
    rows = [(head, _cells(md, env, lines[head], content, spans))]
    for ln in [*row_lines[1:], *indented]:
        start = _content_start(lines[ln], 0)
        row = lines[ln][start:]
        if (_ROW_END_RE.match(row)
                or ln in indented and (not row or _INTERRUPT_RE.match(row))):
            # pulldown-cmark ends the table on such a line
            return _SourceTable(rows, (head + 1, delim_start))
        rows.append((ln, _cells(md, env, lines[ln], start, _split_cells(row)[:ncols])))
    quotes = lines[head + 1][:delim_start].count(">")
    need = 0
    if items:
        indents = [_container(lines[ln], quotes)
                   for ln in [head + 1, *row_lines[1:]]]
        need = min((q - p for p, q in filter(None, indents)), default=0)
    _read_on(md, env, lines, rows, end, quotes, need)
    return _SourceTable(rows, (head + 1, delim_start))


def _paragraph_table(md, env, lines, tok, skip, items,
                     paragraph_ends) -> _SourceTable | None:
    """A table pulldown-cmark draws inside what markdown-it calls a paragraph, or None.

    Two kinds.  A delimiter row starting with ``-`` and a space (``- | -``) is a
    list item to markdown-it, which ends the paragraph above it; to
    pulldown-cmark it makes that paragraph's last line a table header.  And a
    definition list's ``:`` line opens a definition whose first line can be a
    header, with the delimiter and body rows indented under it.
    """
    first, end = tok.map[0] + skip, tok.map[1] + skip
    markers = sum(1 for line in items if line == first)
    for head in range(first, end):
        if head == first and (markers or not _follows_paragraph(lines, head,
                                                                 paragraph_ends)):
            continue           # a definition needs its term right above it
        quotes = lines[head][:_content_start(lines[head], 0)].count(">")
        at = _container(lines[head], quotes)
        m = _DEFINITION_RE.match(lines[head], at[0]) if at is not None else None
        if m is None or len(m.group(1)) > 3:
            continue
        # pulldown-cmark's definition content starts past the ":" and at most
        # three more columns; the rows below it must be indented that far
        pre, post = len(m.group(1)), len(m.group(2))
        need = pre + 1 + min(post, 3 - pre)
        content = m.end()
        while content < len(lines[head]) and lines[head][content] in _WS:
            content += 1
        below = _container(lines[head + 1], quotes) if head + 1 < len(lines) else None
        if (below is not None and need <= below[1] - below[0] <= need + 3
                and _is_table_head(lines[head][content:], lines[head + 1][below[1]:])):
            return _table_from(md, env, lines, head, content, head + 1, below[1],
                               quotes, need)
        if head == end - 1:
            return None        # the last line is a definition's and heads no table
    head = end - 1
    content = _content_start(lines[head], markers if head == first else 0)
    if end < len(lines) and (head == first or lines[head].startswith("|", content)):
        delim_start = _content_start(lines[end], 0)
        quotes = lines[end][:delim_start].count(">")
        at = _container(lines[end], quotes)
        if (at is not None and re.match(r"-[ \t]", lines[end][delim_start:])
                and quotes == lines[head][:content].count(">")
                and _is_table_head(lines[head][content:], lines[end][delim_start:])):
            need = at[1] - at[0] if items else 0
            return _table_from(md, env, lines, head, content, end, delim_start,
                               quotes, need)
    return None


def _follows_paragraph(lines: list[str], ln: int, paragraph_ends: set[int]) -> bool:
    """Whether the nearest line above `ln` that is not blank ends a paragraph."""
    k = ln - 1
    while k >= 0 and not lines[k].strip(_WS + ">"):
        k -= 1
    return k >= 0 and k + 1 in paragraph_ends


def _table_from(md, env, lines, head, content, delim, delim_start, quotes,
                need) -> _SourceTable:
    """The table whose header row starts at ``lines[head][content]``."""
    spans = _split_cells(lines[head][content:])
    rows = [(head, _cells(md, env, lines[head], content, spans))]
    _read_on(md, env, lines, rows, delim + 1, quotes, need)
    return _SourceTable(rows, (delim, delim_start))


def _is_table_head(header: str, delim: str) -> bool:
    """Whether pulldown-cmark reads a paragraph line `header` and the line `delim`
    below it as a table's header and delimiter rows."""
    cells = _split_cells(delim)
    return (_PIPE_RE.search(header) is not None and "|" in delim
            and all(_DELIMITER_CELL_RE.fullmatch(delim[s:e].strip(_WS)) for s, e in cells)
            and len(cells) == len(_split_cells(header)))


def _container(line: str, quotes: int) -> tuple[int, int] | None:
    """``(p, q)``: `line` past `quotes` blockquote markers is ``line[p:]``, and its
    indentation ends at `q`.  None when a marker is missing."""
    p, n = 0, len(line)
    for _ in range(quotes):
        while p < n and line[p] in _WS:
            p += 1
        if p == n or line[p] != ">":
            return None
        p += 1
        if p < n and line[p] == " ":
            p += 1             # the marker's own space
    q = p
    while q < n and line[q] in _WS:
        q += 1
    return p, q


def _read_on(md, env, lines: list[str], rows: list, ln: int, quotes: int,
             need: int) -> None:
    """Append to `rows` the lines from `ln` on that pulldown-cmark reads as more rows.

    A row stays inside the table's container (`quotes` blockquote markers,
    then at least `need` columns of indentation), neither starts another
    block nor is a row in `_ROW_END_RE`, and is not blank.  Only ASCII
    whitespace makes a line blank: pulldown-cmark draws a line of U+00A0 or
    U+3000 as a row, where markdown-it ends its tables.
    """
    ncols = len(rows[0][1])
    while ln < len(lines):
        at = _container(lines[ln], quotes)
        if at is None or at[1] - at[0] < need:
            return
        row = lines[ln][at[1]:]
        if not row or _ROW_END_RE.match(row) or _INTERRUPT_RE.match(row):
            return
        rows.append((ln, _cells(md, env, lines[ln], at[1], _split_cells(row)[:ncols])))
        ln += 1


def _content_start(line: str, list_markers: int) -> int:
    """Where a table row's own text starts, past blockquote markers and indentation.

    `list_markers` list item markers are stripped too: a table whose list item
    opens on its header line carries the marker there.
    """
    p, n = 0, len(line)
    while True:
        while p < n and line[p] in _WS:
            p += 1
        if p < n and line[p] == ">":
            p += 1
            continue
        if list_markers:
            m = _LIST_MARKER_RE.match(line, p)
            if m:
                p = m.end()
                list_markers -= 1
                continue
        return p


def _split_cells(row: str) -> list[tuple[int, int]]:
    """Raw ``(start, end)`` of every cell of a table row, split as pulldown-cmark does.

    A ``|`` splits unless a backslash precedes it -- any number of them, and
    also inside a code span, which is not what the GFM spec says but is what
    mdcat renders.  The outer pipes are optional; a segment after the last pipe
    is a cell only when it holds more than whitespace.
    """
    n = len(row)
    i = 0
    while i < n and row[i] in _WS:
        i += 1
    if i < n and row[i] == "|":
        i += 1
    spans = []
    start = i
    for j in range(i, n):
        if row[j] == "|" and row[j - 1] != "\\":
            spans.append((start, j))
            start = j + 1
    if row[start:].strip(_WS):
        spans.append((start, n))
    return spans


def _cells(md: MarkdownIt, env: dict, line: str, offset: int,
           spans: list[tuple[int, int]]) -> list[_SourceCell]:
    out = []
    for s, e in spans:
        s += offset
        e += offset
        a, b = s, e
        while a < b and line[a] in _WS:
            a += 1
        while b > a and line[b - 1] in _WS:
            b -= 1
        if a == b:
            # the sentinel alone becomes the content, so the cell still has one
            out.append(_SourceCell(s + 1 if e > s else s, ""))
            continue
        text = line[a:b]
        children = md.parseInline(text, env)[0].children or []
        out.append(_SourceCell(a + _mark_offset(text, children), _inline_plain(children)))
    return out


def _mark_offset(text: str, children) -> int:
    """How far into a cell's text its sentinel goes.

    An emphasis opener at the start of a cell must stay glued to the text it
    opens: a sentinel in between is neither whitespace nor punctuation, so it
    would change CommonMark's flanking rules and the delimiters would print
    literally.  A delimiter run markdown-it parsed as literal text (``** a **``,
    ``\\*x\\*``) is not skipped; the sentinel goes before it.  The one opener
    markdown-it does not know is GFM's single ``~``, which pulldown-cmark does.
    """
    offset = 0
    for tok in children:
        if tok.type == "text" and not tok.content:
            continue           # the emptied remains of a consumed delimiter run
        if tok.type in _OPENERS:
            offset += len(tok.markup)
            continue
        if tok.type == "text" and _single_tilde_pairs(text, offset):
            offset += 1
            if tok.content == "~":
                continue
        break
    return offset


def _single_tilde_pairs(text: str, i: int) -> bool:
    """Whether pulldown-cmark reads the lone ``~`` at ``text[i]`` as a strikethrough.

    A tilde run only ever pairs with a run of the same length, so this follows
    pulldown-cmark's flanking rules for single tildes alone.
    """
    n = len(text)
    if (text[i:i + 1] != "~" or text[i + 1:i + 2] in ("", "~")
            or text[i + 1].isspace()):
        return False
    depth = 1
    j = i + 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] != "~":
            j += 1
            continue
        k = j
        while k < n and text[k] == "~":
            k += 1
        if k - j == 1:
            before, after = text[j - 1], text[k:k + 1]
            if not before.isspace() and (
                    not after or after.isspace() or _is_punct(after)):
                depth -= 1
                if depth == 0:
                    return True
            elif after and not after.isspace() and (
                    before.isspace() or _is_punct(before)):
                depth += 1
        j = k
    return False


def _is_punct(ch: str) -> bool:
    """pulldown-cmark's is_punctuation: ASCII punctuation or a Unicode P* character."""
    if ch.isascii():
        return ch.isprintable() and not ch.isalnum() and not ch.isspace()
    return unicodedata.category(ch).startswith("P")


def _is_br(html: str) -> bool:
    """pulldown-cmark-mdcat's is_br_tag: ``<br>``, ``<br/>``, ``<BR />`` and friends."""
    inner = html.strip()
    if inner.startswith("<") and inner.endswith(">"):
        inner = inner[1:-1]
    return inner.rstrip("/").strip().lower() == "br"


def _inline_plain(tokens) -> str:
    """The text mdcat shows for a cell's inline tokens (markup gone, <br> as "\\n")."""
    out = []
    for tok in tokens:
        # an entity or a backslash escape is "text_special"; markdown-it only
        # turns those into text at the top level, not inside an image's alt
        if tok.type in ("text", "text_special", "code_inline"):
            out.append(tok.content)
        elif tok.type in ("softbreak", "hardbreak"):
            out.append(" ")
        elif tok.type == "html_inline":
            out.append("\n" if _is_br(tok.content) else tok.content)
        elif tok.type == "image":
            out.append(_inline_plain(tok.children or ()))
    return "".join(out)


def _geometry_source(source: str, tables: list[_SourceTable]) -> str:
    """`source` with every table cell's sentinel in and every column left aligned."""
    lines = source.split("\n")
    for k, table in enumerate(tables):
        for r, (ln, cells) in enumerate(table.rows):
            text = lines[ln]
            for c in range(len(cells) - 1, -1, -1):      # right to left
                count = k + 2 if r == 0 and c == 0 else 1
                mark = cells[c].mark
                text = text[:mark] + SENTINEL * count + text[mark:]
            lines[ln] = text
        ln, start = table.delimiter
        lines[ln] = lines[ln][:start] + lines[ln][start:].replace(":", "-")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# tables in a render
# ---------------------------------------------------------------------------

#: indent, blockquote bars, a run of U+2500; mdcat draws rules unstyled and unpadded
_RULE_RE = re.compile("( *)((?:\u2502 )*)(\u2500+)")


@dataclass
class _Block:
    """rule, header lines, rule, body lines, rule -- line indexes of the three rules."""

    top: int
    head_rule: int
    bottom: int
    prefix_width: int          # display width of the rule's indent and quote bars


def _find_blocks(plain: list[str]) -> list[_Block]:
    """Every table shaped block of a render, top to bottom.

    Every line of a table starts with the rule's indent and quote bars and is
    never empty (each cell carries its two padding spaces), which keeps a stray
    line of box drawing from scanning the whole document for a partner.
    """
    blocks = []
    i, n = 0, len(plain)
    while i < n:
        m = _RULE_RE.fullmatch(plain[i])
        if m is None:
            i += 1
            continue
        rule, prefix = plain[i], m.group(1) + m.group(2)
        head = _next_rule(plain, i + 1, rule, prefix)
        bottom = _next_rule(plain, head + 1, rule, prefix) if head > i + 1 else -1
        if bottom < 0:
            i += 1
            continue
        blocks.append(_Block(i, head, bottom, text_width(prefix)))
        i = bottom + 1
    return blocks


def _next_rule(plain: list[str], i: int, rule: str, prefix: str) -> int:
    while i < len(plain) and plain[i] != rule:
        if not plain[i] or not plain[i].startswith(prefix):
            return -1
        i += 1
    return i if i < len(plain) else -1


def _sentinel_runs(line: str) -> list[tuple[int, int]]:
    """``(display column, length)`` of every run of sentinels on a geometry line."""
    runs: list[tuple[int, int]] = []
    x = pos = 0
    while (i := line.find(SENTINEL, pos)) >= 0:
        x += text_width(line[pos:i])
        pos = i
        while pos < len(line) and line[pos] == SENTINEL:
            pos += 1
        runs.append((x, pos - i))
    return runs


def _signature(line: str) -> str:
    return " ".join(line.replace(SENTINEL, "").split())


class _Unmapped(Exception):
    """A table failed a check; the message is the notice's short reason."""


def _map_tables(tables: list[_SourceTable], display: list[str],
                geometry: list[str]) -> tuple[list[tuple[int, Table]], dict[int, str]]:
    """Map every source table onto the display render, or say why it could not be."""
    dblocks = _find_blocks(display)
    by_shape: dict[tuple, list[int]] = {}
    for n, db in enumerate(dblocks):
        by_shape.setdefault(_shape(display, db), []).append(n)
    used: set[int] = set()
    after = 0                  # display blocks before this one are behind us
    mapped: list[tuple[int, Table]] = []
    reasons: dict[int, str] = {}
    seen: set[int] = set()
    plain_geometry = [ln.replace(SENTINEL, "") for ln in geometry]
    gblocks = _find_blocks(plain_geometry)
    for gb in gblocks:
        runs = _sentinel_runs(geometry[gb.top + 1])
        k = runs[0][1] - 2 if runs else -1
        if not 0 <= k < len(tables) or k in seen:
            continue           # not one of ours, e.g. a rendering pasted into code
        seen.add(k)
        try:
            cols, starts = _measure(tables[k], gb, geometry, plain_geometry)
            n = _match_block(gb, dblocks, by_shape.get(_shape(plain_geometry, gb), []),
                             used, after)
            if n is None:
                raise _Unmapped("mdcat drew it differently than expected")
            used.add(n)
            after = n + 1
            mapped.append((k, _build(tables[k], gb, dblocks[n], cols, starts,
                                     plain_geometry, display)))
        except _Unmapped as exc:
            reasons[k] = str(exc)
    unseen = set(range(len(tables))) - seen
    if unseen:
        # pulldown-cmark disagreed and printed the rows as text: nothing on screen
        # looks like a table, so there is nothing to tell the user either
        boxed = {ln for gb in gblocks for ln in range(gb.top, gb.bottom + 1)}
        loose = {count - 2 for ln, line in enumerate(geometry) if ln not in boxed
                 for _x, count in _sentinel_runs(line)}
        for k in sorted(unseen - loose):
            reasons[k] = "mdcat did not draw it as a table"
    mapped.sort(key=lambda kt: kt[1].line_start)
    return mapped, dict(sorted(reasons.items()))


def _measure(table: _SourceTable, gb: _Block, geometry: list[str],
             plain: list[str]) -> tuple[list[tuple[int, int]], list[int]]:
    """Column ``(start, width)`` pairs and the body rows' first lines, by sentinel."""
    ncols = table.ncols
    runs = _sentinel_runs(geometry[gb.top + 1])
    cs = [x for x, _count in runs]
    if (len(runs) != ncols or any(count != 1 for _x, count in runs[1:])
            or cs[0] != gb.prefix_width + 1
            or any(b - a < 2 for a, b in zip(cs, cs[1:]))):
        raise _Unmapped("its columns could not be measured")
    rw = text_width(plain[gb.top + 1])
    if rw != text_width(plain[gb.top]):
        raise _Unmapped("rows overflow the column width")
    widths = [b - 2 - a for a, b in zip(cs, cs[1:])] + [rw - 1 - cs[-1]]
    if any(w < 0 for w in widths):
        raise _Unmapped("its columns could not be measured")
    # widths before row starts: a glyph too wide for its column shifts the sentinels too
    for ln in _row_lines(gb):
        if text_width(plain[ln]) != rw:
            raise _Unmapped("rows overflow the column width")
    if any(_sentinel_runs(geometry[ln]) for ln in range(gb.top + 2, gb.head_rule)):
        raise _Unmapped("its rows do not match the source")
    starts = [ln for ln in range(gb.head_rule + 1, gb.bottom)
              if SENTINEL in geometry[ln]]
    body = table.rows[1:]
    if len(starts) != len(body) or (
            gb.head_rule + 1 < gb.bottom and starts[:1] != [gb.head_rule + 1]):
        raise _Unmapped("its rows do not match the source")
    for ln, (_src, cells) in zip(starts, body):
        runs = _sentinel_runs(geometry[ln])
        if [x for x, _c in runs] != cs[:min(len(cells), ncols)] \
                or any(count != 1 for _x, count in runs):
            raise _Unmapped("its rows do not match the source")
    return list(zip(cs, widths)), starts


def _row_lines(block: _Block) -> list[int]:
    return [*range(block.top + 1, block.head_rule),
            *range(block.head_rule + 1, block.bottom)]


def _shape(plain: list[str], block: _Block) -> tuple:
    """What a geometry block and its display block have in common: every line's
    words in order (alignment only moves spaces), and where the header rule is."""
    return (block.head_rule - block.top,
            *(_signature(plain[ln]) for ln in range(block.top, block.bottom + 1)))


def _match_block(gb: _Block, dblocks: list[_Block], candidates: list[int],
                 used: set[int], after: int) -> int | None:
    """Index of the display block that shows geometry block `gb`, or None.

    `candidates` are the display blocks of the same shape, in order.  The one at
    the same line wins; otherwise the first unused one after the last match.
    """
    free = [n for n in candidates if n not in used]
    same_line = [n for n in free if dblocks[n].top == gb.top]
    if same_line:
        return same_line[0]
    return next((n for n in free if n >= after), None)


def _column_slice(line: str, offsets: list[int], a: int,
                  b: int) -> tuple[int, int] | None:
    """Char range of display columns ``[a, b)``, None when a wide glyph straddles.

    Zero-width characters on column `b` stay with the glyph before them (a
    combining mark or a variation selector belongs to the cell it follows).
    """
    last = len(line)
    i = min(bisect_left(offsets, a), last)
    j = min(bisect_left(offsets, b), last)
    if (offsets[i] != a and a < offsets[-1]) or (offsets[j] != b and b < offsets[-1]):
        return None
    while j < last and char_width(line[j]) == 0:
        j += 1
    return i, max(i, j)


def _blank_at(line: str, offsets: list[int], x: int) -> bool:
    """Whether display column `x` of a line is a space (or past its end)."""
    i = bisect_right(offsets, x) - 1       # the glyph whose cells cover `x`
    return i >= len(line) or line[i] == " "


def _build(table: _SourceTable, gb: _Block, db: _Block, cols: list[tuple[int, int]],
           starts: list[int], geometry: list[str], display: list[str]) -> Table:
    """Verify one table against the display render and the source, then build it."""
    shift = db.top - gb.top
    rw = text_width(geometry[gb.top + 1])
    slices: dict[int, list[tuple[int, int]]] = {}
    for gl in _row_lines(gb):
        line = display[gl + shift]
        offsets = cell_offsets(line)
        if offsets[-1] != rw:
            raise _Unmapped("rows overflow the column width")
        gline = geometry[gl]
        goffsets = cell_offsets(gline)
        row = []
        for a, w in cols:
            got = _column_slice(line, offsets, a, a + w)
            ref = _column_slice(gline, goffsets, a, a + w)
            if (got is None or ref is None or not _blank_at(line, offsets, a - 1)
                    or not _blank_at(line, offsets, a + w)
                    or (line[got[0]:got[1]].replace(" ", "")
                        != gline[ref[0]:ref[1]].replace(" ", ""))):
                raise _Unmapped("its columns do not line up")
            row.append(got)
        slices[gl + shift] = row

    firsts = [gb.top + 1, *starts]
    ends = [gb.head_rule, *starts[1:], gb.bottom] if starts else [gb.head_rule]
    rows = [(a + shift, b + shift) for a, b in zip(firsts, ends)]
    cells = []
    for r, (first, end) in enumerate(rows):
        source = table.rows[r][1]
        # a row's later lines hold what wrapped.  One blank in every column that
        # shows whitespace no cell of the row holds (a line of U+00A0) is a row
        # the source side missed, which would be glued onto this one
        own = {ch for cell in source for ch in cell.plain}
        for ln in range(first + 1, end):
            pieces = [display[ln][s:e] for s, e in slices[ln]]
            if all(not piece.strip() for piece in pieces) and any(
                    ch != " " and ch not in own for piece in pieces for ch in piece):
                raise _Unmapped("its rows do not match the source")
        for c in range(table.ncols):
            spans = [(ln, *slices[ln][c]) for ln in range(first, end)]
            segments = [display[ln][s:e] for ln, s, e in spans]
            shown = " ".join(seg.strip() for seg in segments if seg.strip())
            if c < len(source):
                plain = source[c].plain
                wrong = _content_key(shown) != _content_key(plain)
            else:
                plain = ""     # padding mdcat added to a short row shows nothing
                wrong = bool(shown)
            if wrong:
                raise _Unmapped("a cell does not match its source text")
            cells.append(TableCell(r, c, spans, _joins(segments, plain)))
    return Table(db.top, db.bottom + 1, table.ncols, rows, cells)


_FOOTNOTE_RE = re.compile(r"\[(?:\^[^\]]{1,40}|\d{1,4})\]")


def _content_key(text: str) -> str:
    """What a cell's rendered text and its source text must agree on.

    Only letters and digits count: wrapping drops spaces, and mdcat changes
    punctuation (math, smart quotes).  Footnote references go from both sides,
    because mdcat numbers ``[^label]`` as ``[1]``; whitespace goes first, since
    a wrap can fall inside the brackets.
    """
    text = _FOOTNOTE_RE.sub("", "".join(text.split()))
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _joins(segments: list[str], plain: str) -> list[str]:
    """``joins[i]`` for a cell's segments: "" where mdcat wrapped inside a word.

    mdcat may change a cell's punctuation (smart quotes, math, ``[^label]``
    shown as ``[1]``), so the rendered cell and its source are walked in step
    on what `_content_key` compares: letters and digits, footnote references
    left out.  A wrap point falls between two of them, and it joins a word
    when the source has no whitespace between those two.  When as many other
    characters stand between them on both sides (a footnote reference counts
    as one), the wrap is placed exactly instead, so a wrap next to punctuation
    keeps its join too.  A wrap inside a footnote reference always joins.  A
    cell whose walk cannot be kept in step gets " " everywhere.
    """
    joins = [" "] * len(segments)
    shown = "".join(segments)
    (rkept, rrefs), (skept, srefs) = _kept(shown), _kept(plain)
    ralnum, salnum = _alnums(shown, rkept, rrefs), _alnums(plain, skept, srefs)
    if [ch for ch, _k in ralnum] != [ch for ch, _k in salnum]:
        return joins
    rpos = [k for _ch, k in ralnum]
    start = 0
    for i, seg in enumerate(segments):
        before = len(shown[:start].rstrip()) - 1          # the last text before the
        after = start + len(seg) - len(seg.lstrip())      # wrap, the first after it
        if not i or not seg.strip() or before < 0:
            pass
        elif before in rrefs and rrefs.get(after) == rrefs[before]:
            joins[i] = ""
        else:
            w = bisect_left(rkept, start)      # kept characters before the wrap
            n = bisect_left(rpos, w)           # letters and digits before it
            rl = rpos[n - 1] if n else -1
            rr = rpos[n] if n < len(rpos) else len(rkept)
            sl = salnum[n - 1][1] if n else -1
            sr = salnum[n][1] if n < len(salnum) else len(skept)
            if rr - rl == sr - sl:
                sl += w - rl - 1               # the kept character right before the wrap
                sr = sl + 1
            lo = skept[sl] + 1 if sl >= 0 else 0
            hi = skept[sr] if sr < len(skept) else len(plain)
            joins[i] = " " if any(plain[j].isspace() and j not in srefs
                                  for j in range(lo, hi)) else ""
        start += len(seg)
    return joins


def _kept(text: str) -> tuple[list[int], dict[int, int]]:
    """Indexes of the characters of `text` a cell is aligned on, and its footnotes.

    Whitespace does not count, nor does a backslash before a pipe: in a table
    cell pulldown-cmark drops that one even inside a code span, where
    markdown-it keeps it.  A footnote reference counts as its first character.
    The second result maps every character a footnote reference spans to
    where it starts.
    """
    idx = [i for i, ch in enumerate(text)
           if not ch.isspace() and not (ch == "\\" and text[i + 1:i + 2] == "|")]
    squeezed = "".join(text[i] for i in idx)
    refs = {j: idx[m.start()] for m in _FOOTNOTE_RE.finditer(squeezed)
            for j in range(idx[m.start()], idx[m.end() - 1] + 1)}
    return [i for i in idx if refs.get(i, i) == i], refs


def _alnums(text: str, kept: list[int], refs: dict[int, int]) -> list[tuple[str, int]]:
    """``(letter or digit, index into kept)`` for the kept characters of `text`
    outside footnote references, normalised as `_content_key` normalises them."""
    return [(ch, k) for k, i in enumerate(kept) if i not in refs
            for ch in unicodedata.normalize("NFKC", text[i]).casefold() if ch.isalnum()]
