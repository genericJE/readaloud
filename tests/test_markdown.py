"""Tests for readaloud.markdown (``-md``).

The pure tests pin every step on its own, without mdcat: how a source row splits
into cells, which source tables count, where the sentinels go, how a render
divides into table blocks, the ``joins`` between wrapped pieces, and the mapping
checks on renders copied from real ``mdcat --ansi --columns 24`` output.  A fake
mdcat (a tiny Python script) covers the subprocess and error handling.

The tests guarded by ``needs_mdcat`` render a corpus of documents with the real
binary at several widths and check that every table is mapped and every cell
reads back the words of its source cell.  They always pass ``columns`` and point
``XDG_CONFIG_HOME`` at an empty directory, so a user's mdcat config cannot move
a single byte.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
import sys
import unicodedata

import pytest
from markdown_it import MarkdownIt

from readaloud import ansi, markdown
from readaloud.document import Table, TableCell, line_word_spans, split_words
from readaloud.markdown import MdcatError, find_mdcat, render_markdown, render_width

MDCAT = shutil.which("mdcat")
needs_mdcat = pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")

WJ = markdown.SENTINEL
BOM = chr(0xFEFF)
#: lines markdown-it takes for blank and pulldown-cmark does not
UNICODE_SPACES = ["\N{NO-BREAK SPACE}", "\N{IDEOGRAPHIC SPACE}", "\N{LINE SEPARATOR}"]


def cells_of(row: str) -> list[str]:
    return [row[s:e] for s, e in markdown._split_cells(row)]


def tables_of(text: str):
    return markdown._source_tables(markdown._prepare(text))


def plains(table) -> list[list[str]]:
    return [[cell.plain for cell in cells] for _line, cells in table.rows]


def row_lines(table) -> list[int]:
    return [line for line, _cells in table.rows]


def geometry(text: str) -> list[str]:
    prepared = markdown._prepare(text)
    tables = markdown._source_tables(prepared)
    return markdown._geometry_source(prepared, tables).split("\n")


# ---------------------------------------------------------------------------
# preparing the source
# ---------------------------------------------------------------------------


def test_prepare_normalises_newlines_bom_sentinels_and_tabs():
    text = BOM + "a\r\nb\rc" + WJ + "\n|\tx|"
    assert markdown._prepare(text) == "a\nb\nc\n|   x|"


# ---------------------------------------------------------------------------
# splitting a row into cells (pulldown-cmark's rules)
# ---------------------------------------------------------------------------


def test_split_cells_outer_pipes_are_optional():
    assert cells_of("| a | b |") == [" a ", " b "]
    assert cells_of("a | b") == ["a ", " b"]
    assert cells_of("| a | b") == [" a ", " b"]
    assert cells_of("a |") == ["a "]


def test_split_cells_keeps_empty_cells_between_pipes():
    assert cells_of("| a || b |") == [" a ", "", " b "]
    assert cells_of("|  |  |") == ["  ", "  "]


def test_split_cells_a_blank_after_the_last_pipe_is_not_a_cell():
    assert cells_of("| a |   ") == [" a "]


def test_split_cells_a_backslash_before_a_pipe_never_splits():
    assert cells_of(r"| a \| b | c |") == [r" a \| b ", " c "]
    assert cells_of(r"| a \\| b |") == [r" a \\| b "]


def test_split_cells_pipes_inside_code_spans_still_split():
    # not what the GFM spec says, but what mdcat draws
    assert cells_of("| `a | b` | c |") == [" `a ", " b` ", " c "]


# ---------------------------------------------------------------------------
# finding the source tables
# ---------------------------------------------------------------------------


def test_source_table_rows_cells_and_plain_text():
    text = ("Intro.\n\n"
            "| Name | Notes |\n"
            "|:--|--:|\n"
            "| **Bob** | `code` &amp; [link](https://example.com) |\n"
            "| short |\n"
            "| a | b | dropped |\n")
    (table,) = tables_of(text)
    assert row_lines(table) == [2, 4, 5, 6]
    assert table.ncols == 2
    assert table.delimiter == (3, 0)
    assert plains(table) == [
        ["Name", "Notes"], ["Bob", "code & link"], ["short"], ["a", "b"]]


def test_cell_plain_text_is_what_mdcat_shows():
    text = ("| one<br>two<BR />three | ![alt *text*](p.png) x | a \\| b |\n"
            "|---|---|---|\n")
    (table,) = tables_of(text)
    assert plains(table) == [["one\ntwo\nthree", "alt text x", "a | b"]]


def test_an_images_alt_text_keeps_its_entities_and_escapes():
    # inside an image markdown-it leaves them as "text_special" tokens; dropping
    # them made "café" read "caf", and the table fell back line by line
    text = "| ![caf&eacute; A&#66;C](m.png) | ![x\\|y \\*z](p.png) |\n|---|---|\n"
    (table,) = tables_of(text)
    assert plains(table) == [["café ABC", "x|y *z"]]


def test_tables_in_a_blockquote_and_in_lists():
    text = ("> | q | r |\n"
            "> |---|---|\n"
            "> | 1 | 2 |\n"
            "\n"
            "- | l1 | l2 |\n"
            "  |----|----|\n"
            "  | x  | y  |\n"
            "\n"
            "1. item\n"
            "\n"
            "   | o | p |\n"
            "   |---|---|\n"
            "   | 3 | 4 |\n")
    quoted, bulleted, ordered = tables_of(text)
    assert plains(quoted) == [["q", "r"], ["1", "2"]]
    assert quoted.delimiter == (1, 2)
    assert plains(bulleted) == [["l1", "l2"], ["x", "y"]]
    assert plains(ordered) == [["o", "p"], ["3", "4"]]
    assert row_lines(ordered) == [10, 12]


def test_code_blocks_hold_no_tables():
    text = ("```\n| a | b |\n|---|---|\n```\n\n"
            "    | c | d |\n    |---|---|\n")
    assert tables_of(text) == []


def test_a_table_interrupts_a_paragraph_only_with_a_leading_pipe():
    assert tables_of("para\na | b\n--- | ---\nc | d\n") == []
    (table,) = tables_of("para\n| a | b |\n|---|---|\n| c | d |\n")
    assert plains(table) == [["a", "b"], ["c", "d"]]


def test_a_delimiter_row_without_a_pipe_is_not_a_table():
    # markdown-it says table; pulldown-cmark (and so mdcat) draws a heading
    assert tables_of("| a |\n---\n") == []


@pytest.mark.parametrize("ender", ["|", "  |  ", "[^1]: a footnote", ": a definition"])
def test_pulldown_ends_the_body_where_markdown_it_reads_on(ender):
    (table,) = tables_of(f"| a | b |\n|---|---|\n| c | d |\n{ender}\n| e | f |\n")
    assert plains(table) == [["a", "b"], ["c", "d"]]


def test_an_indented_line_after_a_table_is_still_a_row():
    (table,) = tables_of("| a | b |\n|---|---|\n| c | d |\n    e | f\n    - item\n")
    assert row_lines(table) == [0, 2, 3]
    assert plains(table)[-1] == ["e", "f"]


def test_front_matter_keeps_source_line_numbers():
    text = "---\ntitle: | a | b |\n---\n| a | b |\n|---|---|\n| c | d |\n"
    (table,) = tables_of(text)
    assert row_lines(table) == [3, 5]


@pytest.mark.parametrize("definition", [
    "https://example.com/benchmarks", "<https://example.com/benchmarks>", "Ibid."])
def test_a_footnote_reference_stays_text_whatever_its_definition(definition):
    # markdown-it knows no footnotes: "[^n]: <one token>" is a link reference to
    # it, which made "Kokoro[^n]" read "Kokoro^n" and failed the content check
    (table,) = tables_of("| Model | Speed |\n|---|---|\n| Kokoro[^n] | fast |\n\n"
                         f"[^n]: {definition}\n")
    assert plains(table)[1] == ["Kokoro[^n]", "fast"]
    (table,) = tables_of(f"[^n]: {definition}\n\n| Model |\n|---|\n| [^n] Kokoro |\n")
    assert plains(table)[1] == ["[^n] Kokoro"]


def test_a_reference_link_in_a_cell_still_shows_its_text():
    (table,) = tables_of("| Model |\n|---|\n| [Kokoro][k] |\n\n[k]: https://example.com\n")
    assert plains(table)[1] == ["Kokoro"]


@pytest.mark.parametrize("space", UNICODE_SPACES)
def test_a_line_of_unicode_spaces_is_still_a_row(space):
    # markdown-it ends a table at a line str.strip() empties; pulldown-cmark only
    # at ASCII whitespace, and draws that line and the rows after it as rows
    rows = ["| a | b |", "|---|---|", "| c | d |", space, "| → | ✗ |"]
    (table,) = tables_of("\n".join(rows) + "\n\nafter\n")
    assert row_lines(table) == [0, 2, 3, 4]
    assert plains(table)[3] == ["→", "✗"]
    (quoted,) = tables_of("".join(f"> {row}\n" for row in rows))
    assert row_lines(quoted) == [0, 2, 3, 4]
    assert plains(quoted)[3] == ["→", "✗"]


def test_rows_past_unicode_spaces_stay_inside_their_list_item():
    nbsp = "\N{NO-BREAK SPACE}"
    (table,) = tables_of(f"- | a | b |\n  |---|---|\n  | c | d |\n  {nbsp}\n  | e | f |\n"
                         "| g | h |\n")
    assert row_lines(table) == [0, 2, 3, 4]


@pytest.mark.parametrize("ender", ["# heading", "- item", "> quote", "```", "|",
                                   "[^1]: note", ": definition", ""])
def test_reading_on_past_unicode_spaces_stops_where_pulldown_does(ender):
    text = f"| a | b |\n|---|---|\n| c | d |\n\N{NO-BREAK SPACE}\n{ender}\n| e | f |\n"
    (table,) = tables_of(text)
    assert row_lines(table) == [0, 2, 3]


@pytest.mark.parametrize("delimiter", ["- | -", "- | - |", " - | -", "-\t| -", "- | :-:",
                                       "-  |  -"])
def test_a_delimiter_row_starting_with_a_dash_and_a_space(delimiter):
    # markdown-it reads it as a list item; pulldown-cmark (and so mdcat) draws a table
    (table,) = tables_of(f"| Key | Action |\n{delimiter}\n| q | quit |\n\nafter\n")
    assert plains(table) == [["Key", "Action"], ["q", "quit"]]
    assert row_lines(table) == [0, 2]
    assert table.delimiter[0] == 1


def test_a_dash_delimiter_row_in_one_column_a_quote_and_a_list():
    (single,) = tables_of("| Key |\n- |\n| q |\n")
    assert plains(single) == [["Key"], ["q"]]
    (quoted,) = tables_of("> | Key | Action |\n> - | -\n> | q | quit |\n")
    assert plains(quoted) == [["Key", "Action"], ["q", "quit"]]
    (listed,) = tables_of("- | Key | Action |\n  - | -\n  | q | quit |\n| x | y |\n")
    assert plains(listed) == [["Key", "Action"], ["q", "quit"]]
    (after_para,) = tables_of("para\n| Key | Action |\n- | -\n| q | quit |\n")
    assert row_lines(after_para) == [1, 3]


@pytest.mark.parametrize("text", [
    "```\n| Key | Action |\n- | -\n| q | quit |\n```\n",       # a fence
    "    | Key | Action |\n    - | -\n",                         # indented code
    "<div>\n| Key | Action |\n- | -\n</div>\n",                 # an HTML block
    "para\nKey | Action\n- | -\n",       # not a paragraph's first line, no leading pipe
    "| Key | Action |\n- | - | -\n",                             # column count
    "| Key | Action |\n- | x\n",                                 # not a delimiter cell
    "Key Action\n- -\n",                                        # no pipe at all
])
def test_dash_delimiter_decoys_are_not_tables(text):
    assert tables_of(text) == []


@pytest.mark.parametrize("gap", ["", "\n"])
def test_a_table_on_a_definition_line(gap):
    # pulldown-cmark opens a definition at ": " below a paragraph, and its first
    # line can be a table header; markdown-it sees a paragraph
    text = f"Term\n{gap}: | Key | Action |\n  |---|---|\n  | q | quit |\n| x | y |\n"
    (table,) = tables_of(text)
    assert plains(table) == [["Key", "Action"], ["q", "quit"]]
    assert table.delimiter == (2 + len(gap), 2)


def test_a_definition_header_keeps_its_sentinel_past_the_marker():
    # without outer pipes, and with a dash delimiter row that markdown-it takes
    # for a list: the header is still the definition's, not the paragraph line's
    text = "Term\n\n: Key | Action\n  - | -\n  q | quit\n"
    (table,) = tables_of(text)
    assert plains(table) == [["Key", "Action"], ["q", "quit"]]
    assert geometry(text)[2] == f": {WJ * 2}Key | {WJ}Action"


@pytest.mark.parametrize("text", [
    ": | Key | Action |\n  |---|---|\n",              # no term above it
    "# Head\n: | Key | Action |\n  |---|---|\n",      # a heading is no term
    "Term\n: | Key | Action |\n|---|---|\n",          # delimiter not indented under it
])
def test_definition_line_decoys_are_not_tables(text):
    assert tables_of(text) == []


# ---------------------------------------------------------------------------
# sentinel placement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cell, marked", [
    ("plain words", "@plain words"),
    ("**tea**", "**@tea**"),
    ("***both***", "***@both***"),
    ("_under_ x", "_@under_ x"),
    ("**$5**", "**@$5**"),
    ('*"quoted"*', '*@"quoted"*'),
    ("~~old~~ new", "~~@old~~ new"),
    ("~single~ tilde", "~@single~ tilde"),
    ("~5 minutes", "@~5 minutes"),
    ("** a b **", "@** a b **"),
    ("\\*x\\*", "@\\*x\\*"),
    ("`code` tail", "@`code` tail"),
    ("[link](https://example.com)", "@[link](https://example.com)"),
])
def test_the_sentinel_goes_where_the_visible_text_starts(cell, marked):
    lines = geometry(f"| h |\n|---|\n| {cell} |\n")
    assert lines[2] == "| " + marked.replace("@", WJ) + " |"


def test_empty_cells_get_a_sentinel_of_their_own():
    lines = geometry("| a |  | c |\n|---|---|---|\n|  || x |\n")
    assert lines[0] == f"| {WJ * 2}a | {WJ} | {WJ}c |"
    assert lines[2] == f"| {WJ} |{WJ}| {WJ}x |"


def test_the_first_header_cell_carries_the_table_id():
    lines = geometry("| a | b |\n|---|---|\n| c | d |\n\n"
                     "| e | f |\n|---|---|\n| g | h |\n")
    assert lines[0] == f"| {WJ * 2}a | {WJ}b |"
    assert lines[2] == f"| {WJ}c | {WJ}d |"
    assert lines[4] == f"| {WJ * 3}e | {WJ}f |"
    assert lines[6] == f"| {WJ}g | {WJ}h |"


def test_only_the_delimiter_row_loses_its_alignment_colons():
    lines = geometry("> | a: | b |\n> |:--|--:|\n> | x: | y |\n")
    assert lines[1] == "> |---|---|"
    assert lines[0] == f"> | {WJ * 2}a: | {WJ}b |"
    assert lines[2] == f"> | {WJ}x: | {WJ}y |"


# ---------------------------------------------------------------------------
# table blocks in a render
# ---------------------------------------------------------------------------

# `mdcat --ansi --columns 24` on DOC, plain text: the display render ...
DOC = ("| Name | Notes |\n|:-----|------:|\n"
       "| Bob | wraps onto a second line |\n|  | x |\n")
DISPLAY = [
    "",
    "────────────────────────",
    " Name             Notes ",
    "────────────────────────",
    " Bob       wraps onto a ",
    "            second line ",
    "                      x ",
    "────────────────────────",
    "",
]
# ... and the geometry render of the same document
GEOMETRY = [
    "",
    "────────────────────────",
    f" {WJ * 2}Name  {WJ}Notes            ",
    "────────────────────────",
    f" {WJ}Bob   {WJ}wraps onto a     ",
    "       second line      ",
    f" {WJ}      {WJ}x                ",
    "────────────────────────",
    "",
]


def test_find_blocks_rule_header_rule_body_rule():
    assert markdown._find_blocks(DISPLAY) == [markdown._Block(1, 3, 7, 0)]


def test_find_blocks_header_only_quoted_and_indented_tables():
    quoted = ["│ ────", "│  a  ", "│ ────", "│ ────"]
    assert markdown._find_blocks(quoted) == [markdown._Block(0, 2, 3, 2)]
    indented = ["  ──────", "   x  y ", "  ──────", "   1  2 ", "  ──────"]
    assert markdown._find_blocks(indented) == [markdown._Block(0, 2, 4, 2)]


def test_find_blocks_ignores_lone_rules_and_broken_blocks():
    find = markdown._find_blocks
    assert find(["────"]) == []
    assert find(["──", " a ", "──"]) == []                          # no bottom rule
    assert find(["────", "text", "", "────", "x", "────"]) == []    # a blank line
    assert find(["│ ───", "x", "│ ───", "│ ───"]) == []             # lost its quote bar


def test_find_blocks_finds_consecutive_tables():
    blocks = markdown._find_blocks(DISPLAY + DISPLAY)
    assert [(b.top, b.bottom) for b in blocks] == [(1, 7), (10, 16)]


# ---------------------------------------------------------------------------
# joins and the content check
# ---------------------------------------------------------------------------


def test_joins_follow_the_source_whitespace():
    joins = markdown._joins
    assert joins(["wraps onto a", "second line"],
                 "wraps onto a second line") == [" ", " "]
    assert joins(["https://example.com/", "long/path"],
                 "https://example.com/long/path") == [" ", ""]
    assert joins(["Supercal", "ifragili"], "Supercalifragili") == [" ", ""]
    assert joins(["日本語の", "テキスト"], "日本語のテキスト") == [" ", ""]
    assert joins(["one", "two"], "one\ntwo") == [" ", " "]            # a <br>
    assert joins(["ab", "    ", "cd"], "abcd") == [" ", " ", ""]      # blank in between


def test_joins_ignore_the_backslash_of_an_escaped_pipe():
    # `x \| y` in a code span: mdcat shows "x | y", markdown-it keeps "x \| y".
    # In an autolink both keep the backslash; in `a \\| b` text only mdcat drops one.
    joins = markdown._joins
    assert joins(["getting_st", "arted | other"],
                 "getting_started \\| other") == [" ", ""]
    assert joins(["http://e.com/", "a\\|b"], "http://e.com/a\\|b") == [" ", ""]
    assert joins(["a |", "b"], "a \\| b") == [" ", " "]
    assert joins(["a\\", "b"], "a \\ b") == [" ", " "]


def test_joins_align_on_letters_and_digits_when_mdcat_changed_the_text():
    joins = markdown._joins
    # a footnote reference: [^note] is shown as [1]
    assert joins(["te", "xt[1]"], "text[^note]") == [" ", ""]
    assert joins(["text[", "1] more"], "text[^note] more") == [" ", ""]
    assert joins(["text[1]", "more"], "text[^note] more") == [" ", " "]
    assert joins(["text", "[1] more"], "text[^note] more") == [" ", ""]
    assert joins(["[1]", "Kokoro"], "[^n] Kokoro") == [" ", " "]
    # smart punctuation, math
    assert joins(["Don\N{RIGHT SINGLE QUOTATION MARK}t use https://e.com/", "a/b"],
                 "Don't use https://e.com/a/b") == [" ", ""]
    assert joins(["E=mc\N{SUPERSCRIPT TWO} Pneumonoultra", "microscopic"],
                 "E=mc^2 Pneumonoultramicroscopic") == [" ", ""]
    # a wrap before punctuation mdcat did not change is placed exactly
    assert joins(["Tiny and fast", ". Voices"], "Tiny and fast. Voices") == [" ", ""]
    # ... one before punctuation it did change falls back to the source's spaces
    assert joins(["defaults", "\N{HORIZONTAL ELLIPSIS} capped"],
                 "defaults... capped") == [" ", " "]


def test_joins_fall_back_to_spaces_when_the_walk_cannot_keep_in_step():
    assert markdown._joins(["ab", "cd"], "abxd") == [" ", " "]
    assert markdown._joins(["", ""], "") == [" ", " "]


def test_content_key_ignores_wraps_markup_and_footnote_numbers():
    key = markdown._content_key
    assert key("text[ 1] and") == key("text[^note] and")
    assert key("x² + y₁") == key("x^2 + y_1")
    assert key("Designer with a") == key("Designer  with\na")
    assert key("α ≤ β") != key("\\alpha \\leq \\beta")


# ---------------------------------------------------------------------------
# mapping a table (captured renders, no mdcat)
# ---------------------------------------------------------------------------


def words_by_cell(plain: list[str], table: Table) -> dict[tuple[int, int], list[str]]:
    """Every word on the table's row lines, by the one cell whose span holds it."""
    out: dict[tuple[int, int], list[str]] = {(c.row, c.col): [] for c in table.cells}
    for first, end in table.rows:
        for line in range(first, end):
            for s, e in line_word_spans(plain[line]):
                owners = [(c.row, c.col) for c in table.cells
                          for ln, a, b in c.spans if ln == line and a <= s and e <= b]
                assert len(owners) == 1, (plain[line], plain[line][s:e], owners)
                out[owners[0]].append(plain[line][s:e])
    return out


