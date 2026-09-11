"""Tests for readaloud.document.

Plain ``assert`` statements only -- no pytest fixtures, parametrisation or
imports -- so the file runs under ``pytest`` *and* directly with
``uv run python tests/test_document.py`` (pytest is not currently a dependency
of this project).
"""

from __future__ import annotations

import copy
import os
import sys
from bisect import bisect_left

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from readaloud import ansi                                       # noqa: E402
from readaloud.document import (                                 # noqa: E402
    ABBREVIATIONS,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_SENTENCES,
    Chunk,
    Document,
    Table,
    TableCell,
    Word,
    build_chunks,
    extract_words,
    sentence_spans,
    split_words,
    line_word_spans,
)
from readaloud.width import cell_offsets                         # noqa: E402

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def doc(text: str, **kw) -> Document:
    return Document.from_text(text, **kw)


def check_invariants(d: Document) -> None:
    """Every structural guarantee document.py makes, checked at once."""
    flat = "\n".join(d.plain)
    owner = {widx: c for c in d.chunks for widx in c.words}

    # -- words -------------------------------------------------------------
    prev = (-1, -1, -1, -1)
    for i, w in enumerate(d.words):
        assert w.idx == i, f"word {i} carries idx {w.idx}"
        assert 0 <= w.line < len(d.plain), f"word {i} line {w.line} out of range"
        assert 0 <= w.start < w.end <= len(d.plain[w.line])
        assert d.plain[w.line][w.start:w.end] == w.text, (
            f"word {i} {w.text!r} does not round-trip against its line")
        assert w.text.strip() == w.text, f"word {i} {w.text!r} has edge space"
        # reading order: line order, but row by row then cell by cell in a table
        c = owner.get(i)
        if c is not None and c.kind == "cell":
            t, row, col = c.cell
            key = (d.tables[t].rows[row][0], col, w.line, w.start)
        else:
            key = (w.line, 0, w.line, w.start)
        assert key > prev, "words are not in reading order"
        prev = key

    # -- chunk text / offsets ---------------------------------------------
    line_off = []
    pos = 0
    for line in d.plain:
        line_off.append(pos)
        pos += len(line) + 1

    # (table, row) -> the last column of the row with something to say
    last_spoken: dict[tuple[int, int], int] = {}
    for c in d.chunks:
        if c.kind == "cell" and c.words:
            last_spoken[c.cell[:2]] = max(last_spoken.get(c.cell[:2], -1),
                                          c.cell[2])

    seen: list[int] = []
    for c in d.chunks:
        assert c.idx == d.chunks.index(c)
        assert len(c.offsets) == len(c.words) == len(c.word_texts)
        assert c.speakable == bool(c.words)
        assert 0 <= c.line_start < c.line_end <= len(d.plain)

        if c.kind == "cell":
            # built from the cell's trimmed segments, so not a verbatim slice:
            # every word sits at its offset and inside one of the regions
            t, row, col = c.cell
            table = d.tables[t]
            cell = table.cells[row * table.ncols + col]
            assert (c.line_start, c.line_end) == tuple(table.rows[row])
            assert c.regions == [tuple(s) for s in cell.spans]
            # the beat between rows follows the last cell that is read
            assert c.row_end == (col == last_spoken.get((t, row), -1)), (
                f"cell chunk {c.idx} {c.cell}: row_end {c.row_end}")
            for widx in c.words:
                w = d.words[widx]
                assert any(line == w.line and s <= w.start and w.end <= e
                           for line, s, e in c.regions), (
                    f"cell chunk {c.idx}: {w.text!r} outside its regions")
        else:
            # chunk.text must be a VERBATIM slice of the flattened document,
            # positioned on the lines the chunk claims.
            assert not c.regions and c.cell is None
            if c.words:
                w0 = d.words[c.words[0]]
                base = line_off[w0.line] + w0.start - c.offsets[0]
            else:
                base = flat.find(c.text, line_off[c.line_start])
            assert base >= 0
            assert flat[base:base + len(c.text)] == c.text, (
                f"chunk {c.idx} text is not a verbatim slice of the document")
            assert line_off[c.line_start] <= base, (
                f"chunk {c.idx} starts before the line it claims")
            end = base + len(c.text)
            assert end <= line_off[c.line_end - 1] + len(d.plain[c.line_end - 1]), (
                f"chunk {c.idx} runs past the line it claims")
        if c.kind == "rule":
            assert not c.words and c.line_end == c.line_start + 1
        for slot, widx in enumerate(c.words):
            w = d.words[widx]
            off = c.offsets[slot]
            assert 0 <= off <= len(c.text)
            assert c.text[off:off + len(w.text)] == w.text, (
                f"chunk {c.idx} slot {slot}: {w.text!r} not at offset {off}")
            assert c.word_texts[slot] == w.text
            assert d.chunk_of_word(widx) == c.idx
            assert d.slot_of_word(widx) == slot
            assert d.word_span_in_chunk(widx) == (off, off + len(w.text))
        # contiguous and ascending
        if c.words:
            assert c.words == list(range(c.words[0], c.words[0] + len(c.words)))
            assert c.offsets == sorted(c.offsets)
        seen.extend(c.words)

    # -- coverage ----------------------------------------------------------
    assert seen == list(range(len(d.words))), (
        "chunks do not cover every word exactly once, in order")
    assert [c.cell for c in d.chunks if c.kind == "cell"] == [
        (t, r, k) for t, table in enumerate(d.tables)
        for r in range(len(table.rows)) for k in range(table.ncols)
    ], "the cell chunks are not every cell of every table, in reading order"

    # -- line coverage -----------------------------------------------------
    if d.chunks:
        assert d.chunks[0].line_start == 0
        assert d.chunks[-1].line_end == len(d.plain)
        covered: set[int] = set()
        for a, b in zip(d.chunks, d.chunks[1:]):
            if a.kind == b.kind == "cell" and a.cell[:2] == b.cell[:2]:
                # cells of one row share the row's lines
                assert a.line_span == b.line_span, (
                    f"cells {a.idx} and {b.idx} of one row claim different lines")
            elif "cell" in (a.kind, b.kind):
                # the next row, or a rule, starts where the row ends
                assert b.line_start == a.line_end, (
                    f"gap between chunk {a.idx} and {b.idx}")
            else:
                # consecutive chunks are adjacent, or share the one line a
                # sentence boundary fell inside
                assert b.line_start in (a.line_end - 1, a.line_end), (
                    f"gap between chunk {a.idx} and {b.idx}")
        first_cover: dict[int, int] = {}
        for c in d.chunks:
            covered.update(range(c.line_start, c.line_end))
            for line in range(c.line_start, c.line_end):
                first_cover.setdefault(line, c.idx)
        assert covered == set(range(len(d.plain))), "chunks do not cover all lines"
        assert [d.chunk_at_line(i) for i in range(len(d.plain))] == [
            first_cover[i] for i in range(len(d.plain))], (
            "chunk_at_line is not the first chunk covering the line")


def texts(d: Document) -> list[str]:
    return [w.text for w in d.words]


# ---------------------------------------------------------------------------
# word segmentation
# ---------------------------------------------------------------------------


def test_word_offsets_roundtrip_against_plain_lines():
    d = doc("The quick brown fox.\nIt jumped over 2 lazy dogs!\n\nDone.")
    assert texts(d) == ["The", "quick", "brown", "fox", "It", "jumped", "over",
                        "2", "lazy", "dogs", "Done"]
    assert [(w.line, w.start, w.end) for w in d.words[:4]] == [
        (0, 0, 3), (0, 4, 9), (0, 10, 15), (0, 16, 19)]
    check_invariants(d)


