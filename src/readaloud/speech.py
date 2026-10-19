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

The word timings (`align_words`, `word_spans`, `even_timings`, the clamping in
`build_timings`) are computed by :mod:`readaloud._core` (``rust/src/align.rs``);
this module flattens misaki's tokens into tuples for it.  The token -> word-slot
alignment is the version proven by the token-align spike against
mlx-community/Kokoro-82M-4bit + misaki 0.9.4 over 16 texts (prose, code,
markdown tables, URLs, emoji, accents, NBSP, empty input), single- and
multi-Result, with zero monotonicity or range violations.
"""

from __future__ import annotations

import logging
import os
import threading
from bisect import bisect_right
from dataclasses import dataclass
from operator import attrgetter
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import numpy as np

from . import _core

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
        i = bisect_right(self.timings, seconds, key=attrgetter("start"))
        return self.timings[i - 1].word_slot if i else None


# --------------------------------------------------------------------------
# token -> word-slot alignment  (verified by the token-align spike)
# --------------------------------------------------------------------------


def _locate(text: str, needle: str, cursor: int) -> tuple[int, int] | None:
    """Find `needle`'s char span in `text` at/after `cursor`, tolerating the ways
    misaki mangles token text (absorbed leading whitespace, dropped quotes)."""
    return _core.locate(text, needle, cursor)


def _token(tok: Any) -> tuple[str, float | None, float | None]:
    """A misaki MToken as `(text, start_ts, end_ts)`."""
    ts = getattr(tok, "start_ts", None)
    te = getattr(tok, "end_ts", None)
    return (
        getattr(tok, "text", None) or "",
        None if ts is None else float(ts),
        None if te is None else float(te),
    )


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
    spans = [(a, b) for a, b in spans]
    if not spans:
        return []
    flat = [
        ([_token(tok) for tok in tokens or () if getattr(tok, "text", None)],
         float(duration))
        for tokens, duration in results
    ]
    return _core.align_words(text, spans, flat)


# --------------------------------------------------------------------------
# word spans, degraded timings, validation
# --------------------------------------------------------------------------


def _offset(value: Any, n: int) -> int | None:
    """A word's start offset clamped into `text`, or None when it is not a number."""
    try:
        return max(0, min(n, int(value)))
    except (TypeError, ValueError):
        return None


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
    texts = None if word_texts is None else [w or "" for w in word_texts]
    return _core.word_spans(text, [_offset(o, n) for o in offsets], texts)


def even_timings(
    spans: Sequence[tuple[int, int]], duration: float, nslots: int | None = None
) -> list[Timed]:
    """Degraded fallback: spread `duration` over the slots by character width.

    Used when real alignment is impossible or produced nonsense.  Reading is
    still usable (the highlight sweeps at roughly the right pace); only the
    per-word precision is lost.
    """
    n = len(spans) if nslots is None else int(nslots)
    widths = [(a, b) for a, b in spans[:max(0, n)]]
    return [Timed(*t) for t in _core.even_timings(widths, float(duration), n)]


def _sanitize(timings: list[Timed], nslots: int, duration: float) -> list[Timed]:
    """Clamp into [0, duration] and enforce the contract's monotonicity.

    Raises ValueError when the alignment is structurally wrong (wrong number of
    slots, wrong slot ids), which is the signal to fall back to `even_timings`.
    """
    raw = [(int(t.word_slot), float(t.start), float(t.end)) for t in timings]
    return [Timed(*t) for t in _core.sanitize(raw, nslots, float(duration))]


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