def test_map_tables_on_captured_renders():
    mapped, reasons = markdown._map_tables(tables_of(DOC), DISPLAY, GEOMETRY)
    assert reasons == {}
    ((k, table),) = mapped
    assert k == 0
    assert table == Table(1, 8, 2, [(2, 3), (4, 6), (6, 7)], [
        TableCell(0, 0, [(2, 1, 5)], [" "]),
        TableCell(0, 1, [(2, 7, 23)], [" "]),
        TableCell(1, 0, [(4, 1, 5), (5, 1, 5)], [" ", " "]),
        TableCell(1, 1, [(4, 7, 23), (5, 7, 23)], [" ", " "]),
        TableCell(2, 0, [(6, 1, 5)], [" "]),
        TableCell(2, 1, [(6, 7, 23)], [" "]),
    ])
    assert words_by_cell(DISPLAY, table) == {
        (0, 0): ["Name"], (0, 1): ["Notes"],
        (1, 0): ["Bob"], (1, 1): ["wraps", "onto", "a", "second", "line"],
        (2, 0): [], (2, 1): ["x"],
    }


def test_a_misplaced_row_start_is_not_mapped():
    geo = list(GEOMETRY)
    geo[6] = f"  {WJ}     {WJ}x                "
    assert markdown._map_tables(tables_of(DOC), DISPLAY, geo) == (
        [], {0: "its rows do not match the source"})