def test_contractions_stay_one_word():
    assert split_words("it's don't y'all o'clock can’t") == [
        "it's", "don't", "y'all", "o'clock", "can’t"]


def test_hyphenated_words_stay_one_word():
    assert split_words("a well-known state-of-the-art e-mail re-run") == [
        "a", "well-known", "state-of-the-art", "e-mail", "re-run"]
    # an em/en dash is a separator, not part of the word
    assert split_words("one—two three – four") == ["one", "two", "three", "four"]
    # a double hyphen separates
    assert split_words("open--close") == ["open", "close"]


def test_money_decimals_and_percentages():
    assert split_words("$4.50 costs 12.5% of £3 or €1,000,000 (3.14159)") == [
        "$4.50", "costs", "12.5%", "of", "£3", "or", "€1,000,000", "3.14159"]


def test_dates_times_and_versions():
    assert split_words("On 2026-09-10 at 3:45pm, v1.2.3 shipped 24/7.") == [
        "On", "2026-09-10", "at", "3:45pm", "v1.2.3", "shipped", "24/7"]


def test_identifiers_and_initialisms():
    assert split_words("import mlx_audio; e.g. the U.S. and U.S.A a.m. p.m.") == [
        "import", "mlx_audio", "e.g.", "the", "U.S.", "and", "U.S.A", "a.m.",
        "p.m."]
    # leading/trailing underscores (markdown emphasis) are not part of the word
    assert split_words("_italic_ __bold__ mlx_audio_v2") == [
        "italic", "bold", "mlx_audio_v2"]


def test_known_abbreviations_keep_their_dot():
    assert split_words("Dr. Smith saw Fig. 3 in Vol. 2, etc.") == [
        "Dr.", "Smith", "saw", "Fig.", "3", "in", "Vol.", "2", "etc."]
    # a word that merely starts like an abbreviation is untouched
    assert split_words("The casino. Nope. Coding.") == [
        "The", "casino", "Nope", "Coding"]
    # a domain is not an abbreviation followed by a word
    assert split_words("example.co.uk") == ["example.co.uk"]


def test_surrounding_punctuation_is_stripped_from_the_span():
    line = 'He said, "don\'t!" (really) -- [see] {this}, ok?'
    assert split_words(line) == ["He", "said", "don't", "really", "see",
                                 "this", "ok"]
    d = doc(line)
    # ...but every one of those characters is still in the spoken text
    assert d.chunks[0].text == line
    check_invariants(d)


def test_urls_and_emails_are_single_words():
    assert split_words("See https://example.com/a?b=1&c=2. Mail reader@example.com.") == [
        "See", "https://example.com/a?b=1&c=2", "Mail", "reader@example.com"]
    assert split_words("(https://example.org/x)") == ["https://example.org/x"]
    assert split_words("go to www.example.com now") == [
        "go", "to", "www.example.com", "now"]


def test_decorations_produce_no_words():
    for junk in ["─" * 80, "═" * 78, "│ │ │",
                 "|---|---|", "• " * 200, "***", "   ", ">>>", "..."]:
        assert line_word_spans(junk) == [], f"{junk[:12]!r} produced words"


def test_unicode_accents_and_nbsp():
    assert split_words("Résumé naïve café bar") == [
        "Résumé", "naïve", "café", "bar"]


def test_reference_definition_lines_are_not_spoken():
    d = doc("Body text[1] here.\n\n[1]: https://example.com/path?a=1&b=2")
    assert texts(d) == ["Body", "text", "here"]
    assert d.chunks[-1].speakable is False
    check_invariants(d)


# ---------------------------------------------------------------------------
# mdcat's degraded (non-tty) `text[1]` link markers
# ---------------------------------------------------------------------------


MDCAT_PLAIN = (
    " Notes \n"
    "\n"
    "Visit the project homepage[1] for details, or read the reference"
    " manual[2] instead.\n"
    "\n"
    "That is all.\n"
    "\n"
    "[1]: https://example.com/home\n"
    "[2]: https://example.com/ref\n"
)


def test_inline_reference_markers_are_never_spoken():
    """`mdcat notes.md | readaloud`: the `[1]` markers must not reach the TTS.

    They used to segment into a bare digit that Kokoro pronounced ("read the
    reference manual two instead").
    """
    d = doc(MDCAT_PLAIN)
    assert texts(d) == [
        "Notes", "Visit", "the", "project", "homepage", "for", "details", "or",
        "read", "the", "reference", "manual", "instead", "That", "is", "all"]
    body = [c for c in d.chunks if c.speakable and "Visit" in c.text][0]
    assert body.text == (
        "Visit the project homepage for details, or read the reference"
        " manual instead.")
    assert "[1]" not in body.text and "[2]" not in body.text
    # the trailing reference block still exists as a non-speakable chunk
    assert any("https://example.com/home" in c.text and not c.speakable
               for c in d.chunks)
    check_invariants(d)


def test_inline_marker_strip_matches_the_ansi_path():
    """The plain-mdcat pipe now yields exactly what `mdcat --ansi` yields."""
    plain = doc(MDCAT_PLAIN)
    ansi_like = doc(
        " Notes \n"
        "\n"
        "Visit the project homepage for details, or read the reference"
        " manual instead.\n"
        "\n"
        "That is all.\n")
    assert texts(plain) == texts(ansi_like)


def test_marker_strip_keeps_display_and_click_coordinates_consistent():
    d = doc(MDCAT_PLAIN)
    line = [i for i, t in enumerate(d.plain) if t.startswith("Visit")][0]
    assert "[1]" not in d.plain[line]
    # clicking the word right after the removed marker still hits that word
    col = d.plain[line].index("for")
    widx = d.word_at(line, col)
    assert widx is not None and d.words[widx].text == "for"
    check_invariants(d)


def test_marker_strip_preserves_run_styles():
    style = ansi.Style(bold=True)
    other = ansi.Style(italic=True)
    lines = [
        [ansi.Run("see the ", style), ansi.Run("homepage[1]", other),
         ansi.Run(" now.", style)],
        [],
        [ansi.Run("[1]: https://example.com", ansi.Style())],
    ]
    d = Document(lines)
    assert d.plain[0] == "see the homepage now."
    assert [(r.text, r.style) for r in d.lines[0]] == [
        ("see the ", style), ("homepage", other), (" now.", style)]
    check_invariants(d)


def test_marker_split_across_runs_is_still_removed():
    style = ansi.Style()
    lines = [
        [ansi.Run("homepage[", style), ansi.Run("1", style),
         ansi.Run("] here.", style)],
        [],
        [ansi.Run("[1]: https://example.com", style)],
    ]
    d = Document(lines)
    assert d.plain[0] == "homepage here."
    assert texts(d) == ["homepage", "here"]
    check_invariants(d)


def test_markers_are_kept_without_a_matching_reference_definition():
    """No reference block, or a different label: leave the text alone."""
    d = doc("Body text[1] here.")
    assert texts(d) == ["Body", "text", "1", "here"]
    d2 = doc("Body text[7] here.\n\n[1]: https://example.com")
    assert "[7]" in d2.plain[0]
    assert "7" in texts(d2)
    check_invariants(d)
    check_invariants(d2)


def test_markers_inside_code_blocks_survive():
    """`buf[1]` in code is real source, not an mdcat link marker."""
    d = doc(
        "Intro text[1] here.\n"
        "\n"
        "```\n"
        "print(buf[1])\n"
        "```\n"
        "\n"
        "    other = buf[1]\n"
        "\n"
        "[1]: https://example.com\n")
    joined = "\n".join(d.plain)
    assert "print(buf[1])" in joined
    assert "other = buf[1]" in joined
    assert "Intro text here." in joined
    check_invariants(d)


