"""Tests for readaloud.document.

Plain ``assert`` statements only -- no pytest fixtures, parametrisation or
imports -- so the file runs under ``pytest`` *and* directly with
``uv run python tests/test_document.py`` (pytest is not currently a dependency
of this project).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from readaloud import ansi                                       # noqa: E402
from readaloud.document import (                                 # noqa: E402
    ABBREVIATIONS,
    DEFAULT_MAX_CHARS,
    DEFAULT_MAX_SENTENCES,
    Chunk,
    Document,
    Word,
    build_chunks,
    extract_words,
    sentence_spans,
    split_words,
    line_word_spans,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def doc(text: str, **kw) -> Document:
    return Document.from_text(text, **kw)


def check_invariants(d: Document) -> None:
    """Every structural guarantee document.py makes, checked at once."""
    flat = "\n".join(d.plain)

    # -- words -------------------------------------------------------------
    prev = (-1, -1)
    for i, w in enumerate(d.words):
        assert w.idx == i, f"word {i} carries idx {w.idx}"
        assert 0 <= w.line < len(d.plain), f"word {i} line {w.line} out of range"
        assert 0 <= w.start < w.end <= len(d.plain[w.line])
        assert d.plain[w.line][w.start:w.end] == w.text, (
            f"word {i} {w.text!r} does not round-trip against its line")
        assert w.text.strip() == w.text, f"word {i} {w.text!r} has edge space"
        assert (w.line, w.start) > prev, "words are not in reading order"
        prev = (w.line, w.start)

    # -- chunk text / offsets ---------------------------------------------
    line_off = []
    pos = 0
    for line in d.plain:
        line_off.append(pos)
        pos += len(line) + 1

    seen: list[int] = []
    for c in d.chunks:
        assert c.idx == d.chunks.index(c)
        assert len(c.offsets) == len(c.words) == len(c.word_texts)
        assert c.speakable == bool(c.words)
        assert 0 <= c.line_start < c.line_end <= len(d.plain)

        # chunk.text must be a VERBATIM slice of the flattened document,
        # positioned on the lines the chunk claims.
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

    # -- line coverage -----------------------------------------------------
    if d.chunks:
        assert d.chunks[0].line_start == 0
        assert d.chunks[-1].line_end == len(d.plain)
        covered: set[int] = set()
        for a, b in zip(d.chunks, d.chunks[1:]):
            # consecutive chunks are adjacent, or share the one line a
            # sentence boundary fell inside
            assert b.line_start in (a.line_end - 1, a.line_end), (
                f"gap between chunk {a.idx} and {b.idx}")
        for c in d.chunks:
            covered.update(range(c.line_start, c.line_end))
        assert covered == set(range(len(d.plain))), "chunks do not cover all lines"


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