def test_text_crossing_a_gutter_is_not_mapped():
    display = list(DISPLAY)
    display[4] = " Bob wraps      onto a "
    display[4] += " " * (24 - len(display[4]))          # same width, same words
    assert markdown._map_tables(tables_of(DOC), display, GEOMETRY) == (
        [], {0: "its columns do not line up"})


def test_a_glyph_in_a_gutter_is_not_mapped():
    # same words in the same order, same column contents: only the gutter shows it
    display, geo = list(DISPLAY), list(GEOMETRY)
    display[5] = "      Q     second line "
    geo[5] = "      Q second line     "
    assert markdown._map_tables(tables_of(DOC), display, geo) == (
        [], {0: "its columns do not line up"})


def test_body_lines_without_a_row_start_are_not_mapped():
    # pulldown-cmark read a row markdown-it did not: it carries no sentinel
    header_only = tables_of("| Name | Notes |\n|:-----|------:|\n")
    geo = [ln.replace(WJ, "") if i > 3 else ln for i, ln in enumerate(GEOMETRY)]
    assert markdown._map_tables(header_only, DISPLAY, geo) == (
        [], {0: "its rows do not match the source"})


def test_a_row_glued_on_after_a_line_of_unicode_spaces_is_not_mapped():
    # `mdcat --ansi --columns 24` on "| Step | Done |\n|---|---|\n| Build | ✓ |\n"
    # plus a line of U+00A0 and "| → | ✗ |", with sentinels only in the rows a
    # source side that stopped at the U+00A0 line would see: both drawn rows
    # look like wrapped lines of "Build", and no letter tells the cells apart
    nbsp = "\N{NO-BREAK SPACE}"
    rule = "─" * 13
    display = ["", rule, " Step   Done ", rule, " Build  ✓    ", f" {nbsp}           ",
               " →      ✗    ", rule, ""]
    geo = ["", rule, f" {WJ * 2}Step   {WJ}Done ", rule, f" {WJ}Build  {WJ}✓    ",
           f" {nbsp}           ", " →      ✗    ", rule, ""]
    tables = tables_of("| Step | Done |\n|---|---|\n| Build | ✓ |\n")
    assert markdown._map_tables(tables, display, geo) == (
        [], {0: "its rows do not match the source"})
    # the same blank line from a cell's own &nbsp; wrapped alone is fine
    tables = tables_of("| Step | Done |\n|---|---|\n| Build&nbsp; | ✓ |\n")
    mapped, reasons = markdown._map_tables(tables, display[:6] + display[7:],
                                           geo[:6] + geo[7:])
    assert (len(mapped), reasons) == (1, {})


