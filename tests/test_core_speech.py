"""readaloud.speech's word timings, now in Rust, against the Python they
replaced (tests/reference/speech.py), over random texts and token streams.

The tokens are what misaki does to the text: words in order, some with
whitespace glued on, some missing a character, some that are not in the text
at all, with timestamps that are missing, negative, reversed, infinite or NaN.
Results are compared by `repr`, so every float must be the same bit for bit,
signed zeros and NaNs included.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from readaloud import speech
from readaloud.speech import Spoken, Timed
from reference import speech as reference

PIECES = [
    "word", "Words", "it's", "print('hello')", "x.y", "3:45", "$", "$4.50", "é",
    "é", "日本", "🎉", "a", "I", "--", "...", ",", ".", "!", '"', "”", "’", "»",
    "*", "_", "`", "~", ")", "(", "[", "]", "a.b@c.d", "https://ex.org/a",
    " ", " ", " ", "  ", "\t", "\n", "\n\n", " ", "　",
]
TIMES = [0.0, 0.0, 0.1, 0.25, 0.5, 1.0, 1.7, 3.0, -0.0, -0.5, 1e-9, 1e9,
         float("inf"), float("-inf"), float("nan")]


def random_text(rng: random.Random) -> str:
    return "".join(rng.choice(PIECES) for _ in range(rng.randint(0, 40)))


def a_time(rng: random.Random) -> float | None:
    if rng.random() < 0.15:
        return None
    if rng.random() < 0.15:
        return rng.choice(TIMES)
    return round(rng.uniform(-0.2, 4.0), rng.randint(0, 6))


def word_starts(text: str) -> list[int]:
    return [i for i, c in enumerate(text)
            if not c.isspace() and (i == 0 or text[i - 1].isspace())]


def random_tokens(rng: random.Random, text: str) -> list[SimpleNamespace]:
    """misaki's tokens for `text`, mangled the ways misaki mangles them."""
    words = text.split()
    out = []
    for w in words:
        roll = rng.random()
        if roll < 0.1 and len(w) > 2:
            k = rng.randrange(1, len(w) - 1)
            w = w[:k] + w[k + 1:]  # a dropped source character
        elif roll < 0.2:
            w = rng.choice([" ", "\n", "  ", "\t"]) + w  # absorbed whitespace
        elif roll < 0.25:
            w = rng.choice(["zzz", "", " ", "\n", "$", w + w])  # not in the text
        elif roll < 0.3 and len(w) > 1:
            k = rng.randrange(1, len(w))
            out.append(SimpleNamespace(text=w[:k], start_ts=a_time(rng), end_ts=a_time(rng)))
            w = w[k:]  # one word, two tokens
        if rng.random() < 0.05:
            out.append(SimpleNamespace(text=None, start_ts=a_time(rng), end_ts=None))
        out.append(SimpleNamespace(text=w, start_ts=a_time(rng), end_ts=a_time(rng)))
    if rng.random() < 0.2 and len(out) > 1 and len(words) > 1:
        # several words in one token
        k = rng.randrange(len(words) - 1)
        out[min(k, len(out) - 1)] = SimpleNamespace(
            text=" ".join(words[k:k + 2]), start_ts=a_time(rng), end_ts=a_time(rng))
    return out


def random_results(rng: random.Random, text: str) -> list[tuple[list, float]]:
    tokens = random_tokens(rng, text)
    cuts = sorted(rng.randint(0, len(tokens)) for _ in range(rng.randint(0, 3)))
    parts, last = [], 0
    for c in cuts + [len(tokens)]:
        parts.append(tokens[last:c])
        last = c
    results = []
    for part in parts:
        duration = rng.choice(TIMES) if rng.random() < 0.2 else rng.uniform(0.0, 5.0)
        results.append((part if part or rng.random() < 0.5 else None, duration))
    return results


def random_offsets(rng: random.Random, text: str) -> list:
    starts = word_starts(text)
    if rng.random() < 0.8:
        return starts
    out: list = list(starts)
    for _ in range(rng.randint(0, 4)):
        junk = rng.choice([None, "x", "3", 2.7, -5, len(text) + 9, 10 ** 30,
                           rng.randint(0, len(text) + 1)])
        out.insert(rng.randint(0, len(out)), junk)
    return out


def random_word_texts(rng: random.Random, text: str, n: int) -> list | None:
    if rng.random() < 0.4:
        return None
    words: list = text.split()
    words = words[: rng.randint(0, n + 1)]
    return [w if rng.random() < 0.8 else rng.choice([None, "", w[:1], "zz"])
            for w in words]


