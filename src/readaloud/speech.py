"""Kokoro TTS synthesis with word-level timing alignment.

Public surface:

    Timed(word_slot, start, end)
    Spoken(chunk_idx, audio, timings, sample_rate=24000)
    Engine(voice, speed, lang_code, repo_id).load() / .synth(chunk)

`Engine.load()` is blocking (~3.5s, mostly spaCy inside `KokoroPipeline`), safe to
call from a worker thread, and idempotent.  `Engine.synth()` never raises on
ordinary text: a broken alignment degrades to evenly distributed timings and a
broken pipeline degrades to empty audio plus `Engine.last_error`.  A table cell
(`chunk.kind == "cell"`) comes back with most of Kokoro's silence padding cut
off (`trim_silence`), so a table read cell by cell does not stall between cells.

The token -> word-slot alignment (`align_words`) is the version proven by the
token-align spike against mlx-community/Kokoro-82M-4bit + misaki 0.9.4 over 16
texts (prose, code, markdown tables, URLs, emoji, accents, NBSP, empty input),
single- and multi-Result, with zero monotonicity or range violations.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from readaloud.document import Chunk

__all__ = [
    "SAMPLE_RATE",
    "DEFAULT_REPO_ID",
    "DEFAULT_VOICE",
    "FALLBACK_VOICES",
    "Timed",
    "Spoken",
    "Engine",
    "align_words",
    "word_spans",
    "even_timings",
    "build_timings",
    "trim_silence",
    "list_voices",
    "to_mono_f32",
]

log = logging.getLogger("readaloud.speech")

SAMPLE_RATE = 24000
DEFAULT_REPO_ID = "mlx-community/Kokoro-82M-4bit"
DEFAULT_VOICE = "af_heart"

#: Used only when the model snapshot cannot be inspected (no cache, no network).
#: This is the Kokoro v1.0 voice set; `list_voices()` prefers the real snapshot.
FALLBACK_VOICES: tuple[str, ...] = (
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
    "pf_dora", "pm_alex", "pm_santa",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
    "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
)


# --------------------------------------------------------------------------
# contract dataclasses
# --------------------------------------------------------------------------

@dataclass
class Timed:
    """One word slot's span inside a chunk's audio."""

    word_slot: int  # index into Chunk.words
    start: float  # seconds within this chunk's audio
    end: float


@dataclass
class Spoken:
    """Synthesized audio for one chunk plus its per-word timings."""

    chunk_idx: int
    audio: np.ndarray  # float32, mono, 24000 Hz, shape (N,)
    timings: list[Timed]  # sorted by start; monotonic; gaps allowed
    sample_rate: int = 24000

    @property
    def duration(self) -> float:
        """Length of `audio` in seconds (0.0 when empty)."""
        sr = self.sample_rate or SAMPLE_RATE
        return float(self.audio.shape[0]) / float(sr) if self.audio.size else 0.0

    def word_at(self, seconds: float) -> int | None:
        """Word slot audible at `seconds`, or None before the first / after the last.

        Timings may have gaps (punctuation, pauses); a time inside a gap resolves
        to the word that just finished, which is what a highlight should show.
        """
        found: int | None = None
        for t in self.timings:
            if t.start > seconds:
                break
            found = t.word_slot
        return found


# --------------------------------------------------------------------------
# token -> word-slot alignment  (verified by the token-align spike)
# --------------------------------------------------------------------------

_MAX_SKIP = 12  # source chars misaki may drop inside one token
_SEARCH_WINDOW = 400  # how far ahead of the cursor a token may start


def _locate(text: str, needle: str, cursor: int) -> tuple[int, int] | None:
    """Find `needle`'s char span in `text` at/after `cursor`, tolerating the ways
    misaki mangles token text (absorbed leading whitespace, dropped quotes)."""
    if not needle:
        return None
    n = len(text)

    # 1. exact match separated from the cursor by whitespace only (the normal case).
    pos = text.find(needle, cursor)
    if pos >= 0 and not text[cursor:pos].strip():
        return pos, pos + len(needle)

    # 2. misaki sometimes glues leading whitespace/newlines onto .text; retry trimmed.
    trimmed = needle.strip()
    if trimmed and trimmed != needle:
        p = text.find(trimmed, cursor)
        if p >= 0 and not text[cursor:p].strip():
            return p, p + len(trimmed)
    else:
        trimmed = needle

    # 3. subsequence match allowing dropped source characters (e.g. print('hello).
    limit = min(n, cursor + _SEARCH_WINDOW)
    first = trimmed[0]
    for start in range(cursor, limit):
        if text[start] != first:
            continue
        i, j, skipped = start, 0, 0
        while i < n and j < len(trimmed):
            if text[i] == trimmed[j]:
                i += 1
                j += 1
            else:
                i += 1
                skipped += 1
                if skipped > _MAX_SKIP:
                    break
        if j == len(trimmed):
            return start, i

    # 4. last resort: a plain forward exact match, even across a non-whitespace gap.
    if pos >= 0:
        return pos, pos + len(needle)
    p = text.find(trimmed, cursor)
    if p >= 0:
        return p, p + len(trimmed)
    return None


def align_words(
    text: str,
    spans: Sequence[tuple[int, int]],
    results: Iterable[tuple[Sequence[Any], float]],
) -> list[tuple[int, float, float]]:
    """Map misaki tokens onto word slots.

    `text`     the exact string handed to the pipeline (chunk.text).
    `spans`    [(start, end)] char offsets of each word slot inside `text`, in
               order, non-overlapping.
    `results`  one (tokens, audio_duration_seconds) pair per pipeline Result, in
               yield order.  Timestamps inside each Result are relative to that
               Result's own audio, so they are offset by the cumulative duration
               of everything yielded before it.

    Returns [(word_slot, start, end)] for every word slot, sorted by slot index,
    with monotonically non-decreasing, non-negative times.
    """
    word_spans_ = spans
    nwords = len(word_spans_)
    if nwords == 0:
        return []

    lo: list[float | None] = [None] * nwords
    hi: list[float | None] = [None] * nwords

    cursor = 0
    time_offset = 0.0
    total = 0.0
    wi = 0  # first word slot that may still overlap (spans is sorted)

    for tokens, duration in results:
        for tok in tokens or ():
            raw = getattr(tok, "text", None) or ""
            if not raw:
                continue
            span = _locate(text, raw, cursor)
            if span is None:
                continue  # unmatchable token: skip, keep the cursor
            s_char, e_char = span
            cursor = e_char

            ts = getattr(tok, "start_ts", None)
            te = getattr(tok, "end_ts", None)
            if ts is None and te is None:
                continue  # e.g. a bare '$' -- consumes text, no timing
            if ts is None:
                ts = te
            if te is None:
                te = ts
            ts = max(0.0, float(ts)) + time_offset
            te = max(0.0, float(te)) + time_offset
            if te < ts:
                ts, te = te, ts

            # advance the word window, then collect every word this token covers
            while wi < nwords and word_spans_[wi][1] <= s_char:
                wi += 1
            covered = []
            k = wi
            while k < nwords and word_spans_[k][0] < e_char:
                a, b = word_spans_[k]
                overlap = min(b, e_char) - max(a, s_char)
                if overlap > 0:
                    covered.append((k, max(a, s_char), min(b, e_char)))
                k += 1
            if not covered:
                continue

            if len(covered) == 1:
                slots = [(covered[0][0], ts, te)]
            else:
                # one token spoke several words ("  spaces\tand"): split its span
                # proportionally to how much of the token each word occupies.
                base = covered[0][1]
                width = max(1, covered[-1][2] - base)
                slots = []
                for idx, a, b in covered:
                    f0 = (a - base) / width
                    f1 = (b - base) / width
                    slots.append((idx, ts + (te - ts) * f0, ts + (te - ts) * f1))

            for idx, a_t, b_t in slots:
                lo[idx] = a_t if lo[idx] is None else min(lo[idx], a_t)
                hi[idx] = b_t if hi[idx] is None else max(hi[idx], b_t)

        time_offset += max(0.0, float(duration))
        total = time_offset

    if total <= 0.0:
        known = [h for h in hi if h is not None]
        total = max(known) if known else 0.0

    # fill untimed slots by interpolating between the timed anchors around them
    i = 0
    prev_end = 0.0
    while i < nwords:
        if lo[i] is not None:
            prev_end = hi[i]  # type: ignore[assignment]
            i += 1
            continue
        j = i
        while j < nwords and lo[j] is None:
            j += 1
        nxt = lo[j] if j < nwords else total
        left, right = prev_end, max(prev_end, nxt)
        widths = [max(1, word_spans_[k][1] - word_spans_[k][0]) for k in range(i, j)]
        tot_w = sum(widths)
        t = left
        for k, w in zip(range(i, j), widths):
            step = (right - left) * (w / tot_w)
            lo[k], hi[k] = t, t + step
            t += step
        prev_end = right
        i = j

    out: list[tuple[int, float, float]] = []
    run = 0.0
    for k in range(nwords):
        a = max(run, float(lo[k]))  # type: ignore[arg-type]
        b = max(a, float(hi[k]))  # type: ignore[arg-type]
        out.append((k, a, b))
        run = a
    return out


# --------------------------------------------------------------------------
# word spans, degraded timings, validation
# --------------------------------------------------------------------------

# Trailing characters that document.py would not include in Word.text but that
# sit inside a chunk's raw text right after the word.
_TRAILING_PUNCT = "".join(
    (".,;:!?)]}>\"'", "’”…»‘“«*_`~")
)


def word_spans(
    text: str,
    offsets: Sequence[int],
    word_texts: Sequence[str] | None = None,
) -> list[tuple[int, int]]:
    """Derive one (start, end) char span per word slot inside `text`.

    `Chunk.offsets` only carries each word's START offset, and
    `Engine.synth` has no access to the `Document` that owns `Word.text`, so the
    end is inferred: scan to the next whitespace, stop at the next word's offset,
    and trim trailing punctuation that document.py would have excluded.  When the
    caller does have the word texts it can pass them for an exact answer.

    The returned spans are clamped into `text`, non-overlapping and sorted.
    """
    n = len(text)
    spans: list[tuple[int, int]] = []
    cnt = len(offsets)
    floor = 0
    for i in range(cnt):
        try:
            start = int(offsets[i])
        except (TypeError, ValueError):
            start = floor
        start = max(floor, min(n, start))

        if i + 1 < cnt:
            try:
                nxt = int(offsets[i + 1])
            except (TypeError, ValueError):
                nxt = n
            limit = max(start, min(n, nxt))
        else:
            limit = n

        end = -1
        if word_texts is not None and i < len(word_texts):
            wt = word_texts[i] or ""
            if wt and text.startswith(wt, start):
                end = start + len(wt)

        if end < 0:
            j = start
            while j < limit and not text[j].isspace():
                j += 1
            while j > start + 1 and text[j - 1] in _TRAILING_PUNCT:
                j -= 1
            end = j

        end = max(start, min(n, end))
        if end == start and start < n:
            end = start + 1
        spans.append((start, end))
        floor = end
    return spans


def even_timings(
    spans: Sequence[tuple[int, int]], duration: float, nslots: int | None = None
) -> list[Timed]:
    """Degraded fallback: spread `duration` over the slots by character width.

    Used when real alignment is impossible or produced nonsense.  Reading is
    still usable (the highlight sweeps at roughly the right pace); only the
    per-word precision is lost.
    """
    n = len(spans) if nslots is None else int(nslots)
    if n <= 0:
        return []
    dur = max(0.0, float(duration))
    widths = [
        max(1, (spans[i][1] - spans[i][0]) if i < len(spans) else 1) for i in range(n)
    ]
    total_w = float(sum(widths)) or 1.0
    out: list[Timed] = []
    t = 0.0
    for i, w in enumerate(widths):
        step = dur * (w / total_w)
        out.append(Timed(i, t, min(dur, t + step)))
        t = min(dur, t + step)
    return out


def _sanitize(timings: list[Timed], nslots: int, duration: float) -> list[Timed]:
    """Clamp into [0, duration] and enforce the contract's monotonicity.

    Raises ValueError when the alignment is structurally wrong (wrong number of
    slots, wrong slot ids), which is the signal to fall back to `even_timings`.
    """
    if len(timings) != nslots:
        raise ValueError(f"alignment produced {len(timings)} timings for {nslots} slots")
    hi = max(0.0, float(duration))
    run = 0.0
    out: list[Timed] = []
    for i, t in enumerate(timings):
        if int(t.word_slot) != i:
            raise ValueError(f"alignment slot {t.word_slot!r} out of order at {i}")
        s = float(t.start)
        e = float(t.end)
        if s != s or e != e:  # NaN
            raise ValueError("alignment produced NaN timings")
        if hi > 0.0:
            s = min(max(0.0, s), hi)
            e = min(max(0.0, e), hi)
        else:
            s = max(0.0, s)
            e = max(0.0, e)
        s = max(run, s)
        e = max(s, e)
        out.append(Timed(i, s, e))
        run = s
    return out


def build_timings(
    text: str,
    spans: Sequence[tuple[int, int]],
    results: Iterable[tuple[Sequence[Any], float]],
    duration: float,
    nslots: int,
) -> list[Timed]:
    """Align, validate and clamp -- degrading to `even_timings` on any failure.

    This is the single place `Engine.synth` turns pipeline output into `Timed`s,
    factored out so it can be exercised (and sabotaged) without the model.
    """
    if nslots <= 0:
        return []
    try:
        raw = align_words(text, spans, results)
        timings = [Timed(int(s), float(a), float(b)) for s, a, b in raw]
        return _sanitize(timings, nslots, duration)
    except Exception:  # noqa: BLE001 - alignment must never break the reader
        log.warning("word alignment failed; using evenly distributed timings",
                    exc_info=True)
        return even_timings(spans, duration, nslots)


# --------------------------------------------------------------------------
# silence trimming (table cells)
# --------------------------------------------------------------------------

#: Kokoro pads every call with ~0.2-0.4 s of silence before the speech and up
#: to ~0.6 s after it, depending on the voice.  A paragraph never notices, but
#: a table read one cell at a time would stall for about a second between
#: cells, so cell chunks keep only this much of it: a little lead so an onset
#: is never clipped, a short tail between cells and a longer one after the
#: last cell of a row.
CELL_LEAD = 0.06
CELL_TAIL = 0.12
ROW_END_TAIL = 0.35

_MIN_TRIM = 0.02  # seconds; a smaller change is not worth a new buffer


def trim_silence(
    audio: np.ndarray,
    timings: list[Timed],
    sample_rate: int,
    *,
    lead: float = CELL_LEAD,
    tail: float = CELL_TAIL,
    floor: float = 1e-4,
    ratio: float = 0.003,
    window: float = 0.01,
) -> tuple[np.ndarray, list[Timed]]:
    """Cut `audio` down to `lead` seconds before the speech and `tail` after it.

    Speech is where the RMS envelope (`window`-second frames) rises above
    `ratio` of its loudest frame, or `floor` for very quiet audio.  The kept
    padding is measured from the first and last such frame.  `ratio` sits at
    -50 dB because the weakest consonants of some voices do not reach -40 dB
    (af_alloy's final "t" burst, af_nicole's final "s"); a 1% threshold
    measured the tail from before them and cut them off.

    Some voices (bf_emma) end the audio almost on the last phoneme, so a tail
    shorter than `tail` is extended with zeros: the pause after a cell, and
    the longer one after a row, must not depend on the voice.  The lead is
    only ever cut, never padded: it just protects the onset, and the previous
    cell's tail already makes the pause.

    Timings move with the start cut and are clamped into the new duration.
    Returns the inputs unchanged when nothing rises above the threshold or the
    cut and the padding together come to less than 20 ms, so silent or
    already tight audio keeps its exact length.
    """
    n = int(audio.shape[0])
    sr = int(sample_rate) or SAMPLE_RATE
    if n == 0:
        return audio, timings
    win = max(1, int(round(window * sr)))
    frames = -(-n // win)
    power = np.zeros(frames * win, dtype=np.float64)
    power[:n] = np.square(audio, dtype=np.float64)
    counts = np.full(frames, win, dtype=np.float64)
    counts[-1] = n - (frames - 1) * win  # the last frame may be short
    envelope = np.sqrt(power.reshape(frames, win).sum(axis=1) / counts)

    loud = np.flatnonzero(envelope > max(floor, ratio * float(envelope.max())))
    if loud.size == 0:
        return audio, timings
    onset = int(loud[0]) * win
    offset = min(n, (int(loud[-1]) + 1) * win)
    start = max(0, onset - int(round(lead * sr)))
    end = offset + int(round(tail * sr))  # past `n` when the tail needs zeros
    if start + abs(n - end) < int(round(_MIN_TRIM * sr)):
        return audio, timings

    # A fresh buffer, not a view: a view would pin the whole padded original.
    out = np.zeros(end - start, dtype=np.float32)
    kept = audio[start:min(n, end)]
    out[: kept.shape[0]] = kept
    shift = start / float(sr)
    moved = [Timed(t.word_slot, t.start - shift, t.end - shift) for t in timings]
    return out, _sanitize(moved, len(timings), out.shape[0] / float(sr))


# --------------------------------------------------------------------------
# voices
# --------------------------------------------------------------------------

def _snapshot_voices(repo_id: str, allow_download: bool) -> list[str]:
    """Voice names from the downloaded HF snapshot, or [] if it is not there."""
    import glob

    local = os.path.expanduser(str(repo_id))
    roots: list[str] = []
    if os.path.isdir(local):
        roots.append(local)
    else:
        try:
            from huggingface_hub import snapshot_download

            roots.append(
                snapshot_download(
                    repo_id,
                    allow_patterns=["voices/*.safetensors"],
                    local_files_only=not allow_download,
                )
            )
        except Exception:  # noqa: BLE001 - offline / not cached / no hub
            return []

    names: set[str] = set()
    for root in roots:
        for path in glob.glob(os.path.join(root, "voices", "*.safetensors")):
            names.add(os.path.splitext(os.path.basename(path))[0])
    return sorted(names)


def list_voices(
    repo_id: str = DEFAULT_REPO_ID,
    lang_code: str | None = None,
    allow_download: bool = False,
) -> list[str]:
    """Available Kokoro voice names, discovered from the model snapshot.

    Reads `voices/*.safetensors` out of the downloaded HF snapshot (or a local
    model directory) rather than hardcoding; falls back to `FALLBACK_VOICES`
    when nothing is cached and no download is allowed.  `lang_code` ("a", "b",
    "j", ...) filters to the voices that pipeline can actually speak.
    """
    names = _snapshot_voices(repo_id, allow_download)
    if not names:
        names = list(FALLBACK_VOICES)
    if lang_code:
        prefix = str(lang_code).lower()[:1]
        filtered = [v for v in names if v[:1] == prefix]
        if filtered:
            names = filtered
    return sorted(names)


# --------------------------------------------------------------------------
# audio conversion
# --------------------------------------------------------------------------

def to_mono_f32(audio: Any) -> np.ndarray:
    """mx.array (1, N) -> contiguous float32 numpy array of shape (N,)."""
    if audio is None:
        return np.zeros(0, dtype=np.float32)
    try:
        arr = np.asarray(audio)
    except Exception:  # noqa: BLE001 - exotic array types
        try:
            arr = np.asarray(audio.tolist())
        except Exception:  # noqa: BLE001
            return np.zeros(0, dtype=np.float32)
    if arr.dtype != np.float32:
        try:
            arr = arr.astype(np.float32)
        except Exception:  # noqa: BLE001 - e.g. bfloat16 without a numpy dtype
            arr = np.asarray(arr.tolist(), dtype=np.float32)
    arr = arr.reshape(-1)
    if not arr.flags["C_CONTIGUOUS"]:
        arr = np.ascontiguousarray(arr, dtype=np.float32)
    return arr


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

#: What to fetch from the model repo.  Deliberately excludes "*.pth": the
#: Kokoro repos ship a PyTorch checkpoint next to the safetensors, and only the
#: safetensors are ever read here.
MODEL_ALLOW_PATTERNS = ["*.json", "*.safetensors"]


class Engine:
    """Kokoro synthesis engine.

    `load()` is blocking and idempotent; `synth()` is serialized (the pipeline is
    not reentrant) and never raises on ordinary text.
    """

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        lang_code: str = "a",
        repo_id: str = DEFAULT_REPO_ID,
    ) -> None:
        self.voice = voice
        self.speed = float(speed)
        self.lang_code = lang_code
        self.repo_id = repo_id
        self.sample_rate = SAMPLE_RATE
        self.last_error: str | None = None
        self._model: Any = None
        self._pipeline: Any = None
        self._load_lock = threading.Lock()
        self._closed = False
        self._synth_lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def loaded(self) -> bool:
        return self._pipeline is not None

    def _build_pipeline(self) -> tuple[Any, Any]:
        """Construct (model, pipeline). Overridable so tests can avoid the model."""
        # Imported here, not at module import time: pulling in KokoroPipeline
        # drags in spaCy/torch and costs seconds.
        from mlx_audio.tts.models.kokoro import KokoroPipeline
        from mlx_audio.tts.utils import load_model

        # mlx-audio's DEFAULT_ALLOW_PATTERNS includes "*.pth", which drags down
        # kokoro-v1_0.pth -- a 327 MB PyTorch checkpoint that an MLX build never
        # reads, alongside the 283 MB safetensors it actually uses.  Naming the
        # patterns ourselves roughly halves the first-run download.  The
        # parameter is forwarded through load_model's **kwargs to
        # mlx_audio.utils.get_model_path; fnmatch's "*" spans "/", so
        # "*.safetensors" still picks up voices/<name>.safetensors.
        try:
            model = load_model(self.repo_id, allow_patterns=MODEL_ALLOW_PATTERNS)
        except TypeError:
            # A future mlx-audio that stops forwarding the kwarg should cost a
            # bigger download, not a broken reader.
            model = load_model(self.repo_id)
        pipeline = KokoroPipeline(
            lang_code=self.lang_code, model=model, repo_id=self.repo_id
        )
        return model, pipeline

    def load(self) -> None:
        """Build the model + pipeline (~3.5s). Blocking, thread-safe, idempotent.

        Safe to call from several threads: the first caller does the work, the
        rest block until it finishes and then return.  A failed load leaves the
        engine unloaded (and raises) so a later call can retry.
        """
        if self._pipeline is not None:
            return
        with self._load_lock:
            if self._pipeline is not None or self._closed:
                return
            model, pipeline = self._build_pipeline()
            if self._closed:
                # close() ran while we were building; drop the result on the
                # floor rather than resurrecting a released engine.
                return
            # Publish only after both succeeded, so `loaded` is never half true.
            self._model = model
            self._pipeline = pipeline
            self.last_error = None

    def close(self) -> None:
        """Drop the model/pipeline references. Safe to call more than once.

        Never blocks.  ``load()`` holds ``_load_lock`` for its whole ~3.5s
        duration, so waiting for it here would pin process exit to the end of a
        load that nobody wants any more.  Instead we set ``_closed`` (which
        ``load()`` re-checks before publishing) and only clear the references if
        the lock happens to be free.
        """
        self._closed = True
        if not self._load_lock.acquire(blocking=False):
            return  # a load is in flight; it will see _closed and discard itself
        try:
            self._pipeline = None
            self._model = None
        finally:
            self._load_lock.release()

    def voices(self) -> list[str]:
        """Voice names this engine's repo/lang can use."""
        return list_voices(self.repo_id, self.lang_code)

    # -- synthesis ---------------------------------------------------------

    def _run_pipeline(self, text: str) -> tuple[list[np.ndarray], list[tuple[list, float]]]:
        parts: list[np.ndarray] = []
        feed: list[tuple[list, float]] = []
        gen = self._pipeline(
            text, voice=self.voice, speed=self.speed, split_pattern=None
        )
        for result in gen:
            audio = to_mono_f32(getattr(result, "audio", None))
            parts.append(audio)
            tokens = list(getattr(result, "tokens", None) or ())
            feed.append((tokens, audio.shape[0] / float(self.sample_rate)))
        return parts, feed

    def synth(self, chunk: "Chunk") -> Spoken:
        """Synthesize one chunk and align its words to the audio.

        Never raises on ordinary text.  A pipeline failure yields empty audio and
        sets `self.last_error`; an alignment failure yields correct audio with
        evenly distributed (degraded) timings.
        """
        chunk_idx = int(getattr(chunk, "idx", 0) or 0)
        text = getattr(chunk, "text", "") or ""
        offsets = list(getattr(chunk, "offsets", None) or ())
        words = getattr(chunk, "words", None)
        nslots = len(words) if words is not None else len(offsets)
        # `offsets[i]` pairs with `words[i]`; tolerate a short/long offsets list.
        if len(offsets) < nslots:
            offsets = offsets + [len(text)] * (nslots - len(offsets))
        elif len(offsets) > nslots:
            offsets = offsets[:nslots]
        spans = word_spans(text, offsets, getattr(chunk, "word_texts", None))

        if not text.strip():
            return Spoken(
                chunk_idx=chunk_idx,
                audio=np.zeros(0, dtype=np.float32),
                timings=even_timings(spans, 0.0, nslots),
                sample_rate=self.sample_rate,
            )

        parts: list[np.ndarray] = []
        feed: list[tuple[list, float]] = []
        with self._synth_lock:
            try:
                if self._pipeline is None:
                    self.load()
                parts, feed = self._run_pipeline(text)
                self.last_error = None
            except BaseException as exc:  # noqa: BLE001 - the reader must survive
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.error("synthesis failed for chunk %s: %s", chunk_idx,
                          self.last_error, exc_info=True)

        audio = (
            np.concatenate(parts)
            if parts
            else np.zeros(0, dtype=np.float32)
        )
        if audio.dtype != np.float32:
            audio = audio.astype(np.float32)
        duration = audio.shape[0] / float(self.sample_rate)

        timings = build_timings(text, spans, feed, duration, nslots)
        if getattr(chunk, "kind", None) == "cell":
            # Only cells: every other chunk keeps Kokoro's padding, which is
            # the breath between paragraphs.
            tail = ROW_END_TAIL if getattr(chunk, "row_end", False) else CELL_TAIL
            try:
                audio, timings = trim_silence(
                    audio, timings, self.sample_rate, lead=CELL_LEAD, tail=tail
                )
            except Exception:  # noqa: BLE001 - padding is cosmetic
                log.warning("silence trim failed; keeping the padded audio",
                            exc_info=True)
        return Spoken(
            chunk_idx=chunk_idx,
            audio=audio,
            timings=timings,
            sample_rate=self.sample_rate,
        )
