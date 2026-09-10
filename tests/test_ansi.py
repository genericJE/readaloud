"""Tests for readaloud.ansi.

Covers the public surface (Style, Run, parse, has_ansi, strip_markdown),
the escape-sequence state machine's tolerance of malformed input, and a live
round trip through the real `mdcat --ansi` binary.
"""

from __future__ import annotations

import os
import random
import re
import shutil
import subprocess

import pytest

from readaloud.ansi import (
    DEFAULT_STYLE,
    TAB_WIDTH,
    Run,
    Style,
    apply_sgr,
    has_ansi,
    is_reference_line,
    is_rule_line,
    nonspeakable_spans,
    parse,
    plain_lines,
    plain_text,
    rgb_to_x256,
    strip_markdown,
)

ESC = "\x1b"
ST = "\x1b\\"
BEL = "\x07"


def texts(line: list[Run]) -> list[str]:
    return [r.text for r in line]


def one(data: str) -> list[Run]:
    """Parse `data`, asserting it is a single logical line."""
    lines = parse(data)
    assert len(lines) == 1, lines
    return lines[0]


def only_style(data: str) -> Style:
    line = one(data)
    assert len(line) == 1, line
    return line[0].style


# ---------------------------------------------------------------------------
# contract types
# ---------------------------------------------------------------------------


def test_style_defaults_match_contract():
    s = Style()
    assert (s.fg, s.bg) == (None, None)
    assert (s.bold, s.dim, s.italic, s.underline, s.reverse) == (False,) * 5
    assert s.href is None
    assert s == DEFAULT_STYLE


def test_style_is_frozen_and_hashable():
    s = Style(bold=True)
    with pytest.raises(Exception):
        s.bold = False           # type: ignore[misc]
    assert {s, Style(bold=True)} == {Style(bold=True)}


def test_run_is_a_mutable_dataclass():
    r = Run("hi", DEFAULT_STYLE)
    r.text += "!"
    assert r == Run("hi!", DEFAULT_STYLE)


# ---------------------------------------------------------------------------
# parse: line splitting and whitespace
# ---------------------------------------------------------------------------


def test_parse_empty_input():
    assert parse("") == []


def test_parse_splits_lines_and_preserves_blanks():
    lines = parse("one\n\ntwo\n\n\nthree")
    assert plain_lines(lines) == ["one", "", "two", "", "", "three"]


def test_parse_trailing_newline_does_not_add_a_line():
    assert plain_lines(parse("a\n")) == ["a"]
    assert plain_lines(parse("a\n\n")) == ["a", ""]
    assert plain_lines(parse("\n")) == [""]


def test_parse_strips_trailing_cr():
    assert plain_lines(parse("a\r\nb\r\n")) == ["a", "b"]
    assert plain_lines(parse("a\r")) == ["a"]


def test_parse_no_run_contains_a_newline_or_escape():
    lines = parse("a\r\n\x1b[1mb\x1b[0m\nc")
    for line in lines:
        for r in line:
            assert "\n" not in r.text
            assert "\x1b" not in r.text


@pytest.mark.parametrize(
    "src,want",
    [
        ("\tx", "    x"),          # col 0 -> next stop is 4
        ("a\tb", "a   b"),         # col 1 -> 3 spaces
        ("abc\td", "abc d"),       # col 3 -> 1 space
        ("abcd\te", "abcd    e"),  # col 4 -> a full stop
        ("a\t\tb", "a       b"),   # two stops
    ],
)
def test_parse_expands_tabs_to_real_tab_stops(src, want):
    assert plain_lines(parse(src)) == [want]
    assert TAB_WIDTH == 4


def test_tab_column_counts_visible_text_only_not_escapes():
    # The bold escape must not shift the tab stop.
    assert plain_lines(parse("a\x1b[1m\tb")) == ["a   b"]


def test_tab_stops_reset_on_each_logical_line():
    assert plain_lines(parse("abcd\ne\tf")) == ["abcd", "e   f"]


def test_parse_drops_other_c0_controls():
    assert plain_lines(parse("a\x00b\x07c\x08d\x7fe")) == ["abcde"]


# ---------------------------------------------------------------------------
# parse: SGR
# ---------------------------------------------------------------------------