def test_a_table_missing_from_the_display_is_not_mapped():
    display = ["", "Name Notes", "Bob wraps onto a second line", "x"] + [""] * 5
    assert markdown._map_tables(tables_of(DOC), display, GEOMETRY) == (
        [], {0: "mdcat drew it differently than expected"})


def test_cells_that_differ_from_the_source_are_not_mapped():
    tables = tables_of(DOC.replace("onto", "into"))
    assert markdown._map_tables(tables, DISPLAY, GEOMETRY) == (
        [], {0: "a cell does not match its source text"})


def test_rows_wider_than_the_rule_are_not_mapped():
    def widen(lines):
        return [ln + " " if ln and not ln.startswith("─") else ln for ln in lines]
    assert markdown._map_tables(tables_of(DOC), widen(DISPLAY), widen(GEOMETRY)) == (
        [], {0: "rows overflow the column width"})


def test_a_pasted_rendering_without_sentinels_is_skipped():
    pasted = [ln.replace(WJ, "") for ln in GEOMETRY]
    mapped, reasons = markdown._map_tables(
        tables_of(DOC), DISPLAY + DISPLAY, pasted + GEOMETRY)
    assert reasons == {}
    ((_k, table),) = mapped
    assert (table.line_start, table.line_end) == (10, 17)