def test_marker_strip_is_a_no_op_for_ordinary_documents():
    text = ("An array index buf[1] and a matrix m[0][1] stay put.\n"
            "\n"
            "So does a footnote-looking [1] on its own.\n")
    d = doc(text)
    assert "buf[1]" in d.plain[0] and "m[0][1]" in d.plain[0]
    assert "[1]" in d.plain[2]
    check_invariants(d)


# ---------------------------------------------------------------------------
# sentence segmentation / abbreviations
# ---------------------------------------------------------------------------


def test_sentence_spans_basic():
    text = "One two. Three four! Five six? Seven."
    spans = sentence_spans(text)
    assert [text[a:b] for a, b in spans] == [
        "One two.", "Three four!", "Five six?", "Seven."]


def test_abbreviations_do_not_break_sentences():
    text = ("Dr. Smith met Mr. Jones in the U.S. on Mon. See Fig. 3 and e.g. "
            "the appendix. Done.")
    spans = sentence_spans(text)
    assert [text[a:b] for a, b in spans] == [
        "Dr. Smith met Mr. Jones in the U.S. on Mon. See Fig. 3 and e.g. "
        "the appendix.",
        "Done.",
    ]


def test_initials_and_numbered_list_markers_do_not_break():
    text = "J. R. R. Tolkien wrote it. 1. First item. 2. Second item."
    spans = sentence_spans(text)
    rendered = [text[a:b] for a, b in spans]
    assert rendered[0] == "J. R. R. Tolkien wrote it."
    assert "1. First item." in rendered
    assert "2. Second item." in rendered


def test_lowercase_continuation_does_not_break():
    text = "Version 1.2. then it continued. Next."
    assert [text[a:b] for a, b in sentence_spans(text)] == [
        "Version 1.2. then it continued.", "Next."]


def test_closing_quote_stays_with_the_sentence():
    text = 'He said "stop." Then he left.'
    assert [text[a:b] for a, b in sentence_spans(text)] == [
        'He said "stop."', "Then he left."]


def test_abbreviation_table_is_lowercase_and_dotless():
    for a in ABBREVIATIONS:
        assert a == a.lower()
        assert not a.endswith(".")
    for expected in ("dr", "mr", "fig", "e.g", "i.e", "u.s", "etc", "vs"):
        assert expected in ABBREVIATIONS


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


def test_paragraphs_break_on_blank_lines():
    d = doc("First para.\nStill first.\n\nSecond para.")
    speak = [c for c in d.chunks if c.speakable]
    assert len(speak) == 2
    assert speak[0].text == "First para.\nStill first."
    assert speak[1].text == "Second para."
    assert speak[0].line_start == 0 and speak[0].line_end == 2
    assert speak[1].line_start == 3 and speak[1].line_end == 4
    check_invariants(d)


def test_split_when_more_than_max_sentences():
    d = doc("One. Two. Three. Four. Five. Six. Seven.")
    speak = [c.text for c in d.chunks if c.speakable]
    assert speak == ["One. Two. Three. Four.", "Five. Six. Seven."]
    check_invariants(d)


def test_max_sentences_is_configurable():
    d = doc("One. Two. Three. Four.", max_sentences=2)
    assert [c.text for c in d.chunks if c.speakable] == ["One. Two.",
                                                         "Three. Four."]
    check_invariants(d)


def test_split_when_longer_than_max_chars():
    sentence = "This sentence is quite a long one with plenty of filler words. "
    d = doc((sentence * 12).strip())
    speak = [c for c in d.chunks if c.speakable]
    assert len(speak) > 1
    for c in speak:
        assert len(c.text) <= DEFAULT_MAX_CHARS
        assert len(sentence_spans(c.text)) <= DEFAULT_MAX_SENTENCES
    check_invariants(d)


def test_chunks_never_split_mid_word():
    body = " ".join(f"token{i}" for i in range(600))
    d = doc(body)
    flat = "\n".join(d.plain)
    for c in d.chunks:
        start = flat.index(c.text)
        end = start + len(c.text)
        assert start == 0 or flat[start - 1].isspace()
        assert end == len(flat) or flat[end].isspace()
        assert not c.text[:1].isspace() and not c.text[-1:].isspace()
    check_invariants(d)


def test_chunk_offsets_roundtrip_against_chunk_text():
    d = doc("Alpha beta gamma. Delta epsilon!\n\nZeta $1.50 eta-theta.\n\n"
            "```\ncode_here(1)\n```\n")
    for c in d.chunks:
        for slot, widx in enumerate(c.words):
            w = d.words[widx]
            off = c.offsets[slot]
            assert c.text[off:off + len(w.text)] == w.text
    check_invariants(d)


def test_fenced_code_block_is_one_chunk():
    d = doc("Intro.\n\n```python\ndef f():\n    return 1\n\n    # trailing\n```"
            "\n\nOutro.")
    code = [c for c in d.chunks if "def f" in c.text]
    assert len(code) == 1
    assert code[0].kind == "code"
    assert code[0].text.startswith("```python")
    assert code[0].text.endswith("```")
    assert code[0].line_start == 2 and code[0].line_end == 8
    check_invariants(d)


def test_indented_code_block_is_one_chunk():
    d = doc("Intro:\n\n    def f():\n        return 1\n\n    x = 2\n\nOutro.")
    code = [c for c in d.chunks if "def f" in c.text]
    assert len(code) == 1
    assert code[0].kind == "code"
    # indentation is preserved verbatim
    assert code[0].text.startswith("    def f():")
    assert "\n\n    x = 2" in code[0].text
    check_invariants(d)


def test_indented_list_item_is_not_mistaken_for_code():
    d = doc("Intro:\n\n    - nested bullet one. Two. Three. Four. Five.\n"
            "    - nested bullet two.")
    kinds = {c.kind for c in d.chunks if c.speakable}
    assert "code" not in kinds
    check_invariants(d)


def test_long_code_block_is_never_split():
    body = "\n".join(f"    line_{i} = compute({i})" for i in range(60))
    d = doc("Intro:\n\n" + body)
    code = [c for c in d.chunks if c.kind == "code"]
    assert len(code) == 1
    assert len(code[0].text) > DEFAULT_MAX_CHARS
    check_invariants(d)


def test_table_row_group_is_one_chunk():
    table = (" Name   Value \n" + "─" * 15 + "\n a      1     \n"
             " b      2     ")
    d = doc("Intro.\n\n" + table)
    speak = [c for c in d.chunks if c.speakable]
    assert speak[-1].text.count("\n") >= 2      # the rows stayed together
    assert "Name" in speak[-1].text and "b" in speak[-1].text
    check_invariants(d)


def test_nonspeakable_chunks_are_retained_and_flagged():
    d = doc("Title\n\n" + "═" * 78 + "\n\nBody.")
    assert len(d.chunks) == 5
    assert [c.speakable for c in d.chunks] == [True, False, False, False, True]
    rule = d.chunks[2]
    assert rule.text == "═" * 78          # still visible
    assert rule.words == []
    assert d.speakable_chunks == [0, 4]
    check_invariants(d)


def test_word_free_paragraph_is_never_split():
    d = doc("• " * 200)
    assert len(d.chunks) == 1
    assert d.chunks[0].speakable is False
    assert len(d.chunks[0].text) > DEFAULT_MAX_CHARS
    check_invariants(d)


def test_build_chunks_is_callable_standalone():
    plain = ["Hello there.", "", "Second one."]
    words = extract_words(plain)
    chunks = build_chunks(plain, words, max_sentences=4, max_chars=380)
    assert [c.text for c in chunks] == ["Hello there.", "", "Second one."]
    assert isinstance(chunks[0], Chunk) and isinstance(words[0], Word)


