"""Tests for readaloud.speech.

Fast tests use fake pipelines/tokens and never touch the model.  Tests that need
the real Kokoro weights are marked `@pytest.mark.slow`:

    uv run --with pytest python -m pytest tests/test_speech.py -m "not slow"   # fast
    uv run --with pytest python -m pytest tests/test_speech.py                 # all
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import pytest

from readaloud import speech
from readaloud.speech import (
    CELL_LEAD,
    CELL_TAIL,
    DEFAULT_REPO_ID,
    FALLBACK_VOICES,
    ROW_END_TAIL,
    SAMPLE_RATE,
    Engine,
    Spoken,
    Timed,
    align_words,
    build_timings,
    even_timings,
    list_voices,
    to_mono_f32,
    trim_silence,
    word_spans,
)

# --------------------------------------------------------------------------
# stand-ins for the objects owned by other modules
# --------------------------------------------------------------------------


@dataclass
class FakeChunk:
    """Structurally what `readaloud.document.Chunk` promises."""

    idx: int
    words: list[int]
    text: str
    offsets: list[int]
    line_start: int = 0
    line_end: int = 0
    kind: str = "text"
    row_end: bool = False


@dataclass
class FakeToken:
    """Structurally a misaki MToken."""

    text: str
    whitespace: str = " "
    start_ts: float | None = None
    end_ts: float | None = None
    phonemes: str = "x"


@dataclass
class FakeResult:
    tokens: list[FakeToken]
    audio: np.ndarray


class FakePipeline:
    """Yields canned Results; records how it was called."""

    def __init__(self, results, raises=None):
        self._results = results
        self._raises = raises
        self.calls = []

    def __call__(self, text, voice=None, speed=1.0, split_pattern="\n+"):
        self.calls.append(
            {"text": text, "voice": voice, "speed": speed, "split_pattern": split_pattern}
        )
        if self._raises is not None:
            raise self._raises
        for r in self._results:
            yield r


def silence(seconds: float) -> np.ndarray:
    """A near-silent (1, N) buffer, the shape Kokoro actually returns."""
    n = int(round(seconds * SAMPLE_RATE))
    return np.zeros((1, n), dtype=np.float32)


def chunk_from(text: str, words: list[str]) -> FakeChunk:
    """Build a FakeChunk by locating each word in `text` in order."""
    offsets, cursor = [], 0
    for w in words:
        i = text.index(w, cursor)
        offsets.append(i)
        cursor = i + len(w)
    return FakeChunk(idx=0, words=list(range(len(words))), text=text, offsets=offsets)


def assert_monotonic(timings, nslots, duration=None):
    assert [t.word_slot for t in timings] == list(range(nslots))
    prev = -1.0
    for t in timings:
        assert t.start >= 0.0
        assert t.end >= t.start
        assert t.start >= prev - 1e-9, f"start went backwards at slot {t.word_slot}"
        prev = t.start
    if duration is not None and timings:
        assert timings[-1].end <= duration + 1e-6


# --------------------------------------------------------------------------
# word_spans
# --------------------------------------------------------------------------


def test_word_spans_matches_plain_words():
    text = "Kubernetes orchestrates containers"
    ch = chunk_from(text, ["Kubernetes", "orchestrates", "containers"])
    spans = word_spans(text, ch.offsets)
    assert [text[a:b] for a, b in spans] == [
        "Kubernetes",
        "orchestrates",
        "containers",
    ]


def test_word_spans_trims_trailing_punctuation():
    text = "Hello, world. (Really!)"
    ch = chunk_from(text, ["Hello", "world", "Really"])
    spans = word_spans(text, ch.offsets)
    assert [text[a:b] for a, b in spans] == ["Hello", "world", "Really"]


def test_word_spans_keeps_internal_punctuation():
    text = "Visit https://example.com/a?b=1 now"
    ch = chunk_from(text, ["https://example.com/a?b=1"])
    spans = word_spans(text, ch.offsets)
    assert text[spans[0][0] : spans[0][1]] == "https://example.com/a?b=1"


def test_word_spans_uses_exact_word_texts_when_given():
    text = "co-operate now"
    spans = word_spans(text, [0, 11], ["co-operate", "now"])
    assert [text[a:b] for a, b in spans] == ["co-operate", "now"]


def test_word_spans_are_exact_in_a_cell_that_names_its_glyphs():
    """A glyph's slot says its name, spaces and all, and still aligns."""
    from readaloud.document import Document, Table, TableCell

    # a table drawn like mdcat's, with a row like the README's keys table
    plain = ["─" * 24, " Keys           Action  ", "─" * 24,
             " j · ↓ · Enter  down    ", "─" * 24]
    cells = [TableCell(0, 0, [(1, 1, 14)]), TableCell(0, 1, [(1, 16, 23)]),
             TableCell(1, 0, [(3, 1, 14)]), TableCell(1, 1, [(3, 16, 23)])]
    table = Table(0, 5, 2, [(1, 2), (3, 4)], cells)
    doc = Document.from_text("\n".join(plain), tables=[table], references=False)
    keys = next(c for c in doc.chunks if c.cell == (0, 1, 0))
    assert keys.text == "j, down arrow, Enter"

    spans = word_spans(keys.text, keys.offsets, keys.word_texts)
    assert spans == keys.spans()
    assert [keys.text[a:b] for a, b in spans] == ["j", "down arrow", "Enter"]
    toks = [FakeToken("j", "", 0.0, 0.2), FakeToken(",", " ", 0.2, 0.3),
            FakeToken("down", " ", 0.3, 0.5), FakeToken("arrow", "", 0.5, 0.8),
            FakeToken(",", " ", 0.8, 0.9), FakeToken("Enter", "", 0.9, 1.2)]
    timed = align_words(keys.text, spans, [(toks, 1.2)])
    assert [(slot, round(a, 3), round(b, 3)) for slot, a, b in timed] == [
        (0, 0.0, 0.2), (1, 0.3, 0.8), (2, 0.9, 1.2)]