def test_sgr_simple_attributes():
    assert only_style("\x1b[1mx") == Style(bold=True)
    assert only_style("\x1b[2mx") == Style(dim=True)
    assert only_style("\x1b[3mx") == Style(italic=True)
    assert only_style("\x1b[4mx") == Style(underline=True)
    assert only_style("\x1b[7mx") == Style(reverse=True)
    assert only_style("\x1b[21mx") == Style(underline=True)


def test_sgr_combined_parameters():
    assert only_style("\x1b[1;3;4;7mx") == Style(
        bold=True, italic=True, underline=True, reverse=True)


def test_sgr_reset_forms():
    assert texts(one("\x1b[1mA\x1b[0mB")) == ["A", "B"]
    assert one("\x1b[1mA\x1b[0mB")[1].style == DEFAULT_STYLE
    # A bare ESC[m means ESC[0m.
    assert one("\x1b[1mA\x1b[mB")[1].style == DEFAULT_STYLE
    assert one("\x1b[1mA\x1b[;mB")[1].style == DEFAULT_STYLE


def test_sgr_empty_slot_inside_a_list_is_a_reset():
    # ECMA-48: an omitted parameter defaults to 0, so the bold is destroyed.
    assert only_style("\x1b[1;;4mx") == Style(underline=True)


def test_sgr_unset_codes():
    assert only_style("\x1b[1;2;22mx") == DEFAULT_STYLE
    assert only_style("\x1b[3;23mx") == DEFAULT_STYLE
    assert only_style("\x1b[4;24mx") == DEFAULT_STYLE
    assert only_style("\x1b[7;27mx") == DEFAULT_STYLE
    assert only_style("\x1b[31;39mx") == DEFAULT_STYLE
    assert only_style("\x1b[41;49mx") == DEFAULT_STYLE


def test_sgr_basic_and_bright_colours():
    assert only_style("\x1b[30mx").fg == 0
    assert only_style("\x1b[37mx").fg == 7
    assert only_style("\x1b[90mx").fg == 8
    assert only_style("\x1b[97mx").fg == 15
    assert only_style("\x1b[41mx").bg == 1
    assert only_style("\x1b[100mx").bg == 8
    assert only_style("\x1b[107mx").bg == 15


def test_sgr_mdcat_heading_bytes():
    # Real mdcat H1 prelude, verified in the ansi-colour spike.
    assert only_style("\x1b[1m\x1b[97m\x1b[104mHEADING") == Style(
        fg=15, bg=12, bold=True)


def test_sgr_256_colour():
    assert only_style("\x1b[38;5;196mx").fg == 196
    assert only_style("\x1b[48;5;17mx").bg == 17
    assert only_style("\x1b[38;5;196;1mx") == Style(fg=196, bold=True)


def test_sgr_truecolour_is_quantised_to_a_palette_index():
    s = only_style("\x1b[38;2;255;0;0mx")
    assert s.fg == rgb_to_x256(255, 0, 0)
    assert isinstance(s.fg, int) and 0 <= s.fg <= 255
    # a real catppuccin-theme triple from the spike
    s2 = only_style("\x1b[38;2;205;214;244mx")
    assert isinstance(s2.fg, int) and 0 <= s2.fg <= 255
    assert only_style("\x1b[48;2;38;139;210mx").bg is not None


def test_rgb_to_x256_landmarks():
    assert rgb_to_x256(0, 0, 0) == 16
    assert rgb_to_x256(255, 255, 255) == 231
    assert rgb_to_x256(255, 0, 0) == 196
    assert all(0 <= rgb_to_x256(r, g, b) <= 255
               for r in (0, 128, 255) for g in (0, 128, 255) for b in (0, 128, 255))


def test_sgr_colon_separated_iso8613_colour():
    assert only_style("\x1b[38:5:196mx").fg == 196
    assert only_style("\x1b[38:2::255:0:0mx").fg == rgb_to_x256(255, 0, 0)
    assert only_style("\x1b[38:2:255:0:0mx").fg == rgb_to_x256(255, 0, 0)
    assert only_style("\x1b[58:5:9;1mx") == Style(bold=True)   # underline colour