# ---------------------------------------------------------------------------
# lookups
# ---------------------------------------------------------------------------


def test_word_at_hit_testing():
    d = doc("Hello brave world.")
    #        0123456789...
    assert d.word_at(0, 0) == 0
    assert d.word_at(0, 4) == 0          # last char of "Hello"
    assert d.word_at(0, 5) is None       # the space
    assert d.word_at(0, 6) == 1
    assert d.word_at(0, 10) == 1         # last char of "brave"
    assert d.word_at(0, 11) is None      # the space
    assert d.word_at(0, 12) == 2         # "world" starts at 12
    assert d.word_at(0, 16) == 2
    assert d.word_at(0, 17) is None      # the full stop
    assert d.word_at(0, 999) is None
    assert d.word_at(9, 0) is None       # no such line
    assert d.word_at(-1, 0) is None


def test_word_at_across_lines_and_blank_lines():
    d = doc("alpha beta\n\ngamma")
    assert d.word_at(0, 0) == 0
    assert d.word_at(0, 6) == 1
    assert d.word_at(1, 0) is None       # blank line
    assert d.word_at(2, 2) == 2
    for w in d.words:
        for col in range(w.start, w.end):
            assert d.word_at(w.line, col) == w.idx


def test_nearest_word_snaps_to_the_line():
    d = doc("alpha    beta")
    assert d.nearest_word(0, 6) in (0, 1)
    assert d.nearest_word(0, 0) == 0
    assert d.nearest_word(0, 99) == 1
    assert d.nearest_word(1, 0) is None


def test_chunk_of_word_and_slot_of_word():
    d = doc("One. Two. Three. Four. Five. Six.")
    assert d.chunk_of_word(0) == 0
    assert d.slot_of_word(0) == 0
    last = len(d.words) - 1
    assert d.chunk_of_word(last) == d.chunks[-1].idx
    assert d.slot_of_word(last) == len(d.chunks[-1].words) - 1
    for c in d.chunks:
        for slot, widx in enumerate(c.words):
            assert d.chunk_of_word(widx) == c.idx and d.slot_of_word(widx) == slot


def test_chunk_at_line():
    d = doc("Para one.\nStill one.\n\nPara two.")
    assert d.chunk_at_line(0) == 0
    assert d.chunk_at_line(1) == 0
    assert d.chunk_at_line(2) == 1
    assert d.chunk_at_line(3) == 2
    assert d.chunk_at_line(99) is None
    # when two chunks share a line, the first one wins
    d2 = doc("One. Two. Three. Four. Five.")
    assert d2.chunk_at_line(0) == 0


def test_next_and_prev_speakable_chunk():
    d = doc("Title\n\n" + "─" * 40 + "\n\nBody.")
    assert d.first_speakable_chunk() == 0
    assert d.next_speakable_chunk(0) == 4
    assert d.next_speakable_chunk(4) is None
    assert d.next_speakable_chunk(4, wrap=True) == 0
    assert d.prev_speakable_chunk(4) == 0
    assert d.prev_speakable_chunk(0) is None
    assert d.prev_speakable_chunk(0, wrap=True) == 4


def test_chunk_spans_helper_matches_offsets():
    d = doc("Alpha beta.\n\nGamma delta epsilon.")
    for c in d.chunks:
        for (a, b), t in zip(c.spans(), c.word_texts):
            assert c.text[a:b] == t


# ---------------------------------------------------------------------------
# tables: hand-built Table objects over mdcat --ansi output
# ---------------------------------------------------------------------------


def grid(plain: list[str], top: int, rows: list[tuple[int, int]],
         starts: list[int], widths: list[int],
         joins: dict[tuple[int, int], list[str]] | None = None) -> Table:
    """A Table over `plain`, mapped from the columns mdcat drew.

    `rows` are the (first, end) line ranges, header row first; `starts` and
    `widths` are each column's display columns.  Every cell gets a span on
    every line of its row, converted to char offsets and clamped to the line.
    `joins` maps (row, col) to that cell's joins.
    """
    cells = []
    for r, (first, stop) in enumerate(rows):
        for c, (x, w) in enumerate(zip(starts, widths)):
            spans = []
            for line in range(first, stop):
                cols = cell_offsets(plain[line])
                n = len(plain[line])
                spans.append((line, min(n, bisect_left(cols, x)),
                              min(n, bisect_left(cols, x + w))))
            cells.append(TableCell(r, c, spans,
                                   list((joins or {}).get((r, c), ()))))
    bottom = rows[-1][1] if len(rows) > 1 else rows[0][1] + 1
    return Table(top, bottom + 1, len(starts), list(rows), cells)


def shape(d: Document) -> list[tuple]:
    return [(c.kind, c.text, c.line_span, c.words, c.cell) for c in d.chunks]


def cells_of(d: Document) -> dict[tuple[int, int], Chunk]:
    """The cell chunks of the first table, by (row, col)."""
    return {c.cell[1:]: c for c in d.chunks
            if c.kind == "cell" and c.cell[0] == 0}


# mdcat --ansi --columns 40 of
#   | Name | Role | Notes |
#   |------|:----:|------:|
#   | Alice | Engineer | short |
#   | Bob | Designer with a very long title that wraps around the column | code here |
BASIC = [
    "",
    "─" * 40,
    " Name             Role            Notes ",
    "─" * 40,
    " Alice          Engineer          short ",
    " Bob      Designer with a very     code ",
    "         long title that wraps     here ",
    "           around the column            ",
    "─" * 40,
    "",
]


def basic_table() -> Table:
    return grid(BASIC, 1, [(2, 3), (4, 5), (5, 8)], [1, 8, 34], [5, 24, 5])


# mdcat --ansi --columns 40 of a middle column that wraps prose, paths, a URL,
# a word too long for the column, Japanese, and runs of spaces
WRAP = [
    "",
    "─" * 40,
    " id   cell                            z ",
    "─" * 40,
    " r1   alpha beta gamma delta epsilon  Z ",
    "      zeta eta theta iota kappa         ",
    "      lambda                            ",
    " r2   well-known state-of-the-art     Z ",
    "      hyphen-separated                  ",
    "      compound-words here               ",
    " r3   path/to/some/deeply/nested/     Z ",
    "      file/name.txt and more/           ",
    "      slashes/here                      ",
    " r4   https://example.com/a/very/     Z ",
    "      long/url/that/cannot/fit/in/      ",
    "      the/column/at/all                 ",
    " r5   Supercalifragilisticexpialidoc  Z ",
    "      iousandevenlongerwordwithoutbr    ",
    "      eaks end                          ",
    " r6   日本語のテキストはスペースなし  Z ",
    "      で折り返されるべきですかどうか    ",
    "      確認します                        ",
    " r7   double  spaced   words    here  Z ",
    "      and      more       spaces        ",
    " r8   bold words that wrap across     Z ",
    "      lines in the narrow column        ",
    "      here                              ",
    " r9   em—dash—joined—words—and en–    Z ",
    "      dash–joined–words–too             ",
    " r10  a_b_c_d_e_f                     Z ",
    "      underscores_joined_words_long_    ",
    "      enough_to_wrap maybe              ",
    " r11  comma,separated,values,without  Z ",
    "      ,spaces,long,enough,to,wrap       ",
    "─" * 40,
    "",
]