def test_word_spans_are_sorted_non_overlapping_and_clamped():
    text = "one two"
    # deliberately bogus offsets: out of order and past the end
    spans = word_spans(text, [4, 0, 999])
    assert spans == sorted(spans)
    for (a, b), (c, _d) in zip(spans, spans[1:]):
        assert a <= b <= c
    for a, b in spans:
        assert 0 <= a <= len(text) and a <= b <= len(text)


def test_word_spans_empty():
    assert word_spans("", []) == []


# --------------------------------------------------------------------------
# align_words
# --------------------------------------------------------------------------


def test_align_words_simple_one_result():
    text = "alpha beta gamma"
    spans = word_spans(text, chunk_from(text, ["alpha", "beta", "gamma"]).offsets)
    toks = [
        FakeToken("alpha", " ", 0.0, 0.5),
        FakeToken("beta", " ", 0.5, 1.0),
        FakeToken("gamma", "", 1.0, 1.5),
    ]
    out = align_words(text, spans, [(toks, 1.5)])
    assert out == [(0, 0.0, 0.5), (1, 0.5, 1.0), (2, 1.0, 1.5)]


def test_align_words_none_timestamps_still_advance_the_cursor():
    # '$' has no phonemes and therefore no timestamps, but it consumes text.
    text = "it costs $5 today"
    spans = word_spans(text, chunk_from(text, ["it", "costs", "5", "today"]).offsets)
    toks = [
        FakeToken("it", " ", 0.0, 0.2),
        FakeToken("costs", " ", 0.2, 0.6),
        FakeToken("$", "", None, None),
        FakeToken("5", " ", 0.6, 1.2),
        FakeToken("today", "", 1.2, 1.8),
    ]
    out = align_words(text, spans, [(toks, 1.8)])
    assert out[2] == (2, 0.6, 1.2)  # the '5' kept its own timing
    assert out[3] == (3, 1.2, 1.8)


def test_align_words_interpolates_untimed_trailing_words():
    # join_timestamps reliably drops the last token or two of a Result.
    text = "one two three"
    spans = word_spans(text, chunk_from(text, ["one", "two", "three"]).offsets)
    toks = [
        FakeToken("one", " ", 0.0, 0.4),
        FakeToken("two", " ", 0.4, 0.8),
        FakeToken("three", "", None, None),
    ]
    out = align_words(text, spans, [(toks, 2.0)])
    assert out[2][1] == pytest.approx(0.8)
    assert out[2][2] == pytest.approx(2.0)  # stretched to the audio duration


def test_align_words_offsets_multiple_results_by_cumulative_duration():
    text = "first half here. second half here."
    words = ["first", "half", "here", "second", "half", "here"]
    spans = word_spans(text, chunk_from(text, words).offsets)
    r1 = [
        FakeToken("first", " ", 0.25, 0.5),
        FakeToken("half", " ", 0.5, 0.75),
        FakeToken("here", "", 0.75, 1.0),
        FakeToken(".", " ", 1.0, 1.1),
    ]
    # second Result: timestamps restart near zero
    r2 = [
        FakeToken("second", " ", 0.25, 0.5),
        FakeToken("half", " ", 0.5, 0.75),
        FakeToken("here", "", 0.75, 1.0),
        FakeToken(".", "", 1.0, 1.1),
    ]
    out = align_words(text, spans, [(r1, 2.0), (r2, 2.0)])
    assert out[3] == (3, 2.25, 2.5)  # 'second' pushed past the first Result's audio
    assert out[5] == (5, 2.75, 3.0)
    starts = [s for _, s, _ in out]
    assert starts == sorted(starts)


def test_align_words_one_token_covering_several_words_is_split():
    text = "many   spaces\tand more"
    spans = word_spans(text, chunk_from(text, ["many", "spaces", "and", "more"]).offsets)
    toks = [
        FakeToken("many", " ", 0.0, 0.5),
        FakeToken("  spaces\tand", " ", 0.5, 1.5),  # one token, two words
        FakeToken("more", "", 1.5, 2.0),
    ]
    out = align_words(text, spans, [(toks, 2.0)])
    assert out[1][1] >= 0.5 and out[1][2] <= 1.5
    assert out[2][1] >= out[1][1] and out[2][2] <= 1.5
    assert out[1][2] <= out[2][2]  # 'spaces' finishes no later than 'and'
    assert_monotonic([Timed(*o) for o in out], 4, 2.0)


def test_align_words_skips_unmatchable_tokens_without_raising():
    text = "real words only"
    spans = word_spans(text, chunk_from(text, ["real", "words", "only"]).offsets)
    toks = [
        FakeToken("real", " ", 0.0, 0.3),
        FakeToken("ZZZZZZZZ-not-in-source", " ", 0.3, 0.6),
        FakeToken("words", " ", 0.6, 0.9),
        FakeToken("only", "", 0.9, 1.2),
    ]
    out = align_words(text, spans, [(toks, 1.2)])
    assert_monotonic([Timed(*o) for o in out], 3, 1.2)
    assert out[1] == (1, 0.6, 0.9)