def test_sgr_9_strikethrough_is_swallowed_not_crashed():
    # mdcat emits SGR 9 for ~~text~~; Style has no field for it.
    assert texts(one("\x1b[9mstruck\x1b[0m")) == ["struck"]
    assert only_style("\x1b[9mstruck") == DEFAULT_STYLE
    assert only_style("\x1b[1;9;29mx") == Style(bold=True)


def test_sgr_unknown_codes_ignored():
    assert only_style("\x1b[5;8;53;73mx") == DEFAULT_STYLE
    assert only_style("\x1b[1;5;3mx") == Style(bold=True, italic=True)


def test_apply_sgr_is_pure():
    base = Style(bold=True, href="u")
    assert apply_sgr(base, "3") == Style(bold=True, italic=True, href="u")
    assert base == Style(bold=True, href="u")


def test_sgr_reset_does_not_close_an_open_hyperlink():
    # OSC 8 hyperlink state is independent of SGR: ESC[0m resets colour and
    # attributes but must NOT clear href, or a link whose text mdcat re-styles
    # mid-way loses its URL.
    line = one("\x1b]8;;https://e.test\x1b\\\x1b[1mA\x1b[0mB\x1b]8;;\x1b\\C")
    assert texts(line) == ["A", "B", "C"]
    assert line[0].style == Style(bold=True, href="https://e.test")
    assert line[1].style == Style(href="https://e.test")
    assert line[2].style == DEFAULT_STYLE


def test_sgr_reset_keeps_href():
    # mdcat emits ESC[0m *inside* an OSC 8 hyperlink.
    line = one("\x1b]8;;https://e.test\x1b\\\x1b[35malt\x1b[0m\x1b]8;;\x1b\\tail")
    assert texts(line) == ["alt", "tail"]
    assert line[0].style.href == "https://e.test"
    assert line[0].style.fg == 5
    assert line[1].style.href is None


# ---------------------------------------------------------------------------
# parse: non-SGR escapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seq",
    [
        "\x1b[2J", "\x1b[H", "\x1b[?1049h", "\x1b[?25l", "\x1b[c", "\x1b[6n",
        "\x1b[1;5H", "\x1b[38;5;9;1;2;3q", "\x1b[!p", "\x1b[>c", "\x1b[0K",
        "\x1b(B", "\x1b)0", "\x1b=", "\x1b>", "\x1b7", "\x1b8", "\x1bM", "\x1bc",
    ],
)
def test_non_sgr_escapes_are_discarded(seq):
    assert plain_lines(parse("A" + seq + "B")) == ["AB"]


def test_private_csi_m_is_not_treated_as_sgr():
    line = one("A\x1b[?1mB")
    assert plain_text(line) == "AB"
    assert line[0].style == DEFAULT_STYLE


def test_osc_hyperlink_st_form():
    line = one("\x1b]8;;https://example.com/a\x1b\\text\x1b]8;;\x1b\\after")
    assert texts(line) == ["text", "after"]
    assert line[0].style.href == "https://example.com/a"
    assert line[1].style.href is None


def test_osc_hyperlink_bel_form():
    line = one("\x1b]8;;https://example.com/b\x07text\x1b]8;;\x07after")
    assert texts(line) == ["text", "after"]
    assert line[0].style.href == "https://example.com/b"
    assert line[1].style.href is None


def test_osc_hyperlink_with_params():
    line = one("\x1b]8;id=xyz:foo=1;https://example.com/c\x1b\\t\x1b]8;;\x1b\\")
    assert line[0].style.href == "https://example.com/c"


def test_other_osc_strings_are_discarded():
    # mdcat probes the terminal with BEL-terminated OSC 10/11 when stdout is a
    # tty, and iTerm gets an extra OSC 1337.
    data = "\x1b]10;?\x07\x1b]11;?\x07\x1b[c" + "hello"
    assert plain_lines(parse(data)) == ["hello"]
    assert plain_lines(parse("\x1b]1337;SetMark\x1b\\x")) == ["x"]
    assert plain_lines(parse("\x1b]0;window title\x07x")) == ["x"]