def test_a_table_mdcat_printed_as_text_gets_no_notice():
    as_text = ["", f"{WJ * 2}Name | {WJ}Notes |---| {WJ}Bob | {WJ}wraps", ""]
    assert markdown._map_tables(tables_of(DOC), as_text, as_text) == ([], {})
    assert markdown._map_tables(tables_of(DOC), [""], [""]) == (
        [], {0: "mdcat did not draw it as a table"})


def test_source_cells_keep_only_what_the_mapping_reads():
    assert [f.name for f in dataclasses.fields(markdown._SourceCell)] == ["mark", "plain"]


def test_notices_name_each_table_then_collapse():
    assert markdown._notices({0: "x", 2: "y"}) == [
        "-md: table 1 is read line by line (x)",
        "-md: table 3 is read line by line (y)",
    ]
    assert markdown._notices(dict.fromkeys(range(4), "r")) == [
        "-md: 4 tables are read line by line"]


# ---------------------------------------------------------------------------
# running mdcat (a fake one)
# ---------------------------------------------------------------------------


def fake_mdcat(tmp_path, *, status=0, fail_on_sentinel=False):
    """A stand-in mdcat that logs its arguments and echoes its input (or fails)."""
    log = tmp_path / "runs.log"
    script = tmp_path / "mdcat"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "data = sys.stdin.buffer.read()\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"if {status} or ({fail_on_sentinel} and chr(0x2060).encode() in data):\n"
        "    sys.stderr.write('\\n  boom: it broke  \\nmore detail\\n')\n"
        f"    sys.exit({status or 1})\n"
        "sys.stdout.buffer.write(data)\n")
    script.chmod(0o755)
    return str(script), log


def test_mdcat_gets_the_documented_arguments_and_the_prepared_source(tmp_path):
    mdcat, log = fake_mdcat(tmp_path)
    rendered = render_markdown("para\r\n", mdcat=mdcat, columns=33)
    assert log.read_text() == "--ansi --columns 33 --image-protocol none --local -\n"
    assert ansi.plain_lines(rendered.lines) == ["para"]
    assert (rendered.tables, rendered.notices) == ([], [])


def test_a_display_failure_raises_one_line(tmp_path):
    mdcat, _log = fake_mdcat(tmp_path, status=3)
    with pytest.raises(MdcatError) as info:
        render_markdown("| a |\n|---|\n", mdcat=mdcat, columns=40)
    assert str(info.value) == "mdcat failed: boom: it broke"


def test_a_panic_says_so_and_how_to_read_the_file_anyway(tmp_path):
    # a Rust panic's stderr is a source path and an internal detail: not echoed
    mdcat, _log = fake_mdcat(tmp_path, status=101)
    with pytest.raises(MdcatError) as info:
        render_markdown("text", mdcat=mdcat, columns=40)
    assert str(info.value) == (
        "mdcat crashed on this document; readaloud -f FILE reads it without mdcat")


def test_a_missing_binary_raises(tmp_path):
    with pytest.raises(MdcatError) as info:
        render_markdown("text", mdcat=str(tmp_path / "no-such-mdcat"), columns=40)
    assert str(info.value).startswith("could not run mdcat")
    assert "\n" not in str(info.value)