def wrap_table() -> Table:
    rows = [(2, 3), (4, 7), (7, 10), (10, 13), (13, 16), (16, 19), (19, 22),
            (22, 24), (24, 27), (27, 29), (29, 32), (32, 34)]
    # "" where the source has no space at the wrap
    joins = {(3, 1): ["", "", ""], (4, 1): ["", "", ""], (5, 1): ["", "", ""],
             (6, 1): ["", "", ""], (9, 1): ["", ""], (10, 1): ["", " ", ""],
             (11, 1): ["", ""]}
    return grid(WRAP, 1, rows, [1, 6, 38], [3, 30, 1], joins)


# mdcat --ansi --columns 80 of cells holding `<br>` line breaks, an image with
# no alt text, and a row of two empty cells
BLANKS = [
    "",
    "─" * 10,
    " id  cell ",
    "─" * 10,
    " r1  a    ",
    "          ",
    " r2       ",
    "     b    ",
    " r3  x    ",
    "          ",
    "     y    ",
    " r4       ",
    "          ",
    " r6  z    ",
    "─" * 10,
    "",
]


def blanks_table() -> Table:
    rows = [(2, 3), (4, 6), (6, 8), (8, 11), (11, 12), (12, 13), (13, 14)]
    return grid(BLANKS, 1, rows, [1, 5], [2, 4])


# mdcat --ansi --columns 40 of glyphs: a heavy check in emoji presentation,
# a ballot box, a cancellation X, two checks, and a cross with a full stop
VS16 = "\N{VARIATION SELECTOR-16}"
GLYPHS = [
    "",
    "─" * 9,
    " a    b  ",
    "─" * 9,
    " ✔" + VS16 + "    ❌ ",
    " ☑    🗙  ",
    " ✓ ✓  ✗. ",
    "─" * 9,
    "",
]


def glyphs_table(plain: list[str], top: int) -> Table:
    rows = [(top + 1, top + 2), (top + 3, top + 4), (top + 4, top + 5),
            (top + 5, top + 6)]
    return grid(plain, top, rows, [1, 6], [3, 2])


# mdcat --ansi --columns 30 of a table in a blockquote between two paragraphs
# of the quote, and one in an ordered list item (indented like code)
QUOTE = [
    "",
    "│ Before the table.",
    "│ ",
    "│ " + "─" * 28,
    "│  Name  Notes                ",
    "│ " + "─" * 28,
    "│  Bob   a fairly long note   ",
    "│        that will wrap       ",
    "│        inside the quote     ",
    "│ " + "─" * 28,
    "│ ",
    "│ After the table.",
    "",
    " 1. First item",
    "",
    "    " + "─" * 10,
    "     k    v   ",
    "    " + "─" * 10,
    "     one  uno ",
    "    " + "─" * 10,
    "",
]


def quote_tables() -> list[Table]:
    return [grid(QUOTE, 3, [(4, 5), (6, 9)], [3, 9], [4, 20]),
            grid(QUOTE, 15, [(16, 17), (18, 19)], [5, 10], [3, 3])]


def test_table_cells_are_read_one_at_a_time_in_reading_order():
    t = basic_table()
    d = Document.from_text("\n".join(BASIC), tables=[t])
    assert d.tables == [t]
    assert [c.kind for c in d.chunks] == [
        "blank", "rule", "cell", "cell", "cell", "rule", "cell", "cell", "cell",
        "cell", "cell", "cell", "rule", "blank"]
    cells = [c for c in d.chunks if c.kind == "cell"]
    assert [c.text for c in cells] == [
        "Name", "Role", "Notes", "Alice", "Engineer", "short", "Bob",
        "Designer with a very long title that wraps around the column",
        "code here"]
    assert [c.cell for c in cells] == [(0, r, k) for r in range(3)
                                       for k in range(3)]
    assert [c.row_end for c in cells] == [False, False, True] * 3
    assert all(c.speakable for c in cells)
    assert not any(c.speakable for c in d.chunks if c.kind == "rule")
    # the header row is read too, and the words follow the cells, not the lines
    assert texts(d) == [
        "Name", "Role", "Notes", "Alice", "Engineer", "short", "Bob",
        "Designer", "with", "a", "very", "long", "title", "that", "wraps",
        "around", "the", "column", "code", "here"]
    # every cell of a row claims the whole row
    assert {c.line_span for c in cells[6:]} == {(5, 8)}
    check_invariants(d)


def test_wrapped_cell_words_never_leak_into_a_neighbour():
    d = Document.from_text("\n".join(BASIC), tables=[basic_table()])
    cell = cells_of(d)
    bob, role, notes = cell[(2, 0)], cell[(2, 1)], cell[(2, 2)]
    assert bob.text == "Bob" and bob.word_texts == ["Bob"]
    assert "code" not in role.text and "here" not in role.text
    assert notes.text == "code here" and notes.word_texts == ["code", "here"]
    # the regions are the cell's column on every line of its row, no gutters
    assert role.regions == [(5, 8, 32), (6, 8, 32), (7, 8, 32)]
    assert notes.regions == [(5, 34, 39), (6, 34, 39), (7, 34, 39)]
    check_invariants(d)


def test_a_wrap_inside_a_word_is_one_word_for_the_tts():
    d = Document.from_text("\n".join(WRAP), tables=[wrap_table()])
    cell = cells_of(d)
    assert cell[(1, 1)].text == (
        "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda")
    assert cell[(2, 1)].text == (
        "well-known state-of-the-art hyphen-separated compound-words here")
    assert cell[(3, 1)].text == (
        "path/to/some/deeply/nested/file/name.txt and more/slashes/here")
    assert cell[(4, 1)].text == (
        "https://example.com/a/very/long/url/that/cannot/fit/in/the/column/at/all")
    assert cell[(5, 1)].text == (
        "Supercalifragilisticexpialidociousandevenlongerwordwithoutbreaks end")
    assert cell[(6, 1)].text == (
        "日本語のテキストはスペースなしで折り返されるべきですかどうか確認します")
    assert cell[(7, 1)].text == (
        "double  spaced   words    here and      more       spaces")
    assert cell[(9, 1)].text == "em—dash—joined—words—and en–dash–joined–words–too"
    assert cell[(10, 1)].text == (
        "a_b_c_d_e_f underscores_joined_words_long_enough_to_wrap maybe")
    assert cell[(11, 1)].text == (
        "comma,separated,values,without,spaces,long,enough,to,wrap")
    # the pieces on screen stay separate words, each at its place in the text
    long = cell[(5, 1)]
    assert long.word_texts == ["Supercalifragilisticexpialidoc",
                               "iousandevenlongerwordwithoutbr", "eaks", "end"]
    assert long.offsets == [0, 30, 60, 65]
    assert [d.words[i].line for i in long.words] == [16, 17, 18, 18]
    assert [cell[(r, 2)].text for r in range(1, 12)] == ["Z"] * 11
    check_invariants(d)


def test_empty_cells_are_kept_but_unspeakable():
    d = Document.from_text("\n".join(BLANKS), tables=[blanks_table()])
    cells = [c for c in d.chunks if c.kind == "cell"]
    assert [c.text for c in cells] == [
        "id", "cell", "r1", "a", "r2", "b", "r3", "x y", "r4", "", "", "", "r6",
        "z"]
    assert [c.speakable for c in cells] == [bool(c.text) for c in cells]
    for c in cells:
        if not c.text:
            assert c.words == [] and c.offsets == [] and c.word_texts == []
            assert c.regions                     # still washable on screen
    # playback steps straight over them
    assert d.next_speakable_chunk(cells[8].idx) == cells[12].idx
    assert d.prev_speakable_chunk(cells[12].idx) == cells[8].idx
    check_invariants(d)