def test_dcs_pm_apc_strings_are_discarded():
    assert plain_lines(parse("A\x1bPq#0;2;0;0;0\x1b\\B")) == ["AB"]
    assert plain_lines(parse("A\x1b^private\x1b\\B")) == ["AB"]
    assert plain_lines(parse("A\x1b_app\x1b\\B")) == ["AB"]


@pytest.mark.parametrize(
    "data",
    [
        "abc\x1b",
        "abc\x1b[",
        "abc\x1b[1",
        "abc\x1b[1;38;5",
        "abc\x1b]",
        "abc\x1b]8;;https://truncated",
        "abc\x1b]8;;https://x\x1b",
        "abc\x1bP",
        "abc\x1b(",
    ],
)
def test_truncated_escapes_at_end_of_input_do_not_crash(data):
    lines = parse(data)
    assert plain_lines(lines)[0].startswith("abc")
    for line in lines:
        for r in line:
            assert "\x1b" not in r.text


def test_unterminated_osc_swallowed_by_a_new_escape_resyncs():
    # No ST/BEL before the next escape: the string ends there and the SGR runs.
    line = one("A\x1b]8;;https://x\x1b[1mB")
    assert plain_text(line) == "AB"
    assert line[-1].style.bold is True


# ---------------------------------------------------------------------------
# parse: run structure
# ---------------------------------------------------------------------------


def test_adjacent_equal_styles_are_merged_into_one_run():
    assert texts(one("ab\x1b[1m\x1b[22mcd")) == ["abcd"]
    assert texts(one("\x1b[1ma\x1b[1mb")) == ["ab"]


def test_adjacent_runs_never_share_a_style():
    # ui.py relies on this: a run boundary always means an attribute change.
    for src in [
        "ab\x1b[1m\x1b[22mcd",
        "\x1b[1mA\x1b[0m\x1b[1mB",
        "a\x1b[31m\x1b[39mb\x1b]8;;u\x1b\\\x1b]8;;\x1b\\c",
        "\x1b[1m\x1b[3m\x1b[23m\x1b[22mplain",
    ]:
        for line in parse(src):
            for x, y in zip(line, line[1:]):
                assert x.style != y.style, (src, line)


def test_style_change_with_no_text_emits_no_empty_run():
    line = one("\x1b[1m\x1b[0m\x1b[31m\x1b[0mx")
    assert texts(line) == ["x"]
    assert all(r.text for r in line)


def test_style_only_line_is_empty():
    assert parse("a\n\x1b[1m\x1b[0m\nb") == [
        [Run("a", DEFAULT_STYLE)], [], [Run("b", DEFAULT_STYLE)]]


def test_styles_do_not_leak_but_do_persist_across_lines():
    lines = parse("\x1b[1mbold\nstill bold\x1b[0m\nplain")
    assert lines[0][0].style.bold and lines[1][0].style.bold
    assert lines[2][0].style == DEFAULT_STYLE


# ---------------------------------------------------------------------------
# has_ansi
# ---------------------------------------------------------------------------


def test_has_ansi():
    assert has_ansi("\x1b[1mx")
    assert has_ansi("x\x1b]8;;u\x1b\\y")
    assert has_ansi("\x1b(B")
    assert not has_ansi("")
    assert not has_ansi("# plain markdown\n\nwith *stars* and [1] refs\n")
    assert not has_ansi("bullets • and bars │ are not escapes")


# ---------------------------------------------------------------------------
# strip_markdown
# ---------------------------------------------------------------------------


def sm(src: str) -> list[str]:
    return plain_lines(strip_markdown(parse(src)))


def test_strip_markdown_headings():
    assert sm("# Title\n## Sub\n###### Six\n") == ["Title", "Sub", "Six"]
    assert sm("## Closed ##\n") == ["Closed"]
    assert sm("#NotAHeading\n") == ["#NotAHeading"]


def test_strip_markdown_emphasis_sets_attrs():
    line = strip_markdown(parse("a **bold** b"))[0]
    assert plain_text(line) == "a bold b"
    bold = [r for r in line if r.style.bold]
    assert [r.text for r in bold] == ["bold"]

    line = strip_markdown(parse("a *ital* b"))[0]
    assert plain_text(line) == "a ital b"
    assert [r.text for r in line if r.style.italic] == ["ital"]

    line = strip_markdown(parse("__b__ and _i_"))[0]
    assert plain_text(line) == "b and i"
    assert [r.text for r in line if r.style.bold] == ["b"]
    assert [r.text for r in line if r.style.italic] == ["i"]