def test_a_geometry_failure_keeps_the_display_and_says_so(tmp_path):
    mdcat, log = fake_mdcat(tmp_path, fail_on_sentinel=True)
    rendered = render_markdown("| a |\n|---|\n| b |\n", mdcat=mdcat, columns=40)
    assert ansi.plain_lines(rendered.lines) == ["| a |", "|---|", "| b |"]
    assert rendered.tables == []
    assert rendered.notices == [
        "-md: tables are read line by line (mdcat could not render the table map)"]
    assert len(log.read_text().splitlines()) == 2


def test_a_bug_while_mapping_keeps_the_display_and_says_so(tmp_path, monkeypatch):
    # finding the cells is an extra: a crash there must not stop the reading
    def boom(*a, **k):
        raise IndexError("a bug")

    monkeypatch.setattr(markdown, "_map_tables", boom)
    mdcat, _log = fake_mdcat(tmp_path)
    rendered = render_markdown("| a |\n|---|\n| b |\n", mdcat=mdcat, columns=40)
    assert ansi.plain_lines(rendered.lines) == ["| a |", "|---|", "| b |"]
    assert rendered.tables == []
    assert rendered.notices == [
        "-md: tables are read line by line (mapping them failed: IndexError)"]


def test_a_document_without_tables_renders_once(tmp_path):
    mdcat, log = fake_mdcat(tmp_path)
    render_markdown("# Title\n\nJust text | with a pipe.\n", mdcat=mdcat, columns=40)
    assert len(log.read_text().splitlines()) == 1


def test_find_mdcat_looks_on_path(monkeypatch):
    monkeypatch.setattr(markdown.shutil, "which", lambda name: "/opt/bin/" + name)
    assert find_mdcat() == "/opt/bin/mdcat"
    monkeypatch.setattr(markdown.shutil, "which", lambda name: None)
    assert find_mdcat() is None


def test_render_width_prefers_columns_then_the_terminal(monkeypatch):
    monkeypatch.setattr(markdown, "_tty_width", lambda: 50)
    monkeypatch.setenv("COLUMNS", "120")
    assert render_width() == 80
    monkeypatch.setenv("COLUMNS", "33")
    assert render_width() == 33
    monkeypatch.setenv("COLUMNS", "7")
    assert render_width() == 20
    monkeypatch.setenv("COLUMNS", "junk")
    assert render_width() == 50


def test_render_width_falls_back_to_the_default(monkeypatch):
    def no_terminal(*_args):
        raise OSError("not a terminal")
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(markdown, "_tty_width", lambda: 0)
    monkeypatch.setattr(markdown.os, "get_terminal_size", no_terminal)
    assert render_width() == 80
    assert render_width(64) == 64
    assert render_width(200) == 80
    assert render_width(10) == 20


# ---------------------------------------------------------------------------
# real mdcat
# ---------------------------------------------------------------------------

CORPUS = {
    "wrapping": ("""\
| Name | Role | Notes |
|------|------|-------|
| Alice | Engineer | short |
| Bob | Designer with a very long title that wraps around the column | `code` here |
""", 1),
    "alignments": ("""\
| left | center | right | none |
|:-----|:------:|------:|------|
| a | bb | ccc | dddd |
| a longer left cell | centred text here | right aligned words | plain |
""", 1),
    "empty_short_long": ("""\
| a | b | c |
|---|---|---|
|  | only the middle |  |
| a short row |
| one | two | three | four is dropped |
""", 1),
    "wide_glyphs": ("""\
| 日本語 | emoji |
|--------|-------|
| 日本語のテキストはスペースなしで折り返す | 🍵 tea and 👍 thumbs |
| café naïve | ✓ |
""", 1),
    "inline_markup": ("""\
| kind | cell |
|------|------|
| strong | **bold text** here |
| emphasis | *italic* and _under_ |
| strike | ~~struck~~ and ~single~ |
| mixed | ***both*** and **$5** and *"quoted"* |
| literal | ** a b ** and \\*stars\\* |
| entity | Tom &amp; Jerry &copy; 2026 |
""", 1),
    "links_and_code": ("""\
| what | cell |
|------|------|
| link | [a labelled link](https://example.com/path "title") after |
| autolink | <https://example.com/a/very/long/path/that/wraps> |
| image | ![alt text](pic.png) caption |
| code | `a \\| b` and a \\| b |
""", 1),
    "breaks": ("""\
| id | lines |
|----|-------|
| one | first line<br>second line<br/>third |
| two | <br>after a leading break |
""", 1),
    "header_only": ("""\
Some text first.

| only | a header |
|------|----------|
""", 1),
    "blockquote": ("""\
> Quoted intro.
>
> | q | r |
> |:--|--:|
> | 1 | a quoted cell that wraps a little |
""", 1),
    "lists": ("""\
- item before
- | l1 | l2 |
  |----|----|
  | x  | a list cell with several words |

1. ordered

   | o | p |
   |---|---|
   | 3 | four |
""", 2),
    "code_fence_decoy": ("""\
```
────────────
 a    b
────────────
 1    2
────────────
```

| real | table |
|------|-------|
| after | the fence |
""", 1),
    "two_tables": ("""\
| first | table |
|-------|-------|
| a | b |

| second | table |
|--------|-------|
| c | d |
""", 2),
    "footnotes": ("""\
| term | note |
|------|------|
| alpha | defined here[^n] and again later |

[^n]: The footnote.
""", 1),
    "messy_source": (
        BOM + "| tab\tcell | b |\r\n|---|---|\r\n| c" + WJ + "d | e |\r\n", 1),
}

_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])
_FOOTNOTE = re.compile(r"\[(?:\^[^\]]+|\d{1,4})\]")


def shown(tokens) -> str:
    """The text mdcat shows for inline tokens (an independent, minimal renderer)."""
    out = []
    for tok in tokens:
        if tok.type in ("text", "code_inline"):
            out.append(tok.content)
        elif tok.type == "html_inline":
            br = re.fullmatch(r"<br\s*/?>", tok.content, re.I)
            out.append("\n" if br else tok.content)
        elif tok.type == "image":
            out.append(shown(tok.children or []))
    return "".join(out)


