"""Tests for readaloud.player.

These drive the REAL CoreAudio device, because the whole point of the module is
the timing behaviour of a live PortAudio stream -- a mock would assert nothing.
Every buffer is pure digital silence (`np.zeros`), so the suite is inaudible however
long the buffers are -- and they ARE sized from the device's reported output
latency, because a Bluetooth sink can lead the DAC by 0.7 s.

Runs under pytest, and also standalone (pytest is not currently a project dep):

    uv run python tests/test_player.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from readaloud.player import (  # noqa: E402
    BLOCKSIZE,
    SAMPLE_RATE,
    DeviceInfo,
    Player,
    PlayerError,
    format_devices,
    list_devices,
    resolve_device,
)

BLOCK_S = BLOCKSIZE / SAMPLE_RATE  # 0.02133 s


def _output_latency() -> float:
    """Reported DAC lead of the default output device.

    Built-in speakers report ~0.036 s; a Bluetooth sink can report 0.7 s.  Every
    timing assertion here is about *audible* time, so it has to know this number:
    a bare `sleep(0.06); assert position > 0` only passes on a low-latency device,
    because on a Bluetooth sink nothing is audible yet at 60 ms.
    """
    try:
        import sounddevice as sd

        st = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1,
                             blocksize=BLOCKSIZE, latency="low")
        st.start()
        try:
            return float(st.latency)
        finally:
            st.stop()
            st.close()
    except Exception:  # noqa: BLE001 - no device: fall back to "no lead"
        return 0.0


LATENCY = _output_latency()


def long_enough(seconds: float) -> float:
    """A buffer with audible time left over after the DAC lead."""
    return max(seconds, 2 * LATENCY + seconds)


def audible(p: Player, target: float, extra: float = 1.5) -> float:
    """Block until `target` seconds are audible (or the buffer drains)."""
    deadline = time.perf_counter() + LATENCY + target + extra
    while time.perf_counter() < deadline:
        pos = p.position
        if pos >= target or p.finished:
            return pos
        time.sleep(0.004)
    return p.position


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@dataclass
class FakeSpoken:
    """Stand-in for readaloud.speech.Spoken (owned by another agent, may not exist
    yet).  player.py only duck-types `.audio`, `.sample_rate` and `.chunk_idx`."""

    chunk_idx: int
    audio: np.ndarray
    timings: list = field(default_factory=list)
    sample_rate: int = SAMPLE_RATE


def silence(seconds: float, chunk_idx: int = 0) -> FakeSpoken:
    n = int(round(seconds * SAMPLE_RATE))
    return FakeSpoken(chunk_idx=chunk_idx, audio=np.zeros(n, dtype=np.float32))


def raw_position(p: Player) -> float:
    """The uncompensated frame counter -- what `position` would report if it did
    NOT subtract the output latency.  White-box, used only to prove that the
    compensation is actually happening and in the right direction."""
    tr = p._track  # noqa: SLF001
    return 0.0 if tr is None else tr.pos * tr.inv_sr


def fd_count() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return -1


def note(msg: str) -> None:
    """Print a measured number so the run is self-documenting."""
    print("      " + msg)


# --------------------------------------------------------------------------- #
# device discovery
# --------------------------------------------------------------------------- #


def test_list_devices():
    devs = list_devices(output_only=True)
    assert devs, "no output devices found"
    assert all(isinstance(d, DeviceInfo) for d in devs)
    assert all(d.max_output_channels > 0 for d in devs)
    assert sum(1 for d in devs if d.is_default_output) <= 1

    everything = list_devices(output_only=False)
    assert len(everything) >= len(devs)

    text = format_devices()
    assert "available output devices" in text
    assert devs[0].name in text
    note(f"{len(devs)} output device(s); default = "
         f"{next((d.name for d in devs if d.is_default_output), '?')}")


def test_resolve_device():
    assert resolve_device(None) is None
    assert resolve_device("") is None
    assert resolve_device("  ") is None
    assert resolve_device("default") is None
    assert resolve_device("DEFAULT") is None

    devs = list_devices(output_only=True)
    target = next((d for d in devs if d.is_default_output), devs[0])

    assert resolve_device(target.index) == target.index
    assert resolve_device(str(target.index)) == target.index
    assert resolve_device(target.name) == target.index
    assert resolve_device(target.name.upper()) == target.index

    word = target.name.split()[-1]
    if sum(1 for d in devs if word.lower() in d.name.lower()) == 1:
        assert resolve_device(word) == target.index
        note(f"substring {word!r} -> device {target.index}")

    for bad in ("no-such-audio-device-xyz", 9999, "9999"):
        try:
            resolve_device(bad)
        except PlayerError as exc:
            assert "device" in str(exc).lower()
        else:
            raise AssertionError(f"resolve_device({bad!r}) should have raised")

    # an input-only device is not a legal --device value
    inputs_only = [d for d in list_devices(output_only=False)
                   if d.max_output_channels == 0]
    if inputs_only:
        try:
            resolve_device(inputs_only[0].index)
        except PlayerError:
            note(f"input-only device {inputs_only[0].index} correctly rejected")
        else:
            raise AssertionError("input-only device should not resolve")


def test_bad_device_raises_player_error_at_construction():
    for bad in (9999, "no-such-audio-device-xyz"):
        try:
            Player(device=bad)
        except PlayerError as exc:
            assert str(exc)  # human readable, not an empty PortAudio code
        else:
            raise AssertionError(f"Player(device={bad!r}) should have raised")
    note("PlayerError raised at construction for missing devices")


def test_construct_opens_and_close_releases():
    before = fd_count()
    p = Player()
    try:
        assert p.running
        assert p.latency > 0.0
        assert not p.playing
        assert not p.finished
        assert p.position == 0.0
        assert p.duration == 0.0
        assert p.chunk_idx is None
        note(f"stream.latency = {p.latency:.6f}s (real DAC lead is ~0.0163s)")
    finally:
        p.close()
    assert not p.running
    p.close()  # idempotent
    after = fd_count()
    assert abs(after - before) <= 1, f"fd leak: {before} -> {after}"


# --------------------------------------------------------------------------- #
# position: monotonicity and latency compensation
# --------------------------------------------------------------------------- #


def test_position_monotonic_and_latency_compensated():
    dur = long_enough(0.5)
    with Player() as p:
        samples: list[tuple[float, float, float]] = []
        p.play(silence(dur, chunk_idx=3))
        t0 = time.perf_counter()
        while True:
            elapsed = time.perf_counter() - t0
            pos = p.position
            samples.append((elapsed, pos, raw_position(p)))
            if p.finished or elapsed > dur + 0.35 + LATENCY:
                break
            time.sleep(0.004)

        assert p.finished, "buffer never drained"
        assert p.chunk_idx == 3
        assert abs(p.duration - dur) < 1e-6
        assert abs(p.position - dur) < 1e-9  # exactly the duration once drained

        # 1. monotonic
        worst_back = 0.0
        for (_, a, _), (_, b, _) in zip(samples, samples[1:]):
            worst_back = max(worst_back, a - b)
        assert worst_back <= 2e-3, f"position went backwards by {worst_back:.6f}s"

        # 2. you cannot hear the future: position never leads wall clock
        lead = max(pos - el for el, pos, _ in samples)
        assert lead <= 0.010, f"position ran {lead:.6f}s ahead of wall clock"

        # 3. ... and it is not wildly behind either
        mid = [(el, pos) for el, pos, _ in samples
               if LATENCY + 0.10 < el < dur - 0.05]
        assert mid, "no mid-track samples"
        lag = max(el - pos for el, pos in mid)
        assert lag <= 0.100 + LATENCY, f"position lagged wall clock by {lag:.6f}s"

        # 4. the compensation really happens: the reported position trails the
        #    raw frame counter by roughly one DAC lead, never leads it.
        deltas = [rawp - pos for el, pos, rawp in samples
                  if LATENCY + 0.10 < el < dur - 0.05]
        assert deltas
        assert min(deltas) >= -2e-3, f"position ahead of the frame counter by {-min(deltas):.6f}s"
        assert max(deltas) <= 0.080 + LATENCY, (
            f"compensation of {max(deltas):.6f}s is too large")
        note(f"n={len(samples)} worst backstep={worst_back*1e3:.3f}ms "
             f"max lead={lead*1e3:.3f}ms max lag={lag*1e3:.3f}ms")
        note(f"latency compensation (raw frames - position): "
             f"min={min(deltas)*1e3:.3f}ms mean={sum(deltas)/len(deltas)*1e3:.3f}ms "
             f"max={max(deltas)*1e3:.3f}ms")
        assert p.glitches == 0, f"{p.glitches} dropouts during a quiet 0.5s play"


def test_position_clamped_and_zero_without_a_track():
    with Player() as p:
        assert p.position == 0.0
        p.play(silence(0.2))
        time.sleep(0.05)
        assert 0.0 <= p.position <= p.duration
        p.stop()
        assert p.position == 0.0
        assert p.duration == 0.0
    note("position is 0.0 with no active buffer, clamped to [0, duration] with one")


# --------------------------------------------------------------------------- #
# swapping
# --------------------------------------------------------------------------- #


def test_rapid_swap():
    with Player() as p:
        clips = [silence(0.4, chunk_idx=i) for i in range(20)]

        # back-to-back with zero delay: the worst case for the handover
        for c in clips:
            p.play(c)
            assert p.chunk_idx == c.chunk_idx
            assert p.position <= 0.005
        time.sleep(0.05)
        assert p.chunk_idx == 19
        assert p.playing

        # and spaced out, so each swap really does interrupt a running callback
        for c in clips:
            p.play(c)
            time.sleep(BLOCK_S * 1.5)
            assert p.chunk_idx == c.chunk_idx
            assert p.playing
        p.stop()
        assert p.glitches == 0, f"{p.glitches} dropouts across 40 swaps"
        note(f"40 swaps (20 zero-delay + 20 spaced), glitches={p.glitches}")


def test_swap_resets_position_and_finished():
    with Player() as p:
        p.play(silence(0.12, chunk_idx=1))
        assert p.wait_finished(2.0)
        time.sleep(0.03)
        assert p.finished
        p.play(silence(0.3, chunk_idx=2))
        assert not p.finished
        assert p.chunk_idx == 2
        assert p.position < 0.01
        p.stop()
    note("a new buffer clears `finished` and resets `position`")


# --------------------------------------------------------------------------- #
# pause / resume / toggle
# --------------------------------------------------------------------------- #


def test_pause_resume():
    with Player() as p:
        p.play(silence(long_enough(0.6)))
        audible(p, 0.10)

        t_pause = time.perf_counter()
        p.pause()
        pause_cost = time.perf_counter() - t_pause
        assert pause_cost < 0.005, f"pause() took {pause_cost:.6f}s (must not stop the stream)"
        assert not p.playing
        assert p.paused

        time.sleep(0.03)          # let any in-flight callback retire
        frozen = p.position
        time.sleep(0.15)
        still = p.position
        assert abs(still - frozen) < 1e-9, f"position moved while paused: {frozen} -> {still}"
        assert frozen > 0.05, "nothing played before the pause"

        p.resume()
        assert p.playing
        assert not p.paused
        after_resume = p.position
        assert after_resume >= frozen - 0.040, (
            f"resume jumped backwards {frozen - after_resume:.6f}s")
        audible(p, frozen + 0.06)
        moved = p.position
        assert moved > frozen + 0.05, f"position did not advance after resume ({frozen} -> {moved})"
        p.stop()
        note(f"pause() cost {pause_cost*1e6:.1f}us; frozen at {frozen:.6f}s; "
             f"resume backstep {max(0.0, frozen - after_resume)*1e3:.3f}ms")
        assert p.glitches == 0


def test_toggle():
    with Player() as p:
        p.play(silence(0.4))
        assert p.playing
        assert p.toggle() is False
        assert p.paused
        assert p.toggle() is True
        assert not p.paused
        p.stop()
    note("toggle() returns the new `playing` value")


def test_pause_survives_a_full_drain():
    """Pausing near the end must not lose the finished signal when we resume."""
    with Player() as p:
        p.play(silence(0.15))
        time.sleep(0.05)
        p.pause()
        time.sleep(0.05)
        assert not p.finished
        p.resume()
        assert p.wait_finished(2.0)
        time.sleep(0.03)
        assert p.finished
        assert abs(p.position - p.duration) < 1e-9
    note("paused mid-buffer, resumed, drained to exactly duration")


# --------------------------------------------------------------------------- #
# seeking
# --------------------------------------------------------------------------- #


def test_seek():
    dur = 0.5
    with Player() as p:
        p.play(silence(dur, chunk_idx=7))
        time.sleep(0.05)
        before = p.glitches

        for target in (0.30, 0.05, 0.4999, 0.0):
            p.seek(target)
            got = p.position
            assert abs(got - target) <= 0.030, f"seek({target}) -> position {got}"
            assert p.chunk_idx == 7
            assert not p.finished
            time.sleep(0.02)

        p.seek(10.0)               # past the end -> clamps to the duration
        assert abs(p.position - dur) <= 0.001
        p.seek(-5.0)               # before the start -> clamps to 0
        assert p.position <= 0.005

        assert p.glitches == before, "seeking caused a dropout"
        p.stop()
    note("seek is exact within 30ms, clamps at both ends, causes no dropouts")


def test_seek_preserves_pause():
    with Player() as p:
        p.play(silence(long_enough(0.6)))
        audible(p, 0.02)
        p.pause()
        time.sleep(0.03)
        p.seek(0.25)
        assert p.paused, "seek() resumed a paused player"
        assert abs(p.position - 0.25) < 1e-6
        time.sleep(0.08)
        assert abs(p.position - 0.25) < 1e-6, "paused seek started playing"
        p.resume()
        audible(p, 0.29)
        assert p.position > 0.28
        p.stop()
    note("seek while paused stays paused and lands exactly on target")


def test_seek_without_a_track_is_a_noop():
    with Player() as p:
        p.seek(1.0)          # no active buffer
        assert p.position == 0.0
        assert not p.playing
    note("seek() with nothing loaded is a no-op")


# --------------------------------------------------------------------------- #
# finished / on_finished
# --------------------------------------------------------------------------- #


def test_finished_and_on_finished():
    calls: list[str] = []
    evt = threading.Event()

    def on_finished() -> None:
        calls.append(threading.current_thread().name)
        evt.set()

    with Player() as p:
        p.set_on_finished(on_finished)
        assert not p.finished

        p.play(silence(0.15, chunk_idx=11))
        assert not p.finished
        assert evt.wait(2.0), "on_finished never fired"
        time.sleep(0.10)  # would catch a duplicate fire

        assert len(calls) == 1, f"on_finished fired {len(calls)} times"
        assert calls[0] != threading.current_thread().name
        assert p.finished
        assert not p.playing
        assert abs(p.position - p.duration) < 1e-9
        assert p.chunk_idx == 11
        note(f"on_finished fired once, from thread {calls[0]!r}")


def test_raising_on_finished_does_not_kill_the_stream():
    fired = threading.Event()

    def boom() -> None:
        fired.set()
        raise ValueError("deliberate")

    with Player() as p:
        p.set_on_finished(boom)
        p.play(silence(0.12))
        assert fired.wait(2.0)
        time.sleep(0.05)

        p.set_on_finished(None)
        p.play(silence(long_enough(0.4)))
        assert audible(p, 0.06) > 0.05, "the stream died after a raising callback"
        p.stop()
    note("an exception inside on_finished is swallowed; the stream keeps running")


def test_stop_does_not_report_finished():
    with Player() as p:
        p.play(silence(0.5))
        time.sleep(0.05)
        p.stop()
        assert not p.playing
        assert not p.finished, "stop() must not look like a drained buffer"
        assert p.position == 0.0
        assert p.spoken is None
        p.play(silence(0.2, chunk_idx=4))
        assert p.playing
        assert p.chunk_idx == 4
        p.stop()
    note("stop() clears the buffer without setting `finished` (no auto-advance)")


def test_empty_buffer_finishes_immediately():
    evt = threading.Event()
    with Player() as p:
        p.set_on_finished(evt.set)
        p.play(FakeSpoken(chunk_idx=0, audio=np.zeros(0, dtype=np.float32)))
        assert p.wait_finished(1.0), "zero-length buffer never finished"
        assert evt.wait(1.0), "zero-length buffer never fired on_finished"
        assert p.finished
        assert p.duration == 0.0
        assert p.position == 0.0
    note("a zero-length Spoken drains and fires on_finished within one block")


# --------------------------------------------------------------------------- #
# lifecycle churn
# --------------------------------------------------------------------------- #


def test_play_stop_cycles():
    with Player() as p:
        t0 = time.perf_counter()
        for i in range(30):
            p.play(silence(0.2, chunk_idx=i))
            time.sleep(0.005)
            assert p.playing
            p.stop()
            assert not p.playing
        elapsed = time.perf_counter() - t0
        assert p.glitches == 0, f"{p.glitches} dropouts across 30 play/stop cycles"
        note(f"30 play/stop cycles in {elapsed:.3f}s, glitches={p.glitches}")


def test_close_start_cycles_and_no_fd_leak():
    before = fd_count()
    p = Player()
    try:
        for _ in range(8):
            p.close()
            assert not p.running
            p.start()
            assert p.running
            p.play(silence(0.1, chunk_idx=1))
            assert audible(p, 0.05) > 0.0
            p.stop()
        p.start()  # idempotent
    finally:
        p.close()
    after = fd_count()
    assert abs(after - before) <= 1, f"fd leak across 8 close/start cycles: {before} -> {after}"
    note(f"8 close/start cycles, fds {before} -> {after}")


def test_play_after_close_restarts_the_stream():
    p = Player()
    p.close()
    assert not p.running
    try:
        p.play(silence(0.15, chunk_idx=2))
        assert p.running
        assert audible(p, 0.05) > 0.0
    finally:
        p.close()
    note("play() reopens the stream if it was closed")


def test_context_manager():
    with Player() as p:
        assert p.running
        assert isinstance(p, Player)
    assert not p.running
    note("Player works as a context manager")


def test_sample_rate_mismatch_raises_player_error():
    with Player() as p:
        bad = FakeSpoken(chunk_idx=0, audio=np.zeros(100, dtype=np.float32),
                         sample_rate=48000)
        try:
            p.play(bad)
        except PlayerError as exc:
            assert "48000" in str(exc)
        else:
            raise AssertionError("a sample-rate mismatch should raise PlayerError")
    note("a sample-rate mismatch raises a catchable PlayerError, not silence at 2x")


def test_non_contiguous_and_2d_audio_is_accepted():
    with Player() as p:
        two_d = np.zeros((1, int(0.15 * SAMPLE_RATE)), dtype=np.float32)
        p.play(FakeSpoken(chunk_idx=0, audio=two_d))
        assert abs(p.duration - 0.15) < 1e-6
        assert p.wait_finished(2.0)

        strided = np.zeros(int(0.3 * SAMPLE_RATE), dtype=np.float64)[::2]
        p.play(FakeSpoken(chunk_idx=1, audio=strided))
        assert abs(p.duration - 0.15) < 1e-6
        assert p.wait_finished(2.0)
    note("(1, N) and non-contiguous / float64 input are normalised before publishing")


# --------------------------------------------------------------------------- #
# standalone runner (pytest is not a dependency of this project)
# --------------------------------------------------------------------------- #


def _main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    tests.sort(key=lambda nf: nf[1].__code__.co_firstlineno)
    failed: list[str] = []
    t0 = time.perf_counter()
    for name, fn in tests:
        print(f"  {name} ...", flush=True)
        try:
            fn()
        except BaseException:
            failed.append(name)
            print(f"  FAIL {name}")
            traceback.print_exc()
        else:
            print(f"  ok   {name}")
    dt = time.perf_counter() - t0
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed in {dt:.2f}s")
    if failed:
        print("failed: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