# mdcat --ansi --columns 40 of a table with a sparse last column: an empty
# header cell, an empty cell, a star, a row of empty cells and a tick
SPARSE = [
    "",
    "─" * 22,
    " Task   Owner         ",
    "─" * 22,
    " Build  Alice  urgent ",
    " Test   Bob           ",
    " Ship   Carol  ★      ",
    "                      ",
    " Lint   Eve    ✓      ",
    " Docs   Dave   later  ",
    "─" * 22,
    "",
]


def test_the_beat_between_rows_follows_the_last_cell_that_is_read():
    """row_end is on the last cell of a row that is read, not the last column.

    A last cell with nothing to say is skipped by playback, so a beat on it
    would never be heard and the row would run into the next one.
    """
    table = grid(SPARSE, 1, [(2, 3), (4, 5), (5, 6), (6, 7), (7, 8), (8, 9),
                             (9, 10)], [1, 8, 15], [5, 5, 6])
    d = Document.from_text("\n".join(SPARSE), tables=[table])
    assert d.tables == [table]
    cells = [c for c in d.chunks if c.kind == "cell"]
    assert [(c.text, c.speakable, c.row_end) for c in cells] == [
        ("Task", True, False), ("Owner", True, True), ("", False, False),
        ("Build", True, False), ("Alice", True, False), ("urgent", True, True),
        ("Test", True, False), ("Bob", True, True), ("", False, False),
        ("Ship", True, False), ("Carol", True, True), ("★", False, False),
        ("", False, False), ("", False, False), ("", False, False),
        ("Lint", True, False), ("Eve", True, True), ("✓", False, False),
        ("Docs", True, False), ("Dave", True, False), ("later", True, True)]
    # in playback order, the beat comes exactly where the row changes
    order = []
    at = d.first_speakable_chunk()
    while at is not None:
        order.append(d.chunks[at])
        at = d.next_speakable_chunk(at)
    rows = [c.cell[1] for c in order] + [None]
    assert [c.row_end for c in order] == [
        rows[k] != rows[k + 1] for k in range(len(order))]
    check_invariants(d)


def test_table_lookups_on_a_wrapped_row():
    d = Document.from_text("\n".join(BASIC), tables=[basic_table()])
    cell = {rc: c.idx for rc, c in cells_of(d).items()}
    idx = {w.text: w.idx for w in d.words}

    # word -> chunk and slot follow the cell, not the line
    assert d.chunk_of_word(idx["code"]) == cell[(2, 2)]
    assert d.slot_of_word(idx["code"]) == 0
    assert d.chunk_of_word(idx["long"]) == cell[(2, 1)]
    assert d.slot_of_word(idx["long"]) == 4
    assert idx["code"] == idx["column"] + 1
    # word_at still finds every word by its column
    for w in d.words:
        for col in range(w.start, w.end):
            assert d.word_at(w.line, col) == w.idx
    assert d.word_at(5, 7) is None
    assert d.words_of_line(5) == [idx["Bob"], idx["Designer"], idx["with"],
                                  idx["a"], idx["very"], idx["code"]]

    # chunk_at_line: the first chunk covering the line, although every cell of
    # the row covers it
    assert d.chunk_at_line(3) == cell[(0, 2)] + 1        # the header rule
    assert d.chunk_at_line(5) == d.chunk_at_line(7) == cell[(2, 0)]
    assert d.chunk_at_line(8) == cell[(2, 2)] + 1        # the bottom rule

    # cell_at: the cell's column on that line; gutters and rules are no cell
    assert d.cell_at(6, 9) == cell[(2, 1)]
    assert d.cell_at(7, 38) == cell[(2, 2)]              # its blank last line
    assert d.cell_at(2, 1) == cell[(0, 0)]
    assert d.cell_at(6, 7) is None and d.cell_at(6, 0) is None
    assert d.cell_at(3, 10) is None and d.cell_at(0, 0) is None

    # nearest_word stays inside one cell, on any line of the row
    assert d.nearest_word(5, 12) == idx["Designer"]
    assert d.nearest_word(7, 3) == idx["Bob"]            # a blank line of Bob
    assert d.nearest_word(7, 37) == idx["here"]          # nearest line wins
    assert d.nearest_word(6, 31) == idx["wraps"]         # then nearest column
    assert d.nearest_word(6, 7) == idx["long"]           # gutter: nearest cell
    assert d.nearest_word(6, 33) == idx["here"]
    assert d.nearest_word(6, 0) == idx["Bob"]            # the prefix
    assert d.nearest_word(3, 5) is None                  # a rule
    assert d.nearest_word(1, 99) is None
    check_invariants(d)

    # an empty cell has no word to offer, even with a neighbour one column away
    b = Document.from_text("\n".join(BLANKS), tables=[blanks_table()])
    assert b.nearest_word(12, 6) is None
    assert b.nearest_word(12, 0) is None
    assert b.words[b.nearest_word(7, 1)].text == "r2"


def test_nearest_word_in_a_cell_compares_terminal_columns():
    # mdcat --ansi --columns 20: the wide 本語 moves line 5's chars two to the
    # left of the display columns they share with line 4
    plain = [
        "",
        "─" * 20,
        " k      v           ",
        "─" * 20,
        " ab 日  one two     ",
        " 本語               ",
        " x      three four  ",
        "        five six    ",
        "        seven       ",
        "─" * 20,
        "",
    ]
    t = grid(plain, 1, [(2, 3), (4, 6), (6, 9)], [1, 8], [5, 11])
    d = Document.from_text("\n".join(plain), tables=[t])
    assert t.cells[3].spans == [(4, 7, 18), (5, 6, 17)]
    # char 10 of line 5 is display column 12, right under "two" (char 10 of
    # line 4 is the space after "one")
    assert d.words[d.nearest_word(5, 10)].text == "two"
    assert d.words[d.nearest_word(5, 6)].text == "one"
    check_invariants(d)


def test_a_header_only_table_ends_in_two_rules():
    # mdcat --ansi --columns 40 of "| only | header |" and its delimiter row
    plain = ["", "Just a header:", "", "─" * 14, " only  header ", "─" * 14,
             "─" * 14, "", "Done.", ""]
    t = grid(plain, 3, [(4, 5)], [1, 7], [4, 6])
    assert (t.line_start, t.line_end) == (3, 7)
    d = Document.from_text("\n".join(plain), tables=[t])
    assert d.tables == [t]
    assert [c.kind for c in d.chunks] == [
        "blank", "para", "blank", "rule", "cell", "cell", "rule", "rule",
        "blank", "para", "blank"]
    assert [c.row_end for c in d.chunks if c.kind == "cell"] == [False, True]
    check_invariants(d)


def test_two_tables_and_the_prose_around_them_keep_reading_order():
    plain = (["Intro one."] + BASIC[1:9] + ["", "Between the tables."]
             + GLYPHS[1:8] + ["Outro."])
    first = grid(plain, 1, [(2, 3), (4, 5), (5, 8)], [1, 8, 34], [5, 24, 5])
    second = glyphs_table(plain, 11)
    d = Document.from_text("\n".join(plain), tables=[second, first])
    assert d.tables == [first, second]          # document order
    assert texts(d) == (
        ["Intro", "one"] + texts(Document.from_text("\n".join(BASIC),
                                                    tables=[basic_table()]))
        + ["Between", "the", "tables", "a", "b", "Outro"])
    assert [c.kind for c in d.chunks if c.kind != "cell"] == [
        "para", "rule", "rule", "rule", "blank", "para", "rule", "rule", "rule",
        "para"]
    assert {c.cell[0] for c in d.chunks if c.kind == "cell"} == {0, 1}
    check_invariants(d)