def expected_tables(text: str) -> list[list[list[str]]]:
    """Every table's cells as markdown-it's own table rule splits them."""
    tables: list[list[list[str]]] = []
    row: list[str] | None = None
    for tok in _MD.parse(markdown._prepare(text), {}):
        if tok.type == "table_open":
            tables.append([])
        elif tok.type == "tr_open":
            row = []
        elif tok.type == "inline" and row is not None:
            row.append(shown(tok.children or []))
        elif tok.type == "tr_close":
            tables[-1].append(row)
            row = None
    return tables


def letters(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def alnum(text: str) -> str:
    """Letters and digits, less footnote references (mdcat renumbers them)."""
    return letters(_FOOTNOTE.sub("", "".join(text.split())))


def check_structure(table: Table) -> None:
    head_first, head_end = table.rows[0]
    assert head_first == table.line_start + 1
    body = table.rows[1:]
    if body:
        assert body[0][0] == head_end + 1               # one header rule in between
    for (_a, end), (first, _b) in zip(body, body[1:]):
        assert end == first
    assert (body[-1][1] if body else head_end + 1) == table.line_end - 1
    assert [(c.row, c.col) for c in table.cells] == [
        (r, c) for r in range(len(table.rows)) for c in range(table.ncols)]
    for cell in table.cells:
        first, end = table.rows[cell.row]
        assert [ln for ln, _s, _e in cell.spans] == list(range(first, end))
        assert len(cell.joins) == len(cell.spans)


@pytest.fixture
def isolated_mdcat(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return MDCAT


@needs_mdcat
@pytest.mark.parametrize("columns", [30, 44, 80])
@pytest.mark.parametrize("name", sorted(CORPUS))
def test_real_tables_read_back_their_source_cells(isolated_mdcat, name, columns):
    text, count = CORPUS[name]
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=columns)
    assert rendered.notices == []
    assert len(rendered.tables) == count
    plain = ansi.plain_lines(rendered.lines)
    for table, source in zip(rendered.tables, expected_tables(text)):
        check_structure(table)
        assert len(table.rows) == len(source)
        words = words_by_cell(plain, table)
        for cell in table.cells:
            row = source[cell.row]
            want = row[cell.col] if cell.col < len(row) else ""
            joined = "".join(plain[ln][s:e].strip() for ln, s, e in cell.spans)
            assert alnum(joined) == alnum(want), (cell, joined, want)
            assert letters("".join(words[(cell.row, cell.col)])) == letters(joined)


@needs_mdcat
@pytest.mark.parametrize("name", ["two_tables", "empty_short_long", "blockquote",
                                  "lists"])
def test_real_short_cells_read_back_word_for_word(isolated_mdcat, name):
    text, _count = CORPUS[name]
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=80)
    plain = ansi.plain_lines(rendered.lines)
    for table, source in zip(rendered.tables, expected_tables(text)):
        words = words_by_cell(plain, table)
        for cell in table.cells:
            row = source[cell.row]
            want = row[cell.col] if cell.col < len(row) else ""
            assert words[(cell.row, cell.col)] == split_words(want)


@needs_mdcat
def test_real_joins_glue_a_url_split_by_a_wrap(isolated_mdcat):
    text = "| a | b |\n|---|---|\n| https://example.com/some/long/path | z |\n"
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=24)
    (table,) = rendered.tables
    cell = next(c for c in table.cells if (c.row, c.col) == (1, 0))
    plain = ansi.plain_lines(rendered.lines)
    pieces = [plain[ln][s:e].strip() for ln, s, e in cell.spans]
    assert len(pieces) == 3
    assert "".join(j + p for j, p in zip([""] + cell.joins[1:], pieces)) == \
        "https://example.com/some/long/path"


@needs_mdcat
def test_real_joins_glue_a_wrapped_code_span_with_an_escaped_pipe(isolated_mdcat):
    # found in a real README: the source text kept the backslash of `\|`, the
    # render did not, so every wrap was read as a space and the identifier was
    # spoken in two pieces
    text = ("| a | b |\n|---|---|\n"
            "| `getting_started_with_a_long_identifier \\| other` | z |\n")
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=30)
    (table,) = rendered.tables
    cell = next(c for c in table.cells if (c.row, c.col) == (1, 0))
    plain = ansi.plain_lines(rendered.lines)
    pieces = [plain[ln][s:e].strip() for ln, s, e in cell.spans]
    assert len(pieces) == 2
    assert "".join(j + p for j, p in zip([""] + cell.joins[1:], pieces)) == \
        "getting_started_with_a_long_identifier | other"


@needs_mdcat
def test_real_an_image_whose_alt_text_has_an_entity_is_mapped(isolated_mdcat):
    text = "| pic | note |\n|---|---|\n| ![caf&eacute; menu](m.png) | the caf&eacute; |\n"
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=40)
    assert rendered.notices == []
    (table,) = rendered.tables
    plain = ansi.plain_lines(rendered.lines)
    assert words_by_cell(plain, table)[(1, 0)] == ["café", "menu"]


def spoken(plain: list[str], cell: TableCell) -> str:
    """A cell's text the way the Document glues it: trimmed pieces and their joins."""
    parts: list[str] = []
    for join, (ln, s, e) in zip(cell.joins, cell.spans):
        piece = plain[ln][s:e].strip()
        if piece:
            parts.append(join + piece if parts else piece)
    return "".join(parts)


def body_cell(rendered, row: int, col: int) -> tuple[list[str], TableCell]:
    (table,) = rendered.tables
    return (ansi.plain_lines(rendered.lines),
            next(c for c in table.cells if (c.row, c.col) == (row, col)))


@needs_mdcat
def test_real_joins_glue_a_wrapped_word_that_carries_a_footnote(isolated_mdcat):
    word = "Pneumonoultramicroscopicsilicovolcanoconiosis"
    text = (f"| Term | Meaning |\n|---|---|\n| {word}[^1] | a lung disease |\n\n"
            "[^1]: The longest word in major dictionaries.\n")
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=40)
    assert rendered.notices == []
    plain, cell = body_cell(rendered, 1, 0)
    assert sum(1 for ln, s, e in cell.spans if plain[ln][s:e].strip()) > 1
    assert spoken(plain, cell) == word + "[1]"