def test_strip_markdown_nested_emphasis():
    line = strip_markdown(parse("***both***"))[0]
    assert plain_text(line) == "both"
    assert any(r.style.bold and r.style.italic for r in line)


def test_strip_markdown_leaves_snake_case_and_arithmetic_alone():
    assert sm("call some_long_name(x) now") == ["call some_long_name(x) now"]
    assert sm("2*3*4 is twelve... no, 24") == ["2*3*4 is twelve... no, 24"]


def test_strip_markdown_code_spans():
    assert sm("use `printf(\"%d\", n)` here") == ['use printf("%d", n) here']
    # syntax characters inside a code span are protected
    assert sm("`a *b* c`") == ["a *b* c"]
    assert sm("``a `b` c``") == ["a `b` c"]


def test_strip_markdown_links_and_images():
    line = strip_markdown(parse("see [the docs](https://example.com/d) now"))[0]
    assert plain_text(line) == "see the docs now"
    assert [r.text for r in line if r.style.href] == ["the docs"]
    assert {r.style.href for r in line if r.style.href} == {"https://example.com/d"}

    assert sm('[t](https://e.test "title")') == ["t"]
    assert sm("![alt text](pic.png)") == ["alt text"]
    assert sm("a [text][ref] b") == ["a text b"]
    assert sm("<https://example.com/auto>") == ["https://example.com/auto"]


def test_strip_markdown_keeps_mdcat_reference_markers_and_block():
    src = "Final paragraph with a ref[2].\n\n[1]: https://example.com/path?a=1&b=2\n"
    assert sm(src) == [
        "Final paragraph with a ref[2].",
        "",
        "[1]: https://example.com/path?a=1&b=2",
    ]


def test_strip_markdown_blockquotes_and_bullets():
    assert sm("> quoted\n>> deep\n") == ["quoted", "deep"]
    assert sm("- one\n* two\n+ three\n") == ["• one", "• two", "• three"]
    assert sm("  - nested\n") == ["  • nested"]
    assert sm("1. first\n2. second\n") == ["1. first", "2. second"]


def test_strip_markdown_rules_and_table_separators():
    out = sm("a\n\n---\n\nb\n")
    assert out[0] == "a" and out[-1] == "b"
    assert set(out[2]) == {"─"}
    assert set(sm("***\n")[0]) == {"─"}
    assert set(sm("___\n")[0]) == {"─"}
    assert set(sm("|---|-----|\n")[0]) == {"─"}
    assert is_rule_line(strip_markdown(parse("---"))[0])


def test_strip_markdown_leaves_fenced_code_verbatim():
    src = "before\n```python\n# a comment *not* emphasis\nx = a_b_c\n```\nafter\n"
    assert sm(src) == [
        "before", "", "# a comment *not* emphasis", "x = a_b_c", "", "after"]


def test_strip_markdown_backslash_escapes():
    assert sm(r"literal \*stars\* here") == ["literal *stars* here"]
    assert sm(r"\# not a heading") == ["# not a heading"]


def test_strip_markdown_does_not_mangle_bare_urls():
    # A URL is not markdown: its punctuation must survive the emphasis rules.
    assert sm("[1]: https://ex.test/a/*star*/b") == ["[1]: https://ex.test/a/*star*/b"]
    assert sm("[2]: https://ex.test/q?x=~~y~~") == ["[2]: https://ex.test/q?x=~~y~~"]
    assert sm("see https://ex.test/_a_/end now") == ["see https://ex.test/_a_/end now"]
    assert sm("ftp://h.test/**x**") == ["ftp://h.test/**x**"]


def test_strip_markdown_strikethrough():
    assert sm("~~gone~~ but visible") == ["gone but visible"]


def test_strip_markdown_preserves_blank_lines_and_line_count():
    src = "# H\n\npara\n\n- a\n- b\n"
    assert len(strip_markdown(parse(src))) == len(parse(src))
    assert sm(src) == ["H", "", "para", "", "• a", "• b"]