def test_tables_in_a_quote_or_a_list_are_cut_out_of_their_line_group():
    before = Document.from_text("\n".join(QUOTE))
    # unmapped, the quote is one paragraph and the list's table is code
    assert any("Before" in c.text and "After" in c.text for c in before.chunks)
    assert any(c.kind == "code" and "uno" in c.text for c in before.chunks)

    d = Document.from_text("\n".join(QUOTE), tables=quote_tables())
    assert len(d.tables) == 2
    assert [(c.kind, " ".join(c.word_texts)) for c in d.chunks
            if c.speakable] == [
        ("para", "Before the table"),
        ("cell", "Name"), ("cell", "Notes"), ("cell", "Bob"),
        ("cell", "a fairly long note that will wrap inside the quote"),
        ("para", "After the table"),
        ("para", "1 First item"),
        ("cell", "k"), ("cell", "v"), ("cell", "one"), ("cell", "uno")]
    assert all(c.line_end <= 3 for c in d.chunks
               if c.kind == "para" and "Before" in c.text)
    check_invariants(d)


def test_a_table_that_does_not_fit_is_read_as_lines():
    text = "\n".join(BASIC)
    lines_only = shape(Document.from_text(text))
    good = basic_table()

    def broken(change) -> Table:
        t = copy.deepcopy(good)
        change(t)
        return t

    bad = [
        broken(lambda t: setattr(t, "line_end", 99)),         # past the end
        broken(lambda t: setattr(t, "line_start", 2)),        # no top rule
        broken(lambda t: t.rows.__setitem__(2, (6, 8))),      # a line skipped
        broken(lambda t: t.rows.__setitem__(1, "x")),         # not a range
        broken(lambda t: setattr(t, "ncols", 2)),             # cell count
        broken(lambda t: t.cells.pop()),                      # a missing cell
        broken(lambda t: t.cells.reverse()),                  # out of order
        # the Designer cell's span on line 5: past the line, on another row's
        # line, too narrow for its words; the notes cell's: over the Designer
        # column, and a second span on line 6
        broken(lambda t: t.cells[7].spans.__setitem__(0, (5, 8, 99))),
        broken(lambda t: t.cells[7].spans.__setitem__(0, (4, 8, 32))),
        broken(lambda t: t.cells[7].spans.__setitem__(0, (5, 8, 14))),
        broken(lambda t: t.cells[8].spans.__setitem__(0, (5, 30, 39))),
        broken(lambda t: t.cells[8].spans.append((6, 34, 39))),
        broken(lambda t: setattr(t.cells[0], "row", 1)),
    ]
    for t in bad:
        d = Document.from_text(text, tables=[t])
        assert d.tables == []
        assert shape(d) == lines_only
        check_invariants(d)

    # a word on a rule line: the map does not match the lines
    ruled = list(BASIC)
    ruled[3] = "─" * 17 + " oops " + "─" * 17
    d = Document.from_text("\n".join(ruled), tables=[basic_table()])
    assert d.tables == [] and "oops" in texts(d)
    check_invariants(d)

    # overlapping tables: the first one wins
    d = Document.from_text(text, tables=[good, copy.deepcopy(good)])
    assert len(d.tables) == 1
    check_invariants(d)


def test_no_tables_changes_nothing():
    for text in (MDCAT_PLAIN, "\n".join(BASIC), "\n".join(QUOTE)):
        assert shape(Document.from_text(text, tables=())) == shape(
            Document.from_text(text))
    d = Document.from_text("\n".join(BASIC))
    assert d.tables == []
    assert [c.kind for c in d.chunks] == ["blank", "para", "blank"]
    assert all(c.cell is None and c.regions == [] and not c.row_end
               for c in d.chunks)


def test_build_chunks_takes_tables_only_with_their_cells():
    """Tables are fitted by the Document, never by build_chunks on its own."""
    t = basic_table()
    d = Document.from_text("\n".join(BASIC), tables=[t])
    cells = [[c.words for c in d.chunks if c.kind == "cell"]]
    chunks = build_chunks(d.plain, d.words, tables=d.tables, cells=cells)
    assert [(c.kind, c.text, c.words, c.cell, c.regions) for c in chunks] == [
        (c.kind, c.text, c.words, c.cell, c.regions) for c in d.chunks]
    for bad in ({"tables": [t]}, {"cells": cells},
                {"tables": [t, t], "cells": cells}):
        try:
            build_chunks(d.plain, extract_words(d.plain), **bad)
        except ValueError:
            continue
        raise AssertionError(f"build_chunks accepted {sorted(bad)}")


def lay_out(rows_text: list[list[str]], widths: list[int], top: int,
            indent: str = "") -> tuple[list[str], Table]:
    """Plain lines and the Table for `rows_text`, laid out like mdcat.

    Left aligned, a space at either edge, two space gutters; a cell wraps at
    spaces and hard splits a word longer than its column (join "").
    """
    ncols = len(widths)
    starts = [len(indent) + 1 + sum(widths[:c]) + 2 * c for c in range(ncols)]
    rule = indent + "─" * (sum(widths) + 2 * ncols)

    def wrap(text: str, width: int) -> list[list[str]]:
        out: list[list[str]] = []           # [piece, join before it]
        for word in text.split():
            if out and len(out[-1][0]) + 1 + len(word) <= width:
                out[-1][0] += " " + word
                continue
            join = " "
            while len(word) > width:
                out.append([word[:width], join])
                word, join = word[width:], ""
            out.append([word, join])
        return out

    lines = [rule]
    rows: list[tuple[int, int]] = []
    cells: list[TableCell] = []
    for r, row in enumerate(rows_text):
        wrapped = [wrap(text, w) for text, w in zip(row, widths)]
        height = max([1] + [len(p) for p in wrapped])
        first = top + len(lines)
        for k in range(height):
            parts = [(p[k][0] if k < len(p) else "").ljust(w)
                     for p, w in zip(wrapped, widths)]
            lines.append(indent + " " + "  ".join(parts) + " ")
        rows.append((first, first + height))
        for c, p in enumerate(wrapped):
            spans = [(first + k, starts[c], starts[c] + widths[c])
                     for k in range(height)]
            joins = [p[k][1] if k < len(p) else " " for k in range(height)]
            cells.append(TableCell(r, c, spans, joins))
        if r == 0:
            lines.append(rule)
    lines.append(rule)
    return lines, Table(top, top + len(lines), ncols, rows, cells)


def test_fuzz_random_tables_keep_every_invariant():
    import random
    rng = random.Random(20260911)
    # "```" at the start of a cell looks like a fence to the line grouping
    vocab = ["alpha", "beta", "x", "12.5%", "well-known", "it's", "(see)",
             "Supercalifragilistic", "https://example.com/a/b", "--", "e.g.",
             "Dr.", "✓", "✗", "```"]
    prose = ["Some prose here.", "", "More words. And more.",
             "    indented code", "│ quoted line", "• bullet"]
    for _ in range(150):
        lines: list[str] = []
        tables: list[Table] = []
        sources: list[list[list[str]]] = []
        for _block in range(rng.randrange(1, 4)):
            lines.extend(rng.choice(prose) for _ in range(rng.randrange(0, 3)))
            ncols = rng.randrange(1, 5)
            rows_text = [
                [" ".join(rng.choice(vocab)
                          for _ in range(rng.choice([0, 0, 1, 1, 2, 6])))
                 for _ in range(ncols)]
                for _ in range(rng.randrange(1, 5))]
            widths = [rng.randrange(2, 13) for _ in range(ncols)]
            block, table = lay_out(rows_text, widths, len(lines),
                                   rng.choice(["", "  ", "│ "]))
            lines.extend(block)
            tables.append(table)
            sources.append(rows_text)
        lines.extend(rng.choice(prose) for _ in range(rng.randrange(0, 3)))

        shuffled = rng.sample(tables, len(tables))
        d = Document.from_text("\n".join(lines), tables=shuffled,
                               max_chars=rng.choice([20, 380]))
        assert d.tables == tables
        check_invariants(d)
        for c in d.chunks:
            if c.kind != "cell":
                continue
            t, r, k = c.cell
            tokens = sources[t][r][k].split()
            assert c.text == " ".join(tokens)
            for line, s, e in c.regions:
                for col in (s, e - 1):
                    assert d.cell_at(line, col) == c.idx
                    hit = d.nearest_word(line, col)
                    assert (hit is None) == (not c.words)
                    assert hit is None or d.chunk_of_word(hit) == c.idx


