"""readaloud.width, measured in Rust, against the Python it replaced."""

from __future__ import annotations

import random

from readaloud import _core, width
from reference import width as reference

SURROGATES = range(0xD800, 0xE000)


def test_every_character_measures_as_before():
    for cp in range(0x30000):
        if cp in SURROGATES:
            continue
        ch = chr(cp)
        assert width.char_width(ch) == reference.char_width(ch), hex(cp)
        assert _core.char_width(ch) == reference.char_width(ch), hex(cp)


def test_random_text_measures_as_before():
    rng = random.Random(14)
    pool = "aZ1 _.\t漢字é́​✓🎉ｱ한\x1b"
    for _ in range(2000):
        text = "".join(rng.choice(pool) for _ in range(rng.randint(0, 30)))
        assert width.text_width(text) == reference.text_width(text), text
        assert width.cell_offsets(text) == reference.cell_offsets(text), text


def test_a_lone_surrogate_takes_one_cell_like_any_unknown():
    text = "a\udcffb"
    assert width.text_width(text) == 3
    assert width.cell_offsets(text) == [0, 1, 2, 3]
    assert width.char_width("") == 0