def test_strip_markdown_keeps_run_structure_and_styles():
    lines = parse("\x1b[1mkeep\x1b[0m me")           # already-styled runs
    out = strip_markdown(lines)
    assert plain_text(out[0]) == "keep me"
    assert out[0][0].style.bold is True


def test_strip_markdown_never_leaves_escapes_or_newlines():
    src = "# H *i* `c` [l](u)\n\n> q\n- b\n"
    for line in strip_markdown(parse(src)):
        for r in line:
            assert "\x1b" not in r.text and "\n" not in r.text
            assert r.text != ""


# ---------------------------------------------------------------------------
# speakability helpers
# ---------------------------------------------------------------------------


def test_plain_text_and_plain_lines():
    lines = parse("\x1b[1mA\x1b[0mB\nC")
    assert plain_text(lines[0]) == "AB"
    assert plain_lines(lines) == ["AB", "C"]
    assert plain_text("already plain") == "already plain"


def test_is_rule_line():
    assert is_rule_line(parse("═" * 78)[0])
    assert is_rule_line(parse("─" * 34)[0])
    assert is_rule_line(parse("│ │")[0])
    assert is_rule_line(parse("|---|---|")[0])
    assert not is_rule_line(parse("")[0] if parse("") else [])
    assert not is_rule_line(parse("real words")[0])
    assert not is_rule_line(parse("   ")[0])
    assert not is_rule_line(parse("• a bullet item")[0])


def test_is_reference_line():
    assert is_reference_line(parse("[1]: https://example.com/path?a=1&b=2")[0])
    assert is_reference_line("[2]: https://example.org/reference")
    assert not is_reference_line("see ref[2]. and more")
    assert not is_reference_line("[not a ref] just brackets")


def test_nonspeakable_spans_reference_definition_covers_whole_line():
    text = "[1]: https://example.com/x"
    assert nonspeakable_spans(text) == [(0, len(text))]


def test_nonspeakable_spans_markers_urls_and_decoration():
    text = "See the docs[1] at https://example.com/a?b=1 now"
    spans = nonspeakable_spans(text)
    covered = {text[s:e] for s, e in spans}
    assert "[1]" in covered
    assert "https://example.com/a?b=1" in covered
    kept = "".join(
        text[s:e] for s, e in _complement(spans, len(text)))
    assert "See the docs" in kept and "now" in kept


def test_nonspeakable_spans_box_drawing_and_bullets():
    text = "• item"
    assert nonspeakable_spans(text) == [(0, 1)]
    rule = "═" * 78
    assert nonspeakable_spans(rule) == [(0, 78)]
    row = "| a | b |"
    assert [row[s:e] for s, e in nonspeakable_spans(row)] == ["|", "|", "|"]
    assert nonspeakable_spans("plain words only") == []
    assert nonspeakable_spans("") == []


def test_nonspeakable_spans_merges_overlapping_and_touching_sources():
    # A URL is \S+, so a box-drawing char glued to it lands inside the URL span.
    t = "link https://x.test/a\u2502b end"
    assert nonspeakable_spans(t) == [(5, 23)]
    assert [t[s:e] for s, e in nonspeakable_spans(t)] == ["https://x.test/a\u2502b"]
    # two touching reference markers collapse into one span
    t2 = "ref[1][2] tail"
    assert nonspeakable_spans(t2) == [(3, 9)]


def test_nonspeakable_spans_are_sorted_and_disjoint():
    text = "• [1] https://x.test/a │ [22] tail"
    spans = nonspeakable_spans(text)
    assert spans == sorted(spans)
    for (a, b), (c, d) in zip(spans, spans[1:]):
        assert b < c
        assert 0 <= a < b <= len(text)


def test_nonspeakable_spans_accepts_runs_or_str():
    line = parse("• hello")[0]
    assert nonspeakable_spans(line) == nonspeakable_spans("• hello")


def _complement(spans, n):
    out, pos = [], 0
    for s, e in spans:
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < n:
        out.append((pos, n))
    return out