def test_align_words_tolerates_a_dropped_source_character():
    # misaki deletes the quote in print('hello and merges the tokens.
    text = "def main():\n    print('hello, world')"
    spans = word_spans(text, chunk_from(text, ["def", "main", "print", "hello"]).offsets)
    toks = [
        FakeToken("def", " ", 0.0, 0.2),
        FakeToken("main", "", 0.2, 0.5),
        FakeToken("(", "", None, None),
        FakeToken(")", "", None, None),
        FakeToken(":", "", None, None),
        FakeToken("\n    print(hello", "", 0.5, 1.2),
    ]
    out = align_words(text, spans, [(toks, 1.5)])
    assert_monotonic([Timed(*o) for o in out], 4, 1.5)
    assert out[2][1] >= 0.5  # 'print' picked up the merged token's timing


def test_align_words_no_words_or_no_results():
    assert align_words("anything", [], [(([FakeToken("anything")]), 1.0)]) == []
    text = "a b"
    spans = word_spans(text, [0, 2])
    out = align_words(text, spans, [])
    assert [s for _, s, _ in out] == [0.0, 0.0]


def test_align_words_survives_reversed_and_negative_timestamps():
    text = "one two"
    spans = word_spans(text, chunk_from(text, ["one", "two"]).offsets)
    toks = [FakeToken("one", " ", 0.9, 0.1), FakeToken("two", "", -5.0, -1.0)]
    out = align_words(text, spans, [(toks, 1.0)])
    assert_monotonic([Timed(*o) for o in out], 2)


# --------------------------------------------------------------------------
# even_timings / build_timings
# --------------------------------------------------------------------------


def test_even_timings_covers_the_whole_duration():
    spans = [(0, 3), (4, 7), (8, 13)]
    out = even_timings(spans, 4.0)
    assert_monotonic(out, 3, 4.0)
    assert out[0].start == 0.0
    assert out[-1].end == pytest.approx(4.0)


def test_even_timings_zero_duration_and_zero_slots():
    assert even_timings([], 3.0) == []
    out = even_timings([(0, 1), (2, 3)], 0.0)
    assert [(t.start, t.end) for t in out] == [(0.0, 0.0), (0.0, 0.0)]


def test_build_timings_clamps_to_the_audio_duration():
    text = "one two"
    spans = word_spans(text, chunk_from(text, ["one", "two"]).offsets)
    toks = [FakeToken("one", " ", 0.0, 0.5), FakeToken("two", "", 0.5, 99.0)]
    out = build_timings(text, spans, [(toks, 1.0)], 1.0, 2)
    assert_monotonic(out, 2, 1.0)
    assert out[-1].end == pytest.approx(1.0)


def test_build_timings_falls_back_when_alignment_raises(monkeypatch):
    text = "alpha beta gamma"
    spans = word_spans(text, chunk_from(text, ["alpha", "beta", "gamma"]).offsets)

    def boom(*a, **k):
        raise RuntimeError("sabotaged")

    monkeypatch.setattr(speech, "align_words", boom)
    out = build_timings(text, spans, [], 3.0, 3)
    assert_monotonic(out, 3, 3.0)
    assert out[0].start == 0.0
    assert out[-1].end == pytest.approx(3.0)


def test_build_timings_falls_back_when_alignment_returns_the_wrong_shape(monkeypatch):
    text = "alpha beta gamma"
    spans = word_spans(text, chunk_from(text, ["alpha", "beta", "gamma"]).offsets)
    monkeypatch.setattr(speech, "align_words", lambda *a, **k: [(0, 0.0, 1.0)])
    out = build_timings(text, spans, [], 3.0, 3)
    assert len(out) == 3
    assert out[-1].end == pytest.approx(3.0)


def test_build_timings_falls_back_on_nan(monkeypatch):
    spans = [(0, 3), (4, 7)]
    monkeypatch.setattr(
        speech,
        "align_words",
        lambda *a, **k: [(0, 0.0, float("nan")), (1, 1.0, 2.0)],
    )
    out = build_timings("one two", spans, [], 2.0, 2)
    assert all(not math.isnan(t.start) and not math.isnan(t.end) for t in out)


def test_build_timings_no_slots():
    assert build_timings("x", [], [], 1.0, 0) == []


# --------------------------------------------------------------------------
# trim_silence
# --------------------------------------------------------------------------


def secs(seconds: float) -> int:
    return int(round(seconds * SAMPLE_RATE))