def random_spans(rng: random.Random, text: str) -> list[tuple[int, int]]:
    if rng.random() < 0.85:
        return reference.word_spans(text, random_offsets(rng, text))
    n = len(text)
    return [(rng.randint(-2, n + 2), rng.randint(-2, n + 2))
            for _ in range(rng.randint(0, 8))]


def outcome(fn, *args):
    try:
        return repr(fn(*args))
    except Exception as exc:  # noqa: BLE001 - the error is part of the answer
        return type(exc).__name__


def test_locate_finds_what_it_found_before():
    rng = random.Random(51)
    for _ in range(3000):
        text = random_text(rng)
        cursor = rng.randint(0, len(text))
        tokens = random_tokens(rng, text[cursor:]) or [SimpleNamespace(text="x")]
        needle = rng.choice(tokens).text or rng.choice(["", " ", "\n"])
        assert (speech._locate(text, needle, cursor)
                == reference._locate(text, needle, cursor)), (text, needle, cursor)


def test_long_text_is_searched_only_so_far_ahead():
    text = "a" * 500 + " b'c"
    for needle, cursor in (("bc", 0), ("bc", 200), ("b", 498), ("ab", 0)):
        assert (speech._locate(text, needle, cursor)
                == reference._locate(text, needle, cursor))


def test_word_spans_end_where_they_ended_before():
    rng = random.Random(52)
    for _ in range(3000):
        text = random_text(rng)
        offsets = random_offsets(rng, text)
        texts = random_word_texts(rng, text, len(offsets))
        assert (outcome(speech.word_spans, text, offsets, texts)
                == outcome(reference.word_spans, text, offsets, texts)), (text, offsets, texts)


def test_align_words_times_every_slot_as_before():
    rng = random.Random(53)
    for _ in range(3000):
        text = random_text(rng)
        spans = random_spans(rng, text)
        results = random_results(rng, text)
        assert (outcome(speech.align_words, text, spans, results)
                == outcome(reference.align_words, text, spans, results)), (text, spans, results)


def test_even_timings_spread_as_before():
    rng = random.Random(54)
    for _ in range(2000):
        spans = [(rng.randint(-3, 40), rng.randint(-3, 40)) for _ in range(rng.randint(0, 8))]
        duration = rng.choice(TIMES) if rng.random() < 0.3 else rng.uniform(0.0, 30.0)
        nslots = rng.choice([None, None, 0, -1, len(spans), len(spans) + 2, 1])
        assert (outcome(speech.even_timings, spans, duration, nslots)
                == outcome(reference.even_timings, spans, duration, nslots)), (spans, duration, nslots)


def test_sanitize_clamps_and_refuses_as_before():
    rng = random.Random(55)
    for _ in range(3000):
        n = rng.randint(0, 6)
        timings = [Timed(i if rng.random() < 0.97 else i + 1,
                         a_time(rng) or 0.0, a_time(rng) or 0.0) for i in range(n)]
        nslots = n if rng.random() < 0.9 else n + 1
        duration = rng.choice(TIMES) if rng.random() < 0.3 else rng.uniform(0.0, 5.0)
        assert (outcome(speech._sanitize, timings, nslots, duration)
                == outcome(reference._sanitize, timings, nslots, duration)), (timings, nslots, duration)


def test_build_timings_as_before():
    rng = random.Random(56)
    for _ in range(2000):
        text = random_text(rng)
        spans = random_spans(rng, text)
        results = random_results(rng, text)
        duration = rng.choice(TIMES) if rng.random() < 0.2 else rng.uniform(0.0, 8.0)
        nslots = len(spans) if rng.random() < 0.9 else rng.randint(-1, 9)
        assert (outcome(speech.build_timings, text, spans, results, duration, nslots)
                == outcome(reference.build_timings, text, spans, results, duration, nslots))


@pytest.mark.parametrize("seed", range(5))
def test_word_at_finds_the_word_the_scan_found(seed):
    rng = random.Random(570 + seed)
    for _ in range(500):
        starts = sorted(rng.choice([0.0, 0.5, 1.0, rng.uniform(0.0, 3.0)])
                        for _ in range(rng.randint(0, 10)))
        timings = [Timed(i, s, s + 0.1) for i, s in enumerate(starts)]
        sp = Spoken(0, None, timings)
        for seconds in [-1.0, 0.0, 0.5, 1.0, 3.5, float("nan"), *starts,
                        *(rng.uniform(-0.5, 3.5) for _ in range(5))]:
            assert sp.word_at(seconds) == reference.word_at(timings, seconds), (timings, seconds)
