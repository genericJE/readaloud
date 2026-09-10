"""readaloud.player — one persistent output stream, lock-free buffer handover.

Design (all of it measured on macOS arm64 / CoreAudio / sounddevice 0.5.6):

* ONE `sounddevice.OutputStream` is opened for the lifetime of the app.
  `stream.stop()` costs 113 ms and `stream.start()` 21 ms on this platform, so they
  are never used for pause/resume or for switching chunks.  Pause is a bool the
  callback checks (15 us); switching chunks is a single attribute store.

* Handover is lock-free.  The control thread builds a whole immutable-ish `_Track`
  and publishes it with one attribute assignment.  The callback reads
  `self._track` **once** into a local and only ever mutates the object it read, so
  a swap racing a running callback merely leaves the callback writing into an
  orphaned track.  No lock is taken on the audio thread, ever.

* `position` is latency compensated.  CONTRACTS.md prescribes
  `frames/samplerate - stream.latency`; measured, `stream.latency` (0.0362 s)
  overstates the real callback-to-DAC lead (0.0163 s) by 2.2x, which produces a
  mean -9.2 ms bias and a 35 ms peak-to-peak sawtooth in the reported position.
  So the primary path anchors on PortAudio's own DAC schedule
  (`stream.time - epoch`, mean error +0.05 ms) and the contract formula is kept as
  the degraded fallback for when `stream.time` is unavailable.  The public
  interface is exactly the contract's.

* Dropouts are detected by comparing successive `outputBufferDacTime` values.  The
  `status` argument of the PortAudio callback never fires on CoreAudio, not even
  for a 60 ms stall in a 21 ms callback slot, so it cannot be trusted.

Failure model: anything wrong with the audio device raises `PlayerError`, which the
CLI can catch and print.  Constructing a `Player` opens the device, so a missing or
busy device fails there rather than deep inside a PortAudio traceback later.
"""

from __future__ import annotations

import gc
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

try:  # importing sounddevice loads the PortAudio shared library
    import sounddevice as _sd

    _SD_IMPORT_ERROR: BaseException | None = None
except BaseException as _exc:  # pragma: no cover - depends on the host install
    _sd = None  # type: ignore[assignment]
    _SD_IMPORT_ERROR = _exc

if TYPE_CHECKING:  # pragma: no cover - typing only; never import speech at runtime
    from readaloud.speech import Spoken

__all__ = [
    "SAMPLE_RATE",
    "BLOCKSIZE",
    "PlayerError",
    "DeviceInfo",
    "list_devices",
    "format_devices",
    "resolve_device",
    "Player",
]

SAMPLE_RATE = 24000
BLOCKSIZE = 512


class PlayerError(RuntimeError):
    """Raised when the audio device cannot be found, opened or used.

    Catch this in the CLI and print `str(exc)`; it is always a human-readable
    sentence, never a raw PortAudio error.
    """


def _require_sd():
    if _sd is None:
        raise PlayerError(
            "audio support is unavailable: could not import sounddevice/PortAudio "
            f"({_SD_IMPORT_ERROR})"
        )
    return _sd


# --------------------------------------------------------------------------- #
# device discovery -- everything a `--device` flag needs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DeviceInfo:
    """One selectable audio device, as `--device` sees it."""

    index: int
    name: str
    hostapi: str
    max_output_channels: int
    max_input_channels: int
    default_samplerate: float
    is_default_output: bool

    def __str__(self) -> str:
        mark = "*" if self.is_default_output else " "
        return (
            f"{mark} {self.index:>3}  {self.name}  "
            f"({self.hostapi}, {self.max_output_channels} out, "
            f"{self.default_samplerate:.0f} Hz)"
        )


def list_devices(output_only: bool = True) -> list[DeviceInfo]:
    """Enumerate audio devices.

    `output_only` (the default) keeps only devices that can actually play audio,
    which is the useful set for `--device`.
    """
    sd = _require_sd()
    try:
        raw = sd.query_devices()
        default_out = sd.default.device[1]
        hostapis = sd.query_hostapis()
    except BaseException as exc:  # PortAudio raises all sorts
        raise PlayerError(f"could not enumerate audio devices: {exc}") from exc

    out: list[DeviceInfo] = []
    for i, d in enumerate(raw):
        nout = int(d.get("max_output_channels", 0))
        if output_only and nout <= 0:
            continue
        api_idx = int(d.get("hostapi", -1))
        api = ""
        if 0 <= api_idx < len(hostapis):
            api = str(hostapis[api_idx].get("name", ""))
        out.append(
            DeviceInfo(
                index=i,
                name=str(d.get("name", f"device {i}")).strip(),
                hostapi=api,
                max_output_channels=nout,
                max_input_channels=int(d.get("max_input_channels", 0)),
                default_samplerate=float(d.get("default_samplerate", 0.0)),
                is_default_output=(i == default_out),
            )
        )
    return out