def padded_tone(before: float, tone: float, after: float, amp: float = 0.5) -> np.ndarray:
    """Zeros around a 440 Hz block: the shape of one short Kokoro call."""
    t = np.arange(secs(tone)) / SAMPLE_RATE
    block = (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    return np.concatenate(
        [np.zeros(secs(before), np.float32), block, np.zeros(secs(after), np.float32)]
    )


def test_trim_silence_keeps_lead_and_tail_around_the_speech():
    audio = padded_tone(0.4, 0.3, 0.5)
    timings = [Timed(0, 0.40, 0.55), Timed(1, 0.55, 0.70)]

    out, moved = trim_silence(audio, timings, SAMPLE_RATE, lead=0.06, tail=0.12)

    assert out.dtype == np.float32 and out.ndim == 1 and out.flags["C_CONTIGUOUS"]
    assert out.shape[0] == secs(0.06 + 0.3 + 0.12)
    start = secs(0.40 - 0.06)
    np.testing.assert_array_equal(out, audio[start : start + out.shape[0]])
    # timings follow the start cut
    assert [t.start for t in moved] == pytest.approx([0.06, 0.21])
    assert [t.end for t in moved] == pytest.approx([0.21, 0.36])
    assert_monotonic(moved, 2, out.shape[0] / SAMPLE_RATE)
    # the inputs are left alone
    assert audio.shape[0] == secs(1.2)
    assert timings[0] == Timed(0, 0.40, 0.55)


def test_trim_silence_defaults_to_the_cell_padding():
    out, _ = trim_silence(padded_tone(0.4, 0.3, 0.5), [], SAMPLE_RATE)
    assert out.shape[0] == secs(CELL_LEAD) + secs(0.3) + secs(CELL_TAIL)


def test_trim_silence_extends_a_short_tail_with_zeros():
    # some voices stop the audio right after the last phoneme
    audio = padded_tone(0.4, 0.3, 0.05)
    out, _ = trim_silence(audio, [], SAMPLE_RATE, lead=0.06, tail=0.35)
    assert out.shape[0] == secs(0.06 + 0.3 + 0.35)
    kept = audio[secs(0.34) :]
    np.testing.assert_array_equal(out[: kept.shape[0]], kept)
    assert not out[kept.shape[0] :].any()


def test_trim_silence_never_pads_the_lead():
    audio = padded_tone(0.01, 0.3, 0.5)
    out, _ = trim_silence(audio, [Timed(0, 0.01, 0.31)], SAMPLE_RATE, lead=0.06, tail=0.12)
    np.testing.assert_array_equal(out, audio[: secs(0.01 + 0.3 + 0.12)])


def test_trim_silence_clamps_timings_into_the_new_duration():
    audio = padded_tone(0.4, 0.3, 0.5)
    # alignment put the first word in the lead and the last one in the tail
    timings = [Timed(0, 0.10, 0.50), Timed(1, 0.60, 1.20)]

    out, moved = trim_silence(audio, timings, SAMPLE_RATE, lead=0.06, tail=0.12)

    duration = out.shape[0] / SAMPLE_RATE
    assert moved[0].start == 0.0
    assert moved[0].end == pytest.approx(0.50 - 0.34)
    assert moved[1].start == pytest.approx(0.60 - 0.34)
    assert moved[1].end == pytest.approx(duration)
    assert_monotonic(moved, 2, duration)


def test_trim_silence_returns_silent_audio_unchanged():
    audio = np.zeros(secs(1.0), np.float32)
    timings = [Timed(0, 0.2, 0.6)]
    out, moved = trim_silence(audio, timings, SAMPLE_RATE)
    assert out is audio and moved is timings

    # below the absolute floor is silence too, however it compares to its own peak
    hum = padded_tone(0.4, 0.3, 0.5, amp=1e-5)
    assert trim_silence(hum, [], SAMPLE_RATE)[0] is hum

    empty = np.zeros(0, np.float32)
    assert trim_silence(empty, [], SAMPLE_RATE)[0] is empty


def test_trim_silence_leaves_tight_audio_alone():
    # 10 ms too much tail, or 10 ms too little: not worth a new buffer
    for after in (0.13, 0.11):
        audio = padded_tone(0.05, 0.5, after)
        timings = [Timed(0, 0.05, 0.55)]
        out, moved = trim_silence(audio, timings, SAMPLE_RATE, lead=0.06, tail=0.12)
        assert out is audio and moved is timings


def test_trim_silence_is_idempotent():
    out, moved = trim_silence(padded_tone(0.4, 0.3, 0.5), [Timed(0, 0.4, 0.7)], SAMPLE_RATE)
    again, moved_again = trim_silence(out, moved, SAMPLE_RATE)
    assert again is out and moved_again is moved


def test_trim_silence_threshold_is_relative_to_the_loudest_window():
    loud = padded_tone(0.4, 0.3, 0.0)

    # a trail at -60 dB is silence: the tail is measured from the loud part
    whisper = padded_tone(0.0, 0.3, 0.3, amp=0.5 * 0.001)
    out, _ = trim_silence(np.concatenate([loud, whisper]), [], SAMPLE_RATE,
                          lead=0.06, tail=0.12)
    assert out.shape[0] == secs(0.06 + 0.3 + 0.12)

    # at -40 dB, where a voice's weakest final consonants sit, it is speech
    soft = padded_tone(0.0, 0.3, 0.3, amp=0.5 * 0.01)
    out, _ = trim_silence(np.concatenate([loud, soft]), [], SAMPLE_RATE,
                          lead=0.06, tail=0.12)
    assert out.shape[0] == secs(0.06 + 0.6 + 0.12)

    # the same trail ahead of the speech is kept as an onset
    out, _ = trim_silence(np.concatenate([soft[::-1], loud[::-1]]), [], SAMPLE_RATE,
                          lead=0.06, tail=0.12)
    assert out.shape[0] == secs(0.06 + 0.6 + 0.12)


# --------------------------------------------------------------------------
# audio conversion / Spoken
# --------------------------------------------------------------------------


def test_to_mono_f32_flattens_and_casts():
    a = to_mono_f32(np.zeros((1, 10), dtype=np.float64))
    assert a.shape == (10,) and a.dtype == np.float32 and a.flags["C_CONTIGUOUS"]
    assert to_mono_f32(None).shape == (0,)
    assert to_mono_f32([[0.0, 1.0, 2.0]]).shape == (3,)


def test_spoken_duration_and_word_at():
    sp = Spoken(
        chunk_idx=3,
        audio=np.zeros(SAMPLE_RATE, dtype=np.float32),
        timings=[Timed(0, 0.0, 0.4), Timed(1, 0.5, 0.9)],
    )
    assert sp.sample_rate == 24000
    assert sp.duration == pytest.approx(1.0)
    assert sp.word_at(0.1) == 0
    assert sp.word_at(0.45) == 0  # inside the gap: the word that just finished
    assert sp.word_at(0.6) == 1
    assert Spoken(0, np.zeros(0, np.float32), []).duration == 0.0


# --------------------------------------------------------------------------
# Engine (fake pipeline)
# --------------------------------------------------------------------------


def make_engine_with(pipeline) -> Engine:
    eng = Engine()
    eng._pipeline = pipeline
    eng._model = object()
    return eng


def test_engine_synth_uses_split_pattern_none_and_the_configured_voice():
    text = "alpha beta"
    ch = chunk_from(text, ["alpha", "beta"])
    toks = [FakeToken("alpha", " ", 0.0, 0.4), FakeToken("beta", "", 0.4, 0.8)]
    pipe = FakePipeline([FakeResult(toks, silence(0.8))])
    eng = make_engine_with(pipe)
    eng.voice = "am_puck"
    eng.speed = 1.25

    sp = eng.synth(ch)

    assert pipe.calls[0]["split_pattern"] is None
    assert pipe.calls[0]["voice"] == "am_puck"
    assert pipe.calls[0]["speed"] == 1.25
    assert pipe.calls[0]["text"] == text
    assert sp.chunk_idx == 0
    assert sp.audio.dtype == np.float32 and sp.audio.ndim == 1
    assert sp.audio.shape[0] == int(round(0.8 * SAMPLE_RATE))
    assert_monotonic(sp.timings, 2, sp.duration)


def test_engine_synth_concatenates_multiple_results():
    text = "first half here second half here"
    words = ["first", "half", "here", "second", "half", "here"]
    ch = chunk_from(text, words)
    r1 = FakeResult(
        [
            FakeToken("first", " ", 0.25, 0.5),
            FakeToken("half", " ", 0.5, 0.75),
            FakeToken("here", " ", 0.75, 1.0),
        ],
        silence(1.5),
    )
    r2 = FakeResult(
        [
            FakeToken("second", " ", 0.25, 0.5),
            FakeToken("half", " ", 0.5, 0.75),
            FakeToken("here", "", 0.75, 1.0),
        ],
        silence(1.5),
    )
    eng = make_engine_with(FakePipeline([r1, r2]))
    sp = eng.synth(ch)

    assert sp.audio.shape[0] == int(round(3.0 * SAMPLE_RATE))
    assert sp.duration == pytest.approx(3.0)
    assert_monotonic(sp.timings, 6, sp.duration)
    # the second Result's words must land in the second half of the audio
    assert sp.timings[3].start > 1.5


def test_engine_synth_never_raises_when_the_pipeline_explodes():
    text = "alpha beta gamma"
    ch = chunk_from(text, ["alpha", "beta", "gamma"])
    eng = make_engine_with(FakePipeline([], raises=RuntimeError("model exploded")))

    sp = eng.synth(ch)

    assert sp.audio.shape == (0,) and sp.audio.dtype == np.float32
    assert len(sp.timings) == 3
    assert_monotonic(sp.timings, 3, 0.0)
    assert eng.last_error is not None and "model exploded" in eng.last_error


def test_engine_synth_degrades_when_alignment_is_sabotaged(monkeypatch):
    text = "alpha beta gamma"
    ch = chunk_from(text, ["alpha", "beta", "gamma"])
    toks = [
        FakeToken("alpha", " ", 0.0, 0.4),
        FakeToken("beta", " ", 0.4, 0.8),
        FakeToken("gamma", "", 0.8, 1.2),
    ]
    eng = make_engine_with(FakePipeline([FakeResult(toks, silence(1.2))]))

    def boom(*a, **k):
        raise RuntimeError("sabotaged alignment")

    monkeypatch.setattr(speech, "align_words", boom)
    sp = eng.synth(ch)

    assert sp.audio.shape[0] == int(round(1.2 * SAMPLE_RATE))  # audio still correct
    assert_monotonic(sp.timings, 3, sp.duration)
    assert sp.timings[0].start == 0.0
    assert sp.timings[-1].end == pytest.approx(sp.duration)


def test_engine_synth_empty_and_unspeakable_text():
    eng = make_engine_with(FakePipeline([]))
    for text in ("", "   \n\t  "):
        sp = eng.synth(FakeChunk(idx=7, words=[], text=text, offsets=[]))
        assert sp.chunk_idx == 7
        assert sp.audio.shape == (0,)
        assert sp.timings == []
    # a chunk of pure rule characters: still no synthesis attempt is required to
    # succeed, but the call must return a usable Spoken
    sp = eng.synth(FakeChunk(idx=8, words=[], text="────────", offsets=[]))
    assert sp.chunk_idx == 8 and sp.timings == []


def test_engine_synth_tolerates_mismatched_offsets():
    text = "alpha beta gamma"
    ch = FakeChunk(idx=0, words=[0, 1, 2], text=text, offsets=[0])  # too few offsets
    toks = [FakeToken("alpha", " ", 0.0, 0.4)]
    eng = make_engine_with(FakePipeline([FakeResult(toks, silence(0.4))]))
    sp = eng.synth(ch)
    assert len(sp.timings) == 3
    assert_monotonic(sp.timings, 3, sp.duration)


def test_engine_synth_result_with_no_audio():
    text = "alpha beta"
    ch = chunk_from(text, ["alpha", "beta"])
    eng = make_engine_with(FakePipeline([FakeResult([FakeToken("alpha")], None)]))
    sp = eng.synth(ch)
    assert sp.audio.shape == (0,)
    assert len(sp.timings) == 2


def test_engine_synth_times_the_slots_of_a_respelled_chunk_exactly():
    """The Engine says the respelling; each word is timed by what its slot says."""
    from readaloud.document import Document
    from readaloud.pronounce import Lexicon

    doc = Document.from_text(
        "Run kubectl on foo.id in New York\nCity, userId now.",
        pronunciations=Lexicon([("id", "ID"), ("kubectl", "cube control"),
                                ("New York City", "NYC")]))
    [ch] = doc.chunks
    assert ch.text == "Run cube control on foo.ID in NYC, user ID now."
    spans = word_spans(ch.text, ch.offsets, ch.word_texts)
    assert spans == ch.spans()
    assert [ch.text[a:b] for a, b in spans] == [
        "Run", "cube control", "on", "foo.ID", "in", "N", "Y", "C", "user ID",
        "now"]

    # tokens the way misaki splits the respelled text
    toks = [FakeToken("Run", " ", 0.0, 0.3), FakeToken("cube", " ", 0.3, 0.6),
            FakeToken("control", " ", 0.6, 1.0), FakeToken("on", " ", 1.0, 1.2),
            FakeToken("foo", "", 1.2, 1.5), FakeToken(".", "", None, None),
            FakeToken("ID", " ", 1.5, 1.9), FakeToken("in", " ", 1.9, 2.0),
            FakeToken("NYC", "", 2.0, 2.6), FakeToken(",", " ", 2.6, 2.7),
            FakeToken("user", " ", 2.7, 3.0), FakeToken("ID", " ", 3.0, 3.3),
            FakeToken("now", "", 3.3, 3.6), FakeToken(".", "", 3.6, 3.7)]
    timings = build_timings(ch.text, spans, [(toks, 4.0)], 4.0, len(ch.words))
    assert_monotonic(timings, len(ch.words), 4.0)
    assert [(t.start, t.end) for t in timings] == [
        (0.0, 0.3), (0.3, 1.0), (1.0, 1.2), (1.2, 1.9), (1.9, 2.0),
        (2.0, pytest.approx(2.2)), (pytest.approx(2.2), pytest.approx(2.4)),
        (pytest.approx(2.4), 2.6), (2.7, 3.3), (3.3, 3.6)]

    pipe = FakePipeline([FakeResult(toks, silence(4.0))])
    sp = make_engine_with(pipe).synth(ch)
    assert pipe.calls[0]["text"] == ch.text
    assert sp.timings == timings


# -- table cells: Kokoro's padding is trimmed --------------------------------


def cell_chunk(kind: str = "cell", row_end: bool = False) -> FakeChunk:
    ch = chunk_from("alpha beta", ["alpha", "beta"])
    ch.kind = kind
    ch.row_end = row_end
    return ch


def padded_pipeline(after: float = 0.5) -> FakePipeline:
    """One Result shaped like Kokoro's: 0.4 s of silence, 0.3 s of speech, `after`."""
    toks = [FakeToken("alpha", " ", 0.40, 0.55), FakeToken("beta", "", 0.55, 0.70)]
    return FakePipeline([FakeResult(toks, padded_tone(0.4, 0.3, after)[None, :])])


def test_engine_synth_trims_the_padding_of_a_cell():
    sp = make_engine_with(padded_pipeline()).synth(cell_chunk())

    assert sp.audio.shape[0] == secs(CELL_LEAD) + secs(0.3) + secs(CELL_TAIL)
    assert sp.audio.dtype == np.float32 and sp.audio.ndim == 1
    assert [t.start for t in sp.timings] == pytest.approx([CELL_LEAD, CELL_LEAD + 0.15])
    assert_monotonic(sp.timings, 2, sp.duration)
    assert sp.word_at(sp.duration) == 1  # the pause after the cell keeps its last word


def test_engine_synth_pauses_longer_after_the_last_cell_of_a_row():
    assert ROW_END_TAIL > CELL_TAIL
    expected = secs(CELL_LEAD) + secs(0.3) + secs(ROW_END_TAIL)

    sp = make_engine_with(padded_pipeline()).synth(cell_chunk(row_end=True))
    assert sp.audio.shape[0] == expected
    assert_monotonic(sp.timings, 2, sp.duration)

    # the beat is there even when the voice left almost no tail of its own
    sp = make_engine_with(padded_pipeline(after=0.02)).synth(cell_chunk(row_end=True))
    assert sp.audio.shape[0] == expected


def test_engine_synth_never_trims_other_kinds():
    # the kinds a Document makes, and "text", the default of a hand-built chunk
    for kind in ("para", "code", "blank", "rule", "text"):
        sp = make_engine_with(padded_pipeline()).synth(cell_chunk(kind=kind))
        assert sp.audio.shape[0] == secs(1.2), kind
    # a chunk that predates `kind` altogether
    bare = SimpleNamespace(idx=0, words=[0, 1], text="alpha beta", offsets=[0, 6])
    sp = make_engine_with(padded_pipeline()).synth(bare)
    assert sp.audio.shape[0] == secs(1.2)


def test_engine_synth_silent_or_failed_cell_keeps_its_length():
    toks = [FakeToken("alpha", " ", 0.0, 0.4), FakeToken("beta", "", 0.4, 0.8)]
    sp = make_engine_with(FakePipeline([FakeResult(toks, silence(0.8))])).synth(cell_chunk())
    assert sp.audio.shape[0] == secs(0.8)
    assert_monotonic(sp.timings, 2, sp.duration)

    eng = make_engine_with(FakePipeline([], raises=RuntimeError("model exploded")))
    sp = eng.synth(cell_chunk(row_end=True))
    assert sp.audio.shape == (0,)
    assert_monotonic(sp.timings, 2, 0.0)


def test_engine_synth_keeps_the_padding_when_trimming_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sabotaged trim")

    monkeypatch.setattr(speech, "trim_silence", boom)
    sp = make_engine_with(padded_pipeline()).synth(cell_chunk())
    assert sp.audio.shape[0] == secs(1.2)
    assert_monotonic(sp.timings, 2, sp.duration)


# --------------------------------------------------------------------------
# Engine.load
# --------------------------------------------------------------------------


class CountingEngine(Engine):
    """Engine whose model build is counted and slow, without any real weights."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.builds = 0
        self.fail_first = False

    def _build_pipeline(self):
        self.builds += 1
        if self.fail_first and self.builds == 1:
            raise RuntimeError("weights missing")
        threading.Event().wait(0.05)  # long enough for the racers to pile up
        return object(), FakePipeline([])


def test_engine_load_is_idempotent():
    eng = CountingEngine()
    assert eng.loaded is False
    eng.load()
    eng.load()
    eng.load()
    assert eng.builds == 1
    assert eng.loaded is True


def test_engine_load_is_thread_safe():
    eng = CountingEngine()
    errors: list[BaseException] = []

    def worker():
        try:
            eng.load()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert eng.builds == 1
    assert eng.loaded


def test_engine_load_failure_leaves_engine_retryable():
    eng = CountingEngine()
    eng.fail_first = True
    with pytest.raises(RuntimeError):
        eng.load()
    assert eng.loaded is False
    eng.load()
    assert eng.loaded is True
    assert eng.builds == 2


def test_engine_synth_lazily_loads():
    eng = CountingEngine()
    ch = chunk_from("alpha beta", ["alpha", "beta"])
    sp = eng.synth(ch)
    assert eng.builds == 1
    assert eng.loaded
    assert len(sp.timings) == 2


def test_engine_close_is_safe_and_repeatable():
    eng = CountingEngine()
    eng.load()
    eng.close()
    eng.close()
    assert eng.loaded is False


# --------------------------------------------------------------------------
# voices
# --------------------------------------------------------------------------


def test_list_voices_from_the_snapshot():
    voices = list_voices()
    assert voices == sorted(set(voices))
    assert all(isinstance(v, str) and v for v in voices)
    assert "af_heart" in voices
    assert len(voices) >= 20


def test_list_voices_filters_by_lang_code():
    british = list_voices(lang_code="b")
    assert british and all(v.startswith("b") for v in british)
    assert "bf_emma" in british
    assert "af_heart" not in british


def test_list_voices_falls_back_when_the_repo_is_unknown():
    voices = list_voices(repo_id="definitely-not-a-real/repo-xyz")
    assert voices == sorted(FALLBACK_VOICES)
    assert speech.DEFAULT_VOICE in voices


def test_engine_defaults_match_the_contract():
    eng = Engine()
    assert eng.voice == "af_heart"
    assert eng.speed == 1.0
    assert eng.lang_code == "a"
    assert eng.repo_id == DEFAULT_REPO_ID
    assert eng.sample_rate == SAMPLE_RATE == 24000
    assert eng.loaded is False and eng.last_error is None


def test_engine_voices_uses_its_own_repo_and_lang():
    eng = Engine(lang_code="b")
    voices = eng.voices()
    assert voices and all(v.startswith("b") for v in voices)


# --------------------------------------------------------------------------
# real synthesis (needs the Kokoro weights)
# --------------------------------------------------------------------------

REAL_TEXT = "Kubernetes orchestrates containers. It runs fast."
REAL_WORDS = ["Kubernetes", "orchestrates", "containers", "It", "runs", "fast"]


@pytest.fixture(scope="module")
def real_engine():
    eng = Engine()
    eng.load()
    return eng


@pytest.mark.slow
def test_real_synthesis_end_to_end(real_engine):
    ch = chunk_from(REAL_TEXT, REAL_WORDS)
    sp = real_engine.synth(ch)

    assert real_engine.last_error is None
    assert sp.chunk_idx == 0
    assert sp.sample_rate == 24000
    assert sp.audio.dtype == np.float32
    assert sp.audio.ndim == 1 and sp.audio.shape[0] > SAMPLE_RATE  # over one second
    assert sp.audio.flags["C_CONTIGUOUS"]
    assert np.isfinite(sp.audio).all()

    # every speakable word slot gets a timing, monotonic, inside the audio
    assert len(sp.timings) == len(REAL_WORDS)
    assert_monotonic(sp.timings, len(REAL_WORDS), sp.duration)
    assert sp.timings[-1].end <= sp.duration + 1e-6

    # the alignment is real, not a fallback: words are spread across the clip
    assert sp.timings[0].start < 1.0
    assert sp.timings[-1].start > sp.duration * 0.5
    # non-degenerate: each word occupies some time
    assert all(t.end > t.start for t in sp.timings)
    # and the highlight can be resolved back from a play position
    assert sp.word_at(sp.timings[0].start + 1e-3) == 0
    assert sp.word_at(sp.timings[-1].start + 1e-3) == len(REAL_WORDS) - 1


@pytest.mark.slow
def test_real_synthesis_with_untimed_tokens(real_engine):
    # '$' and '--' produce tokens with start_ts/end_ts == None.
    text = "It costs $5 and runs -- fast."
    words = ["It", "costs", "5", "and", "runs", "fast"]
    ch = chunk_from(text, words)
    sp = real_engine.synth(ch)

    assert len(sp.timings) == len(words)
    assert_monotonic(sp.timings, len(words), sp.duration)
    assert sp.timings[-1].end <= sp.duration + 1e-6
    assert real_engine.last_error is None


@pytest.mark.slow
def test_real_synthesis_degraded_timings_when_alignment_sabotaged(
    real_engine, monkeypatch
):
    ch = chunk_from(REAL_TEXT, REAL_WORDS)
    good = real_engine.synth(ch)

    def boom(*a, **k):
        raise RuntimeError("sabotaged alignment")

    monkeypatch.setattr(speech, "align_words", boom)
    bad = real_engine.synth(ch)

    # same audio, degraded timings
    assert bad.audio.shape == good.audio.shape
    assert bad.duration == pytest.approx(good.duration)
    assert len(bad.timings) == len(REAL_WORDS)
    assert_monotonic(bad.timings, len(REAL_WORDS), bad.duration)
    assert bad.timings[0].start == 0.0
    assert bad.timings[-1].end == pytest.approx(bad.duration)
    # evenly distributed means proportional to word length, not the real timings
    assert bad.timings != good.timings


@pytest.mark.slow
def test_real_synthesis_long_chunk_may_yield_several_results(real_engine):
    # ~1200 chars forces the internal 510-phoneme split into >1 Result.
    sentence = (
        "The quick brown fox jumps over the lazy dog while the engine "
        "synthesizes a rather long paragraph of speech. "
    )
    text = (sentence * 6).strip()
    words = [w for w in text.replace(".", "").split() if w]
    ch = chunk_from(text, words)
    sp = real_engine.synth(ch)

    assert sp.duration > 10.0
    assert len(sp.timings) == len(words)
    assert_monotonic(sp.timings, len(words), sp.duration)
    # timings must keep climbing across the Result boundary, not restart
    assert sp.timings[-1].start > sp.duration * 0.8


@pytest.mark.slow
def test_real_synthesis_trims_a_cell(real_engine, monkeypatch):
    # Kokoro is not sample-for-sample deterministic, so compare against the
    # very buffer that was trimmed rather than a second synthesis.
    seen = []

    def spy(audio, timings, sample_rate, **kw):
        out = trim_silence(audio, timings, sample_rate, **kw)
        seen.append((audio, out[0]))
        return out

    monkeypatch.setattr(speech, "trim_silence", spy)
    text, words = "Supports fast sync", ["Supports", "fast", "sync"]
    ch = chunk_from(text, words)
    ch.kind = "cell"
    cell = real_engine.synth(ch)

    assert real_engine.last_error is None
    [(padded, trimmed)] = seen
    assert trimmed is cell.audio
    # Kokoro's padding around one call comes to well over 0.3 s
    assert cell.duration < padded.shape[0] / SAMPLE_RATE - 0.3
    assert_monotonic(cell.timings, len(words), cell.duration)
    assert all(t.end > t.start for t in cell.timings)
    # the speech itself is all there
    energy = float(np.square(cell.audio, dtype=np.float64).sum())
    assert energy > 0.999 * float(np.square(padded, dtype=np.float64).sum())
    # what is left is exactly lead + speech + tail: trimming again changes nothing
    again, _ = trim_silence(cell.audio, cell.timings, cell.sample_rate)
    assert again is cell.audio


@pytest.mark.slow
def test_real_synthesis_says_the_pronunciations(real_engine):
    from readaloud.document import Document
    from readaloud.pronounce import Lexicon

    doc = Document.from_text(
        "Look up foo.id for the userId,\nthen run kubectl in New York\n"
        "City before the id expires.",
        pronunciations=Lexicon([("id", "ID"), ("kubectl", "cube control"),
                                ("New York City", "NYC")]))
    [ch] = doc.chunks
    assert doc.respell_failures == 0
    assert ch.text == ("Look up foo.ID for the user ID,\nthen run cube control "
                       "in NYC before the ID expires.")
    sp = real_engine.synth(ch)

    assert real_engine.last_error is None
    assert len(sp.timings) == len(ch.words)
    assert_monotonic(sp.timings, len(ch.words), sp.duration)
    # misaki reads "the id" as Freud's id (ˈɪd) and ID as "eye dee" (ˌIdˈi),
    # and kubectl as "kjˈubɛktᵊl" where "cube control" has its control
    phonemes, _tokens = real_engine._pipeline.g2p(ch.text)
    assert "ˌIdˈi" in phonemes and "kjˈub" in phonemes
    assert "kəntɹˈOl" in phonemes and "ˈɪd" not in phonemes


@pytest.mark.slow
def test_real_engine_load_is_idempotent(real_engine):
    before = real_engine._pipeline
    real_engine.load()
    assert real_engine._pipeline is before


# -- close() must never wait on an in-flight load ------------------------------


def test_close_does_not_block_on_an_in_flight_load():
    """`q` during model load must exit now, not in 3.5s."""
    import threading
    import time

    from readaloud.speech import Engine

    eng = Engine()
    started = threading.Event()
    release = threading.Event()

    def slow_build():
        started.set()
        release.wait(5.0)
        return object(), object()

    eng._build_pipeline = slow_build  # type: ignore[method-assign]
    worker = threading.Thread(target=eng.load, daemon=True)
    worker.start()
    assert started.wait(2.0), "build never started"

    t0 = time.monotonic()
    eng.close()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.25, f"close() blocked for {elapsed:.2f}s"

    release.set()
    worker.join(5.0)
    # The load finished after close(); it must NOT have published a pipeline.
    assert not eng.loaded, "close() was overtaken by the load it cancelled"


def test_load_after_close_stays_closed():
    from readaloud.speech import Engine

    eng = Engine()
    eng._build_pipeline = lambda: (object(), object())  # type: ignore[method-assign]
    eng.close()
    eng.load()
    assert not eng.loaded
