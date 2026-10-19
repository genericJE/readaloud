"""readaloud.document's words, sentences and chunk spans, now in Rust, against
the Python they replaced (tests/reference/words.py), over random text.

The pieces the text is made of are the ones each rule turns on: URLs and
addresses with punctuation after them, initialisms and abbreviations in any
case, money, thousands and percentages, the connectors, letters and digits of
other scripts, combining accents, the closing quotes after a full stop, list
markers, fences and indentation.
"""

from __future__ import annotations

import random

from readaloud import _core, document
from reference import words as reference

SURROGATES = range(0xD800, 0xE000)

PIECES = [
    "a", "Z", "word", "Words", "it's", "donʼt", "well-known", "mlx_audio",
    "x.y", "3:45pm", "TCP/IP", "AT&T", "12.5", "$4.50", "€3", "$", "1,000",
    "1,0000", "12,34", "99%", "a%", "https://ex.org/a?b=1", "http://", "git://x",
    "ftp://h/p)", "me@ex.co", "a.b@c.d.e", "x@y", "e.g.", "i.e", "U.S.A",
    "U.S.", "a.m.", "Dr.", "DR.", "dr.x", "etc.", "Fig.", "St", "no.", "vs.",
    "J.", "3.", "1234.", "Résumé", "é", "́", "日本", "٣", "²", "ß",
    "Σ", "ʼ", "_", "-", "--", ".", "..", "...", "!", "?!", "…", "‽", ",", ";",
    ":", "—", "–", "'", '"', "”", "’", "»", ")", "]", "}", "(", "[", "<", ">",
    "|", "@", "#", "%", "&", "/", " ", " ", " ", "  ", "\t", " ", "\x1c",
    "\n",
]
LINE_STARTS = ["", "", "", "    ", "\t", "  ", "```", "~~~", "   ```py",
               "    ```", "- ", "* ", "1. ", "2) ", "a. ", "> ", "• ", "-x"]


def random_line(rng: random.Random, n: int = 14) -> str:
    return "".join(rng.choice(PIECES) for _ in range(rng.randint(0, n)))


def random_doc(rng: random.Random) -> list[str]:
    lines = []
    for _ in range(rng.randint(1, 14)):
        if rng.random() < 0.2:
            lines.append(rng.choice(["", " ", "\t"]))
        else:
            lines.append(rng.choice(LINE_STARTS)
                         + random_line(rng).replace("\n", " "))
    return lines


def test_the_abbreviations_are_the_same():
    assert document.ABBREVIATIONS == reference.ABBREVIATIONS


def test_every_character_splits_as_before():
    for cp in range(0x30000):
        if cp in SURROGATES:
            continue
        c = chr(cp)
        for text in (c, "a" + c + "b", "1" + c + "2", c + ". Next"):
            assert (document.line_word_spans(text)
                    == reference.line_word_spans(text)), hex(cp)
        text = "Done" + c + " Then " + c + "x. " + c
        assert (document.sentence_spans(text)
                == reference.sentence_spans(text)), hex(cp)


def test_random_lines_split_into_the_same_words():
    rng = random.Random(1)
    for _ in range(20000):
        text = random_line(rng, 20)
        assert (document.line_word_spans(text)
                == reference.line_word_spans(text)), text


def test_random_text_splits_into_the_same_sentences():
    rng = random.Random(2)
    for _ in range(10000):
        text = random_line(rng, 30)
        lo = rng.randint(0, len(text))
        hi = rng.choice([None, rng.randint(lo, len(text))])
        assert (document.sentence_spans(text, lo, hi)
                == reference.sentence_spans(text, lo, hi)), (text, lo, hi)


def test_random_documents_group_into_the_same_lines():
    rng = random.Random(3)
    for _ in range(5000):
        plain = random_doc(rng)
        lo = rng.randint(0, len(plain))
        hi = rng.choice([None, rng.randint(lo, len(plain))])
        assert (document._line_groups(plain, lo, hi)
                == reference._line_groups(plain, lo, hi)), (plain, lo, hi)


def test_long_sentences_are_cut_in_the_same_places():
    rng = random.Random(4)
    for _ in range(5000):
        text = random_line(rng, 60)
        lo = rng.randint(0, len(text))
        hi = rng.randint(lo, len(text))
        max_chars = rng.randint(1, 30)
        assert (_core.split_long(text, lo, hi, max_chars)
                == reference._split_long(text, lo, hi, max_chars)), (
            text, lo, hi, max_chars)


def test_paragraphs_are_cut_into_the_same_chunks():
    rng = random.Random(5)
    for _ in range(5000):
        text = random_line(rng, 60)
        lo = rng.randint(0, len(text))
        hi = rng.randint(lo, len(text))
        args = (lo, hi, rng.randint(1, 4), rng.randint(1, 80))
        assert (_core.paragraph_spans(text, *args)
                == reference._paragraph_spans(text, *args)), (text, args)


def test_documents_are_cut_into_the_same_chunk_spans():
    rng = random.Random(6)
    for _ in range(3000):
        plain = random_doc(rng)
        cuts = sorted(set(rng.sample(range(len(plain) + 1),
                                     min(len(plain) + 1, rng.randint(2, 5)))))
        pieces = [(a, b) for a, b in zip(cuts, cuts[1:]) if a < b]
        flat, offsets = reference._flatten(plain)
        word_at = [offsets[i] + s for i, line in enumerate(plain)
                   for s, _e in reference.line_word_spans(line)]
        args = (rng.randint(1, 4), rng.randint(1, 120))
        assert (_core.prose_spans(plain, pieces, word_at, *args)
                == reference.prose_spans(plain, pieces, word_at, *args)), (
            plain, pieces, args)