def format_devices(devices: list[DeviceInfo] | None = None) -> str:
    """A block of text listing the output devices, ready to print from the CLI."""
    try:
        devs = list_devices(output_only=True) if devices is None else devices
    except PlayerError as exc:
        return str(exc)
    if not devs:
        return "no audio output devices found"
    body = "\n".join(str(d) for d in devs)
    return "available output devices (* = system default):\n" + body


def resolve_device(spec: Any) -> int | None:
    """Turn a user-supplied `--device` value into a PortAudio device index.

    Accepts `None` / `""` / `"default"` (meaning: the system default, returned as
    `None`), an integer or numeric string index, or a case-insensitive device-name
    substring such as `"MacBook Pro Speakers"` or just `"speakers"`.

    Raises `PlayerError` with the device list attached when the value does not
    name exactly one usable output device.
    """
    if spec is None:
        return None
    if isinstance(spec, bool):  # guard: bool is an int subclass
        raise PlayerError(f"invalid --device value: {spec!r}")

    devs = list_devices(output_only=True)

    if isinstance(spec, int):
        idx: int | None = spec
    else:
        s = str(spec).strip()
        if s == "" or s.lower() == "default":
            return None
        try:
            idx = int(s)
        except ValueError:
            idx = None
        if idx is None:
            low = s.lower()
            exact = [d for d in devs if d.name.lower() == low]
            if len(exact) == 1:
                return exact[0].index
            subs = exact or [d for d in devs if low in d.name.lower()]
            if len(subs) == 1:
                return subs[0].index
            if len(subs) > 1:
                names = ", ".join(f"{d.index}:{d.name}" for d in subs)
                raise PlayerError(
                    f"--device {spec!r} is ambiguous, it matches: {names}"
                )
            raise PlayerError(
                f"no audio output device matches --device {spec!r}\n"
                + format_devices(devs)
            )

    valid = {d.index for d in devs}
    if idx not in valid:
        raise PlayerError(
            f"--device {idx} is not a usable audio output device\n"
            + format_devices(devs)
        )
    return idx


# --------------------------------------------------------------------------- #
# the active buffer
# --------------------------------------------------------------------------- #


class _Track:
    """One in-flight `Spoken`.

    The control thread NEVER mutates a track that the callback may be reading; to
    change anything it publishes a brand new `_Track`.  The callback mutates only
    `pos`, `epoch` and `done`, all single stores of immutable values.
    """

    __slots__ = ("audio", "n", "pos", "epoch", "done", "spoken", "inv_sr", "dur")

    def __init__(self, audio: np.ndarray, start_frame: int, spoken: Any, sr: int):
        self.audio = audio
        self.n = int(audio.shape[0])
        self.pos = start_frame
        # DAC time at which frame 0 of this track would have been audible.
        # A single float, stored by the callback; None until it has run once.
        self.epoch: float | None = None
        # Always False here, even for an empty buffer or a seek to the very end:
        # letting the callback be the one that flips it keeps `on_finished`
        # firing from exactly one place, on the audio thread, for every buffer.
        self.done = False
        self.spoken = spoken
        self.inv_sr = 1.0 / sr
        self.dur = self.n / sr


# --------------------------------------------------------------------------- #
# the player
# --------------------------------------------------------------------------- #