# ---------------------------------------------------------------------------
# fuzz / robustness
# ---------------------------------------------------------------------------


def test_random_escape_soup_never_crashes_and_never_leaks_escapes():
    rng = random.Random(20260910)
    alphabet = [
        "\x1b", "[", "]", ";", "m", "0", "1", "8", "\\", "\x07", "a", " ",
        "\n", "\t", "\r", "?", "5", "2", ":", "P", "(", "B", "•", "é",
    ]
    for _ in range(3000):
        data = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        lines = parse(data)
        for line in lines:
            for r in line:
                assert "\x1b" not in r.text
                assert "\n" not in r.text
                assert "\r" not in r.text
                assert "\t" not in r.text
                assert r.text != ""
        # strip_markdown must survive the same soup
        for line in strip_markdown(lines):
            for r in line:
                assert "\x1b" not in r.text and "\n" not in r.text


def test_long_input_is_handled():
    src = ("\x1b[1mword\x1b[0m " * 20000) + "\n"
    lines = parse(src)
    assert len(lines) == 1
    assert plain_text(lines[0]).count("word") == 20000


# ---------------------------------------------------------------------------
# live mdcat round trip
# ---------------------------------------------------------------------------

MDCAT = shutil.which("mdcat")

FIXTURE_MD = """\
# Read Aloud Fixture

A paragraph with **bold**, *italic*, `inline code`, ~~strikethrough~~ and a
[labelled link](https://example.com/path?a=1&b=2) inside it.

## Second heading

- first bullet
- second bullet with a [reference](https://example.org/reference)
- third

> A blockquote line.
> A second quoted line.

```python
def main() -> None:
    print("hello, world")  # entry point
```

| Column | Value |
|--------|-------|
| alpha  | 1     |
| beta   | 2     |

---

Final paragraph with an autolink <https://rust-lang.org/> and some trailing text.
"""


def run_mdcat(*args: str, path: str) -> str:
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    proc = subprocess.run(
        [MDCAT, *args, path], capture_output=True, text=True, env=env, check=True)
    return proc.stdout


def naive_plain(data: str) -> list[str]:
    """Deliberately dumb reference implementation: regex the escapes away."""
    s = re.sub(r"\x1b\][^\x07\x1b]*(?:\x1b\\|\x07)", "", data)
    s = re.sub(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]", "", s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    out = []
    for raw in s.split("\n"):
        expanded, col = [], 0
        for ch in raw:
            if ch == "\t":
                pad = TAB_WIDTH - (col % TAB_WIDTH)
                expanded.append(" " * pad)
                col += pad
            else:
                expanded.append(ch)
                col += 1
        out.append("".join(expanded))
    if data.endswith("\n") and out and out[-1] == "":
        out.pop()
    return out


@pytest.fixture(scope="module")
def fixture_md(tmp_path_factory):
    p = tmp_path_factory.mktemp("ansi") / "fixture.md"
    p.write_text(FIXTURE_MD, encoding="utf-8")
    return str(p)


@pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")
def test_mdcat_ansi_plain_text_matches_a_naive_regex_strip(fixture_md):
    out = run_mdcat("--ansi", "--columns", "60", path=fixture_md)
    assert has_ansi(out)
    assert plain_lines(parse(out)) == naive_plain(out)


@pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")
@pytest.mark.parametrize("theme", ["dracula", "solarized-dark"])
def test_mdcat_ansi_themes_also_match(fixture_md, theme):
    out = run_mdcat("--ansi", "--theme", theme, "--columns", "60", path=fixture_md)
    assert plain_lines(parse(out)) == naive_plain(out)
    # themed output is truecolour; every colour must survive as a palette index
    for line in parse(out):
        for r in line:
            for c in (r.style.fg, r.style.bg):
                assert c is None or (isinstance(c, int) and 0 <= c <= 255)


@pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")
def test_mdcat_ansi_hyperlink_urls_are_captured(fixture_md):
    out = run_mdcat("--ansi", "--columns", "60", path=fixture_md)
    lines = parse(out)
    hrefs = {r.style.href for line in lines for r in line if r.style.href}
    assert "https://example.com/path?a=1&b=2" in hrefs
    assert "https://example.org/reference" in hrefs
    assert "https://rust-lang.org/" in hrefs
    linked = [r.text for line in lines for r in line
              if r.style.href == "https://example.com/path?a=1&b=2"]
    assert "".join(linked).strip() == "labelled link"
    # the URL itself is never part of the visible text
    assert "https://example.com/path?a=1&b=2" not in "\n".join(plain_lines(lines))


@pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")
def test_mdcat_ansi_styles_are_recognised(fixture_md):
    out = run_mdcat("--ansi", "--columns", "60", path=fixture_md)
    lines = parse(out)
    runs = [r for line in lines for r in line]
    assert any(r.style.bold for r in runs)
    assert any(r.style.italic for r in runs)
    assert any(r.style.fg is not None for r in runs)
    for r in runs:
        assert "\x1b" not in r.text and "\n" not in r.text and r.text


@pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")
def test_mdcat_ansi_rules_are_detected_as_nonspeakable(fixture_md):
    out = run_mdcat("--ansi", "--columns", "60", path=fixture_md)
    lines = parse(out)
    rules = [ln for ln in lines if is_rule_line(ln)]
    assert rules, "expected mdcat's thematic break / table rules"
    for ln in rules:
        text = plain_text(ln)
        assert nonspeakable_spans(ln) and nonspeakable_spans(ln)[0] != (0, 0)
        covered = sum(e - s for s, e in nonspeakable_spans(ln))
        assert covered >= len(text.strip())


@pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")
def test_mdcat_degraded_output_is_plain_with_reference_block(fixture_md):
    out = run_mdcat("--columns", "60", path=fixture_md)
    assert not has_ansi(out)
    lines = strip_markdown(parse(out))
    plain = plain_lines(lines)
    joined = "\n".join(plain)
    assert "[1]" in joined                      # markers stay visible
    refs = [ln for ln in lines if is_reference_line(ln)]
    assert refs, joined
    for ln in refs:
        assert nonspeakable_spans(ln) == [(0, len(plain_text(ln)))]
    marker_lines = [ln for ln in plain if re.search(r"\[\d+\]", ln)
                    and not is_reference_line(ln)]
    assert marker_lines
    for ln in marker_lines:
        covered = {ln[s:e] for s, e in nonspeakable_spans(ln)}
        assert any(re.fullmatch(r"\[\d+\]", c) for c in covered)


def run_mdcat_on_a_tty(*args: str, path: str) -> str:
    """Run mdcat with stdout attached to a real pty.

    This is the `script -q /dev/null mdcat doc.md | readaloud` path.  mdcat then
    prefixes its output with BEL-terminated OSC 10/11 capability probes and a
    bare `CSI c`, and the pty rewrites every line ending to CRLF.
    """
    import pty
    import select

    mfd, sfd = pty.openpty()
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    proc = subprocess.Popen(
        [MDCAT, *args, path], stdout=sfd, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, env=env)
    os.close(sfd)
    buf = b""
    try:
        while True:
            ready, _, _ = select.select([mfd], [], [], 5.0)
            if not ready:
                break
            try:
                chunk = os.read(mfd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    finally:
        proc.wait(timeout=10)
        os.close(mfd)
    return buf.decode("utf-8", "replace")


@pytest.mark.skipif(MDCAT is None, reason="mdcat is not installed")
def test_mdcat_on_a_tty_probes_and_crlf_are_handled(fixture_md):
    tty_out = run_mdcat_on_a_tty("--ansi", "--columns", "60", path=fixture_md)
    # the exact prefix the ansi-colour spike measured
    assert tty_out.startswith("\x1b]10;?\x07\x1b]11;?\x07\x1b[c")
    assert "\r" in tty_out

    lines = parse(tty_out)
    for line in lines:
        for r in line:
            assert "\x1b" not in r.text
            assert "\r" not in r.text
            assert "\x07" not in r.text
    # the probes and the CRLFs must not change a single visible character
    piped = run_mdcat("--ansi", "--columns", "60", path=fixture_md)
    assert plain_lines(lines) == plain_lines(parse(piped))
    hrefs = {r.style.href for line in lines for r in line if r.style.href}
    assert "https://example.com/path?a=1&b=2" in hrefs