@needs_mdcat
def test_real_joins_glue_a_wrapped_url_under_smart_punctuation(isolated_mdcat, tmp_path):
    # -md keeps the user's mdcat config, and smart_punctuation curls the apostrophe
    (tmp_path / "mdcat").mkdir()
    (tmp_path / "mdcat" / "config.toml").write_text("[defaults]\nsmart_punctuation = true\n")
    url = "https://github.com/swsnr/mdcat/releases/latest"
    text = f"| Tool | Notes |\n|---|---|\n| mdcat | Don't use {url} |\n"
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=44)
    assert rendered.notices == []
    plain, cell = body_cell(rendered, 1, 1)
    assert "\N{RIGHT SINGLE QUOTATION MARK}" in plain[cell.spans[0][0]]
    assert sum(1 for ln, s, e in cell.spans if plain[ln][s:e].strip()) > 1
    assert spoken(plain, cell) == f"Don\N{RIGHT SINGLE QUOTATION MARK}t use {url}"


@needs_mdcat
@pytest.mark.parametrize("definition", ["https://example.com/benchmarks", "Ibid."])
def test_real_a_footnote_with_a_one_token_definition_is_mapped(isolated_mdcat,
                                                               definition):
    text = ("| Model | Speed |\n|---|---|\n| Kokoro[^bench] | fast |\n\n"
            f"[^bench]: {definition}\n")
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=80)
    assert rendered.notices == []
    (table,) = rendered.tables
    plain = ansi.plain_lines(rendered.lines)
    assert [spoken(plain, c) for c in table.cells] == ["Model", "Speed", "Kokoro[1]", "fast"]


@needs_mdcat
@pytest.mark.parametrize("quote", ["", "> "])
@pytest.mark.parametrize("space", UNICODE_SPACES)
def test_real_a_line_of_unicode_spaces_keeps_the_rows_apart(isolated_mdcat, space,
                                                           quote):
    rows = ["| Step | Done |", "|---|---|", "| Build | ✓ |", space, "| → | ✗ |"]
    text = "".join(f"{quote}{row}\n" for row in rows)
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=40)
    assert rendered.notices == []
    (table,) = rendered.tables
    assert len(table.rows) == 4
    plain = ansi.plain_lines(rendered.lines)
    assert [spoken(plain, c) for c in table.cells] == [
        "Step", "Done", "Build", "✓", "", "", "→", "✗"]


@needs_mdcat
@pytest.mark.parametrize("text", [
    "| Key | Action |\n- | -\n| q | quit |\n",
    "| Key | Action |\n-\t| - |\n| q | quit |\n",
    "| Key |\n- |\n| q |\n",
    "> | Key | Action |\n> - | -\n> | q | quit |\n",
    "- | Key | Action |\n  - | -\n  | q | quit |\n",
    "Term\n: | Key | Action |\n  |---|---|\n  | q | quit |\n",
    "Term\n\n: Key | Action\n  - | -\n  q | quit\n",
    "```\n| a | b |\n- | -\n| c | d |\n```\n\n| Key | Action |\n- | -\n| q | quit |\n",
])
def test_real_tables_markdown_it_does_not_see_are_mapped(isolated_mdcat, text):
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=60)
    assert rendered.notices == []
    (table,) = rendered.tables
    plain = ansi.plain_lines(rendered.lines)
    assert [spoken(plain, c) for c in table.cells] == (
        ["Key", "q"] if table.ncols == 1 else ["Key", "Action", "q", "quit"])


@needs_mdcat
def test_real_display_is_mdcats_own_output(isolated_mdcat):
    text, _count = CORPUS["wrapping"]
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=44)
    direct = subprocess.run(
        [isolated_mdcat, "--ansi", "--columns", "44", "--image-protocol", "none",
         "--local", "-"],
        input=text.encode(), capture_output=True, check=True).stdout.decode()
    assert rendered.lines == ansi.parse(direct)


SQUEEZED = ("| a | b | c | d | e | f | g | h |\n|---|---|---|---|---|---|---|---|\n"
            "| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |\n")


@needs_mdcat
@pytest.mark.parametrize("text, columns, reason", [
    (SQUEEZED, 20, "rows overflow the column width"),
    ("| m | x |\n|---|---|\n| $$\\alpha \\leq \\beta$$ | y |\n", 40,
     "a cell does not match its source text"),
    ("| m | x |\n|---|---|\n| **x\\** | y |\n", 40,
     "mdcat drew it differently than expected"),
])
def test_real_unmappable_tables_fall_back_with_a_notice(isolated_mdcat, text, columns,
                                                        reason):
    rendered = render_markdown("Before.\n\n" + text + "\nAfter.\n",
                               mdcat=isolated_mdcat, columns=columns)
    assert rendered.tables == []
    assert rendered.notices == [f"-md: table 1 is read line by line ({reason})"]
    assert "After." in ansi.plain_lines(rendered.lines)


@needs_mdcat
def test_real_many_failures_collapse_into_one_notice(isolated_mdcat):
    good = CORPUS["two_tables"][0]
    text = "\n".join([SQUEEZED] * 4) + "\n" + good
    rendered = render_markdown(text, mdcat=isolated_mdcat, columns=20)
    assert rendered.notices == ["-md: 4 tables are read line by line"]
    assert len(rendered.tables) == 2


@needs_mdcat
@pytest.mark.parametrize("text", [
    "```\n| a | b |\n|---|---|\n",                     # unclosed fence
    "| a |\n",                                          # a pipe, no table
    "para\n| a | b |\n|---|---|\n| c |\n    | d | e |\n",
    "| a | b |\n|---|---|\n| c | d |\n|\n",
    "- - | a | b |\n    |---|---|\n    | c | d |\n",
    "",
])
def test_real_odd_documents_never_raise(isolated_mdcat, text):
    for columns in (20, 80):
        rendered = render_markdown(text, mdcat=isolated_mdcat, columns=columns)
        assert isinstance(rendered.lines, list)
        assert len(rendered.notices) <= 1