def test_a_cell_that_looks_like_a_fence_leaves_the_text_after_the_table_alone():
    lines, t = lay_out([["lang", "sample"], ["```", "opens a code block"]],
                       [6, 10], 0)
    assert lines[3].startswith(" ```")
    plain = lines + ["", "One sentence. Two sentences.", "Three. Four. Five."]
    d = Document.from_text("\n".join(plain), tables=[t])
    assert d.tables == [t]
    after = [c for c in d.chunks if c.line_start >= t.line_end and c.speakable]
    assert [c.kind for c in after] == ["para", "para"]
    check_invariants(d)


# ---------------------------------------------------------------------------
# ansi integration
# ---------------------------------------------------------------------------


def test_document_from_ansi_runs():
    data = ("\x1b[1m\x1b[97mTitle\x1b[0m\n\n"
            "Some \x1b[3mitalic\x1b[0m text with a "
            "\x1b]8;;https://example.com\x1b\\link\x1b]8;;\x1b\\.\n\n"
            "• bullet one\n• bullet two\n")
    d = Document(ansi.parse(data))
    assert d.plain[0] == "Title"
    assert d.plain[2].startswith("Some italic text")
    assert "link" in d.plain[2]
    assert "\x1b" not in "".join(d.plain)
    assert texts(d)[:1] == ["Title"]
    assert "link" in texts(d)
    check_invariants(d)


def test_document_survives_tabs_and_crlf():
    d = doc("a\tb\r\nc\td\r\n")
    assert d.plain[0] == "a   b"
    assert texts(d) == ["a", "b", "c", "d"]
    check_invariants(d)


# ---------------------------------------------------------------------------
# pathological input
# ---------------------------------------------------------------------------


def test_empty_string():
    d = doc("")
    assert d.plain == [""]
    assert d.words == []
    assert len(d.chunks) == 1
    assert d.chunks[0].speakable is False
    assert d.speakable_chunks == []
    assert d.first_speakable_chunk() is None
    assert d.word_at(0, 0) is None
    assert d.chunk_at_line(0) == 0
    check_invariants(d)


def test_no_lines_at_all():
    d = Document()
    assert d.lines == [] and d.plain == [] and d.words == [] and d.chunks == []
    assert d.word_at(0, 0) is None
    assert d.chunk_at_line(0) is None
    assert d.first_speakable_chunk() is None
    assert d.next_speakable_chunk(0) is None
    check_invariants(d)


def test_whitespace_only():
    d = doc("   \n\t\n \n   ")
    assert d.words == []
    assert all(not c.speakable for c in d.chunks)
    assert len(d.chunks) == 1
    check_invariants(d)


def test_five_thousand_char_paragraph_without_sentence_punctuation():
    body = " ".join(f"word{i}" for i in range(830))
    assert len(body) > 5000
    d = doc(body)
    speak = [c for c in d.chunks if c.speakable]
    assert len(speak) > 10
    for c in speak:
        assert len(c.text) <= DEFAULT_MAX_CHARS
    assert len(d.words) == 830
    check_invariants(d)


def test_line_of_two_hundred_bullets():
    d = doc("• " * 200)
    assert d.words == []
    assert len(d.chunks) == 1 and not d.chunks[0].speakable
    check_invariants(d)


def test_two_hundred_bullet_lines():
    d = doc("\n".join(f"• bullet item number {i}" for i in range(200)))
    assert len(d.plain) == 200
    assert len(d.words) == 200 * 4
    speak = [c for c in d.chunks if c.speakable]
    assert len(speak) > 1
    for c in speak:
        assert len(c.text) <= DEFAULT_MAX_CHARS
    check_invariants(d)


def test_single_unsplittable_token_stays_whole():
    d = doc("x" * 900)
    assert len(d.chunks) == 1
    assert d.chunks[0].text == "x" * 900          # never split mid-word
    assert d.words[0].text == "x" * 900
    check_invariants(d)


def test_many_blank_lines():
    d = doc("A.\n" + "\n" * 50 + "B.")
    assert len([c for c in d.chunks if c.speakable]) == 2
    check_invariants(d)


def test_document_with_only_punctuation():
    d = doc("...\n---\n???\n!!!")
    assert d.words == []
    assert all(not c.speakable for c in d.chunks)
    check_invariants(d)


def test_defaults_match_the_contract():
    assert DEFAULT_MAX_SENTENCES == 4
    assert DEFAULT_MAX_CHARS == 380
    d = doc("hi")
    assert d.max_sentences == 4 and d.max_chars == 380


def test_large_mixed_document_invariants():
    parts = []
    for i in range(40):
        parts.append(f"Paragraph {i} opens here. It has a second sentence, "
                     f"e.g. one with Dr. Ada and Fig. {i}. And a third one "
                     f"that runs on for a while so the 380-char limit bites.")
        parts.append("─" * 30)
        parts.append(f"    code_line_{i} = value * {i}\n    more_{i} = 1")
        parts.append(f"• item {i}\n• item {i + 1}")
    d = doc("\n\n".join(parts))
    check_invariants(d)
    assert len(d.chunks) > 100
    assert any(c.kind == "code" for c in d.chunks)
    assert any(not c.speakable for c in d.chunks)


def test_fuzz_random_input_keeps_every_invariant():
    import random
    rng = random.Random(20260910)
    alphabet = list("abcXYZ 0123.,!?'\"-_/:$%()[]#*`~|\u2022\u2500\u2502"
                    "\u00e9\u2019\t\n")
    for _ in range(200):
        n = rng.randrange(0, 400)
        text = "".join(rng.choice(alphabet) for _ in range(n))
        d = doc(text, max_sentences=rng.choice([1, 2, 4]),
                max_chars=rng.choice([20, 80, 380]))
        check_invariants(d)
        for w in d.words:
            assert d.word_at(w.line, w.start) == w.idx
            assert d.chunk_at_line(w.line) is not None


def test_giant_token_inside_a_long_paragraph_loses_nothing():
    body = ("alpha beta gamma " * 20) + ("Z" * 900) + (" delta epsilon" * 20)
    d = doc(body)
    check_invariants(d)
    assert any(w.text == "Z" * 900 for w in d.words)
    assert sum(len(c.words) for c in d.chunks) == len(d.words)
    joined = " ".join(c.text for c in d.chunks if c.speakable)
    for token in ("alpha", "epsilon", "Z" * 900):
        assert token in joined


# ---------------------------------------------------------------------------
# standalone runner (pytest is not installed in this venv)
# ---------------------------------------------------------------------------


def _main() -> int:
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failures = []
    for name, fn in tests:
        try:
            fn()
        except Exception as exc:                       # noqa: BLE001
            import traceback
            failures.append((name, traceback.format_exc()))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - len(failures)} passed, {len(failures)} failed")
    for name, tb in failures:
        print("\n" + "=" * 70 + f"\n{name}\n{tb}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