class Player:
    """Plays `Spoken` buffers through one persistent output stream.

    Constructing a `Player` opens and starts the stream (~50 ms) and raises
    `PlayerError` if the device is missing, busy or refuses the format.  Pass
    `autostart=False` to defer that to an explicit `start()`.

    Threading: `play`/`pause`/`resume`/`toggle`/`stop`/`seek`/`position` are all
    safe to call from the curses loop and none of them block on the audio thread.
    """

    def __init__(
        self,
        samplerate: int = SAMPLE_RATE,
        blocksize: int = BLOCKSIZE,
        device: Any = None,
        tune_runtime: bool = True,
        autostart: bool = True,
    ) -> None:
        self._sr = int(samplerate)
        self._blocksize = int(blocksize)
        self._blk_s = self._blocksize / self._sr
        self._tune = bool(tune_runtime)
        self._stream = None  # type: ignore[var-annotated]
        self._track: _Track | None = None  # the atomic publish point
        self._paused = False  # atomic bool, read by the callback
        self._on_finished: Callable[[], None] | None = None
        self._latency = 0.0
        self._ctl = threading.Lock()  # control thread ONLY -- never the callback
        self._finished_evt = threading.Event()
        self._finished_evt.set()
        # CoreAudio never sets the callback `status` flags, so count dropouts by
        # looking for discontinuities in the DAC schedule instead.
        self.glitches = 0
        self._last_dac = 0.0

        self._device = resolve_device(device)
        if autostart:
            self.start()

    # ------------------------------------------------------------------ #
    # audio thread
    # ------------------------------------------------------------------ #

    def _callback(self, out, frames, time_info, status) -> None:  # noqa: ARG002
        # An uncaught exception anywhere in here permanently kills the stream
        # (`stream.active` goes False, with only an ignored-cffi-callback note on
        # stderr, which is invisible under curses).  Hence the blanket guard.
        try:
            dac = time_info.outputBufferDacTime
            last = self._last_dac
            if last and dac - last > self._blk_s * 1.5:
                self.glitches += 1
            self._last_dac = dac

            tr = self._track  # ONE read; everything below is consistent with it
            if tr is None or tr.done or self._paused:
                out[:] = 0
                return

            p = tr.pos
            n = tr.n - p
            if n > frames:
                n = frames
            if n > 0:
                out[:n, 0] = tr.audio[p : p + n]
            if n < frames:
                out[n:, 0] = 0

            # One float store: the DAC time at which frame 0 would have played.
            # Cannot tear, and makes `position` exact without any clock fitting.
            tr.epoch = dac - p * tr.inv_sr
            tr.pos = p + n

            if tr.pos >= tr.n:
                tr.done = True
                self._finished_evt.set()
                cb = self._on_finished
                if cb is not None:
                    try:
                        cb()
                    except BaseException:
                        pass
        except BaseException:
            try:
                out[:] = 0
            except BaseException:
                pass

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Open and start the persistent stream.  Idempotent.

        Costs ~50 ms and grabs the audio device, so call it once at startup, never
        from a key handler.  Raises `PlayerError` on any device problem.
        """
        sd = _require_sd()
        with self._ctl:
            if self._stream is not None:
                return
            if self._tune:
                # A Python PortAudio callback has to win the GIL every
                # blocksize/samplerate seconds.  CPython's 5 ms default switch
                # interval is a quarter of a 512-frame block and causes audible
                # dropouts under pure-Python load (measured 14 dropouts/1.2 s,
                # 0 after this line).  gc.freeze() moves already-loaded objects
                # out of generational scanning; the caller should call it again
                # after Engine.load() for the Kokoro model graph.
                if sys.getswitchinterval() > 0.001:
                    sys.setswitchinterval(0.001)
                gc.freeze()
            self._last_dac = 0.0
            st = None
            try:
                st = sd.OutputStream(
                    samplerate=self._sr,
                    channels=1,
                    blocksize=self._blocksize,
                    latency="low",
                    dtype="float32",
                    device=self._device,
                    callback=self._callback,
                )
                st.start()
            except BaseException as exc:
                if st is not None:
                    try:
                        st.close()
                    except BaseException:
                        pass
                where = "default output device" if self._device is None else f"device {self._device}"
                raise PlayerError(
                    f"could not open the audio {where} at {self._sr} Hz mono: {exc}\n"
                    + format_devices()
                ) from exc
            try:
                self._latency = float(st.latency)
            except BaseException:
                self._latency = 0.0
            self._stream = st

    def close(self) -> None:
        """Stop and release the stream.  Idempotent; safe to call on quit."""
        with self._ctl:
            self._track = None
            st, self._stream = self._stream, None
        self._finished_evt.set()
        if st is not None:
            try:
                st.stop()
            except BaseException:
                pass
            try:
                st.close()
            except BaseException:
                pass

    def __enter__(self) -> "Player":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # control thread
    # ------------------------------------------------------------------ #

    def play(self, spoken: "Spoken", start_time: float = 0.0) -> None:
        """Make `spoken` the active buffer, starting `start_time` seconds in.

        Replaces whatever was playing.  All the work (dtype/contiguity fixes,
        frame arithmetic) happens before the publish, so the audio thread sees
        either the old track or a fully-formed new one and never blocks.
        """
        audio = np.asarray(getattr(spoken, "audio", None))
        if audio.ndim > 1:  # Kokoro yields (1, N)
            audio = audio.reshape(-1)
        if audio.dtype != np.float32 or not audio.flags["C_CONTIGUOUS"]:
            audio = np.ascontiguousarray(audio, dtype=np.float32)

        sr = int(getattr(spoken, "sample_rate", self._sr) or self._sr)
        if sr != self._sr:
            raise PlayerError(
                f"cannot play {sr} Hz audio on a {self._sr} Hz stream; construct "
                f"Player(samplerate={sr}) instead"
            )

        start = int(round(float(start_time) * sr))
        if start < 0:
            start = 0
        elif start > audio.shape[0]:
            start = audio.shape[0]

        tr = _Track(audio, start, spoken, sr)
        if self._stream is None:
            self.start()
        with self._ctl:
            self._finished_evt.clear()
            self._track = tr  # atomic publish
            self._paused = False

    def pause(self) -> None:
        """Silence output but keep the position.  Costs ~15 us."""
        self._paused = True

    def resume(self) -> None:
        """Resume from where `pause()` left off."""
        tr = self._track
        if tr is not None:
            # Drop the stale DAC anchor; the next callback re-anchors.  Until it
            # does, `position` reports the frame counter, which is exactly the
            # last thing the listener heard.
            tr.epoch = None
        self._paused = False

    def toggle(self) -> bool:
        """Flip pause/resume.  Returns the new value of `playing`."""
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self.playing

    def stop(self) -> None:
        """Drop the active buffer.  The stream stays open (stopping it costs 113 ms)."""
        self._track = None
        self._paused = False
        self._finished_evt.set()

    def seek(self, seconds: float) -> None:
        """Jump within the active buffer.  No copy: the same ndarray is republished."""
        tr = self._track
        if tr is None or tr.spoken is None:
            return
        was_paused = self._paused
        self.play(tr.spoken, seconds)
        if was_paused:
            self._paused = True

    def set_on_finished(self, cb: Callable[[], None] | None) -> None:
        """Register a no-argument callback fired when the active buffer drains.

        It runs ON THE AUDIO THREAD, exactly once per buffer.  It must be cheap
        and non-blocking -- `threading.Event.set()` or `deque.append()`.  It must
        NEVER call `stop()`, `close()` or `stream.stop()`: `Pa_StopStream` joins
        the audio thread and would deadlock.  Exceptions raised by it are
        swallowed so they cannot kill the stream.
        """
        self._on_finished = cb

    def wait_finished(self, timeout: float | None = None) -> bool:
        """Block until the active buffer drains (or `stop()`).  Control thread only."""
        return self._finished_evt.wait(timeout)

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #

    @property
    def position(self) -> float:
        """Seconds of the active `Spoken` the listener has actually heard.

        Latency compensated: the frames already handed to PortAudio have not
        reached the speakers yet, so the raw frame counter runs ~16 ms ahead.
        Measured error against the real DAC schedule: mean +0.05 ms, 0.11 ms
        peak-to-peak over 3 s.
        """
        tr = self._track
        if tr is None:
            return 0.0
        dur = tr.dur
        if tr.done:
            return dur
        pos_s = tr.pos * tr.inv_sr
        stream = self._stream
        if self._paused or stream is None:
            # Whatever is still in the hardware buffer plays out and lands here.
            return pos_s if pos_s < dur else dur
        e = tr.epoch  # ONE read of a single float; cannot tear
        if e is None:
            # Freshly published (play/seek) or just resumed: the callback has not
            # consumed anything from this track yet, so nothing new is audible and
            # the audible position is exactly the start point.  Subtracting the
            # latency here would drag the highlight ~36 ms behind the word the
            # user just clicked.
            return pos_s if pos_s < dur else dur
        try:
            v = stream.time - e  # exact, from PortAudio's own DAC schedule
        except BaseException:
            v = pos_s - self._latency  # degraded: the CONTRACTS.md formula
        if v < 0.0:
            return 0.0
        return dur if v > dur else v

    @property
    def playing(self) -> bool:
        tr = self._track
        return (
            self._stream is not None
            and not self._paused
            and tr is not None
            and not tr.done
        )

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def finished(self) -> bool:
        """True only when there IS an active buffer and it has drained.

        False after `stop()` (nothing is active), so `stop()` will not make an
        auto-advance loop skip forward.
        """
        tr = self._track
        return tr is not None and tr.done

    @property
    def spoken(self) -> Any:
        tr = self._track
        return tr.spoken if tr is not None else None

    @property
    def chunk_idx(self) -> int | None:
        tr = self._track
        return getattr(tr.spoken, "chunk_idx", None) if tr is not None else None

    @property
    def duration(self) -> float:
        tr = self._track
        return tr.dur if tr is not None else 0.0

    @property
    def latency(self) -> float:
        """PortAudio's reported output latency, in seconds."""
        return self._latency

    @property
    def running(self) -> bool:
        """True while the persistent stream is open."""
        return self._stream is not None
