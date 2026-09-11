"""The orchestrator: curses loop, prefetch worker, playback state machine.

Threading model — three threads, one direction of data flow:

* **main / curses thread** owns the `Screen`, the viewport (`top`), which chunk
  is current, and every user command.  It never blocks on synthesis.
* **prefetch thread** owns the `Engine`.  It loads the model once and then keeps
  the current chunk plus `--prefetch` more synthesized in a bounded cache.  The
  main thread only ever *reads* that cache (`Prefetcher.get`) and *publishes* a
  new "current" index (`Prefetcher.set_current`).
* **PortAudio callback thread** is inside `Player` and is never touched here;
  the main loop polls `player.position` / `player.finished` instead of using an
  audio-thread callback, per the audio-stream spike's warning that anything but
  `Event.set()` there risks a deadlock.

Nothing in this module blocks the curses loop for longer than the input poll.
"""

from __future__ import annotations

import gc
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Sequence

from .keys import Action, Command, Keymap, SCROLL_ACTIONS
from .ui import Screen, Status, Theme, screen_session

__all__ = ["Prefetcher", "App", "run", "POLL_MS", "MIN_SPEED", "MAX_SPEED",
           "FOLLOW_MARGIN", "FOLLOW_LEAD"]

#: how long the curses loop waits for a key before doing housekeeping
POLL_MS = 30

#: `[` / `]` bounds and step
MIN_SPEED = 0.5
MAX_SPEED = 3.0
SPEED_STEP = 0.1

#: how long a transient status message stays up
MESSAGE_TTL = 4.0

#: config-file warnings sit around longer: the user has to read a path
NOTICE_TTL = 12.0

#: rows of context follow-mode keeps above/below the spoken word
FOLLOW_MARGIN = 2

#: extra rows follow mode scrolls past the strict minimum, so the text about to
#: be spoken lands around the middle of the viewport instead of on the last row.
#: `~/.readaloud.conf`'s ``follow_lead`` overrides it; keep the two in step.
FOLLOW_LEAD = 20

#: don't let a pathological pattern build a million highlight spans
MAX_MATCHES = 5000

#: how much of a chunk Control Center gets as the "track title".  A chunk is a
#: whole paragraph; the lock screen shows one line.
MEDIA_TITLE_CHARS = 64

#: sentinel for "nothing has been published to Control Center yet"
_UNSET: Any = object()

#: Seconds to wait after handing the Now Playing role back before the process
#: exits.  Releasing it is an asynchronous XPC round trip to `nowplayingd`, and
#: a process that exits inside that window is SIGKILLed -- readaloud would exit
#: 137 instead of 0 on every quit.  Measured on macOS 26.6 / M1: 0 ms is always
#: killed, 50 ms is always clean; take 150 ms of headroom.  The terminal has
#: already been restored by the time this runs, so it costs the user nothing.
#: Pause after releasing the Now Playing role before the process exits.
#:
#: This is load-bearing, and cheaply disproved if you doubt it: set it to 0.0
#: and the pty tests fail with wait status 11 -- SIGSEGV, not a clean exit.
#: Releasing the role tears down MediaRemote/AppKit state that the run loop is
#: still holding, and finalising the interpreter on top of that segfaults.  A
#: review once flagged this as unnecessary after eight clean manual quits; the
#: crash needs the role to have actually been claimed, which those runs missed.
MEDIA_STOP_SETTLE = 0.15

#: How often to refresh the Control Center scrubber.  The system interpolates
#: from the playback rate between updates, so this only has to correct drift.
MEDIA_ELAPSED_EVERY = 1.0


# --------------------------------------------------------------------------- #
# prefetch
# --------------------------------------------------------------------------- #


class Prefetcher:
    """Background synthesis with a bounded, current-chunk-centred cache.

    The worker loads the engine first (that is the "loading voice..." phase),
    then repeatedly synthesizes the first *wanted* chunk that is not cached.
    "Wanted" is the current chunk followed by the next `ahead` speakable ones,
    so moving `current` immediately re-prioritises: the worker finishes at most
    one already-started chunk before it picks up the new target.
    """

    def __init__(self, engine: Any, doc: Any, ahead: int = 2,
                 capacity: int | None = None) -> None:
        self.engine = engine
        self.doc = doc
        self.ahead = max(0, int(ahead))
        self.capacity = max(2, self.ahead + 3) if capacity is None else max(1, capacity)

        self._cache: dict[int, Any] = {}
        self._cv = threading.Condition(threading.Lock())
        self._current: int | None = None
        self._generation = 0  # bumped by invalidate(); stale results are dropped
        self._stop = False
        self._busy: int | None = None

        #: set once the engine has finished loading (successfully or not)
        self.ready = threading.Event()
        self.loaded = False
        self.load_error: str | None = None
        self.synth_error: str | None = None

        self._thread = threading.Thread(
            target=self._run, name="readaloud-prefetch", daemon=True
        )

    # -- public API (main thread) -----------------------------------------

    def start(self) -> None:
        self._thread.start()

    def set_current(self, idx: int | None) -> None:
        with self._cv:
            if self._current != idx:
                self._current = idx
                self._cv.notify_all()

    def get(self, idx: int) -> Any | None:
        with self._cv:
            return self._cache.get(idx)

    def has(self, idx: int) -> bool:
        with self._cv:
            return idx in self._cache

    @property
    def busy(self) -> int | None:
        """Chunk currently being synthesized, if any."""
        return self._busy

    @property
    def cached(self) -> int:
        with self._cv:
            return len(self._cache)

    def invalidate(self) -> None:
        """Throw the cache away (voice/speed changed) and re-run everything."""
        with self._cv:
            self._cache.clear()
            self._generation += 1
            self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    def join(self, timeout: float = 2.0) -> bool:
        """Join the worker; returns True if it actually stopped."""
        if not self._thread.is_alive():
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # -- worker ------------------------------------------------------------

    def _wanted_locked(self) -> list[int]:
        cur = self._current
        if cur is None:
            return []
        out = [cur]
        i = cur
        for _ in range(self.ahead):
            try:
                nxt = self.doc.next_speakable_chunk(i)
            except Exception:  # noqa: BLE001 - a duck-typed doc in tests
                break
            if nxt is None or nxt in out:
                break
            out.append(nxt)
            i = nxt
        return out

    def _evict_locked(self) -> None:
        if len(self._cache) <= self.capacity:
            return
        cur = self._current if self._current is not None else 0
        keep = set(self._wanted_locked())
        victims = sorted(
            (k for k in self._cache if k not in keep),
            key=lambda k: -abs(k - cur),
        )
        for k in victims:
            if len(self._cache) <= self.capacity:
                break
            self._cache.pop(k, None)

    def _load(self) -> bool:
        try:
            self.engine.load()
        except BaseException as exc:  # noqa: BLE001 - surfaces in the status bar
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                self.load_error = "interrupted"
                self.ready.set()
                return False
            self.load_error = f"{type(exc).__name__}: {exc}"
            self.ready.set()
            return False
        # The Kokoro graph is large and permanent; keep the GC away from it.
        try:
            gc.freeze()
        except Exception:  # noqa: BLE001
            pass
        self.loaded = True
        self.ready.set()
        return True

    def _idle_until_stop(self) -> None:
        with self._cv:
            while not self._stop:
                self._cv.wait(0.25)

    def _run(self) -> None:
        if not self._load():
            self._idle_until_stop()
            return
        while True:
            with self._cv:
                todo: list[int] = []
                while not self._stop:
                    todo = [i for i in self._wanted_locked() if i not in self._cache]
                    if todo:
                        break
                    self._cv.wait(0.2)
                if self._stop:
                    return
                idx = todo[0]
                gen = self._generation
                self._busy = idx
            try:
                spoken = self.engine.synth(self.doc.chunks[idx])
            except BaseException as exc:  # noqa: BLE001 - Engine.synth shouldn't,
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    self._busy = None
                    return
                self.synth_error = f"{type(exc).__name__}: {exc}"  # but never trust
                spoken = None                                      # a contract blindly
            finally:
                self._busy = None
            with self._cv:
                if self._stop:
                    return
                if gen == self._generation and spoken is not None:
                    self._cache[idx] = spoken
                    err = getattr(self.engine, "last_error", None)
                    if err:
                        self.synth_error = err
                    self._evict_locked()
                elif spoken is None:
                    # do not spin on a chunk that keeps blowing up
                    self._cache[idx] = _FAILED


class _Failed:
    """Placeholder cached for a chunk whose synthesis raised outright."""

    __slots__ = ()
    chunk_idx = -1
    timings: tuple = ()
    sample_rate = 24000

    class _Empty:
        size = 0

        def __len__(self) -> int:
            return 0

    audio = _Empty()


_FAILED = _Failed()


# --------------------------------------------------------------------------- #
# the application
# --------------------------------------------------------------------------- #


class App:
    """Everything the curses loop needs, in one object (easy to drive in tests)."""

    def __init__(self, doc: Any, screen: Screen, player: Any, engine: Any, *,
                 ahead: int = 2, start_chunk: int = 0, voice: str = "",
                 autoplay: bool = True, follow_lead: int = FOLLOW_LEAD,
                 follow_margin: int = FOLLOW_MARGIN,
                 media_keys: Any = None, doc_name: str = "") -> None:
        self.doc = doc
        self.screen = screen
        self.player = player
        self.engine = engine
        self.voice = voice
        self.keymap = Keymap()
        self.prefetch = Prefetcher(engine, doc, ahead=ahead)

        #: a `mediakeys.MediaKeys` (or None when the system play/pause button
        #: is not ours).  Every call into it is wrapped: losing the button must
        #: never cost the reader a frame, let alone the session.
        self.media_keys = media_keys
        self.doc_name = doc_name or "readaloud"
        self._mk_playing: bool | None = None   # last state reported to macOS
        self._mk_chunk: Any = _UNSET           # last chunk published as a title
        self._mk_elapsed = 0.0                 # seconds into the current chunk
        self._mk_elapsed_at = 0.0              # monotonic stamp of the last push

        self.top = 0
        self.follow = True
        #: how far follow mode scrolls past the minimum, and how much context it
        #: keeps at the edges.  `c` deliberately ignores the lead: it is a
        #: "put the word in the middle right now", not a scroll-ahead.
        self.follow_lead = max(0, int(follow_lead))
        self.follow_margin = max(0, int(follow_margin))
        self.want_play = bool(autoplay)
        self.quit = False

        self.cur_word: int | None = None
        self._active: int | None = None            # chunk handed to the player
        self._spoken: Any = None                   # its Spoken
        self._target: tuple[int, int | None] | None = None  # (chunk, word slot)

        self._message = ""
        self._message_until = 0.0
        self._force = 0                            # bumped to force a repaint

        #: `less` remembers the last count given to d/u as the new default
        self._half_step: int | None = None

        self._pattern = ""
        self._search_back = False
        self.matches: list[tuple[int, int, int]] = []
        self._show_matches = True
        #: display row of the match `n`/`N` last landed on (unclamped)
        self._match_row: int | None = None

        speakable = list(doc.speakable_chunks)
        self._speakable = speakable
        self._rank = {c: i for i, c in enumerate(speakable)}
        self._start_chunk = self._resolve_start(start_chunk)

        self.screen.layout(doc, self.screen.width)

    # -- helpers -----------------------------------------------------------

    def _resolve_start(self, idx: int) -> int | None:
        if not self._speakable:
            return None
        if idx in self._rank:
            return idx
        after = [c for c in self._speakable if c >= idx]
        return after[0] if after else self._speakable[-1]

    def notify(self, msg: str, ttl: float = MESSAGE_TTL) -> None:
        self._message = msg
        self._message_until = time.monotonic() + ttl

    @property
    def message(self) -> str:
        if self._message and time.monotonic() > self._message_until:
            self._message = ""
        return self._message

    def _current_chunk(self) -> int | None:
        if self._target is not None:
            return self._target[0]
        return self._active

    def _row_span(self, cidx: int | None) -> tuple[int, int] | None:
        """The display rows ``(first, last)`` of cell chunk `cidx`'s table row.

        Follow mode keeps a table row in view whole rather than the spoken
        word: every cell of a wrapped row starts back on the row's first line,
        so following the word would scroll down through one cell and back up
        for the next.  None for any other chunk, and for a row too tall to fit
        between the margins, where only following the word keeps it on screen.
        """
        chunks = getattr(self.doc, "chunks", None) or ()
        if cidx is None or not 0 <= cidx < len(chunks):
            return None
        chunk = chunks[cidx]
        h = self.screen.body_height
        if getattr(chunk, "kind", None) != "cell" or h <= 0:
            return None
        first = self.screen.first_row_of_line(chunk.line_start)
        if chunk.line_end >= len(self.doc.plain):
            # first_row_of_line clamps: past the last line it gives the last
            # row, not one past it
            last = len(self.screen.rows) - 1
        else:
            last = self.screen.first_row_of_line(chunk.line_end) - 1
        last = max(first, last)
        m = min(self.follow_margin, max(0, (h - 1) // 2))
        if last - first + 1 > h - 2 * m:
            return None
        return first, last

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.prefetch.start()
        self._start_media_keys()
        if self._start_chunk is None:
            self.notify("nothing speakable in this document", ttl=1e9)
            self.want_play = False
        else:
            self._set_target(self._start_chunk, None)
            first_row = self.screen.first_row_of_line(
                self.doc.chunks[self._start_chunk].line_start
            )
            self.top = self.screen.clamp_top(first_row)

    def close(self, timeout: float = 2.0) -> bool:
        """Stop the worker and report whether it really went away."""
        self._stop_media_keys()
        self.prefetch.stop()
        return self.prefetch.join(timeout)

    # -- the system play/pause button --------------------------------------

    def _start_media_keys(self) -> None:
        """Claim the Now Playing role.  Must run on the main thread.

        A failure here is a missing convenience, never a reason not to read: it
        buys one status-bar line and the spacebar still works.
        """
        mk = self.media_keys
        if mk is None:
            return
        try:
            ok = bool(mk.start())
        except Exception as exc:  # noqa: BLE001 - PyObjC can raise anything
            ok = False
            self.media_keys = None
            self.notify(f"media keys unavailable: {type(exc).__name__}: {exc}")
            return
        if ok:
            # Short and brief on purpose: a longer message would push the
            # position indicator off the right of an 80-column status bar for
            # as long as it stayed up, and this is only a confirmation.
            self.notify("media keys on", ttl=2.0)
        else:
            reason = getattr(mk, "error", None) or "could not claim the role"
            self.notify(f"media keys unavailable: {reason}")

    def _media_elapsed(self) -> float:
        """Seconds heard in the current chunk, for the Control Center scrubber."""
        try:
            return max(0.0, float(self.player.position))
        except Exception:  # noqa: BLE001 - a scrubber must never break playback
            return 0.0

    def _media_duration(self) -> float:
        """Length of the current chunk's audio, or 0 when nothing is loaded."""
        try:
            spoken = self._spoken
            return max(0.0, float(spoken.duration)) if spoken is not None else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _stop_media_keys(self) -> None:
        mk = self.media_keys
        if mk is None:
            return
        try:
            mk.stop()
        except Exception:  # noqa: BLE001,S110 - quitting; nothing to report to
            pass

    def _media_poll(self) -> None:
        """Deliver run-loop callbacks and act on whatever the button sent."""
        mk = self.media_keys
        if mk is None:
            return
        try:
            mk.pump()
            commands = list(mk.poll())
        except Exception:  # noqa: BLE001 - a dead run loop must not stop the UI
            return
        for name in commands:
            self._media_command(name)

    def _media_command(self, name: str) -> None:
        if name == "pause":
            self._want_playback(False)
        elif name == "play":
            # macOS sends discrete play/pause, and it only ever sends 'play'
            # while it believes we are paused -- so a 'play' arriving while we
            # are playing IS the button asking for a pause.  Treating it as
            # "resume" is what makes the button feel one-way.
            self._want_playback(not self.want_play)
        elif name == "toggle":
            self._want_playback(not self.want_play)
        elif name == "next":
            self.handle(Command(Action.NEXT_CHUNK))
        elif name == "previous":
            self.handle(Command(Action.PREV_CHUNK))

    def _want_playback(self, playing: bool) -> None:
        """Reach `playing` through the same path the spacebar takes."""
        if bool(playing) == bool(self.want_play):
            return
        self.handle(Command(Action.PLAY_PAUSE))

    def _media_title(self, cidx: int | None) -> str:
        """The current chunk, trimmed to something a lock screen can show."""
        text = ""
        if cidx is not None:
            try:
                text = " ".join(self.doc.chunks[cidx].text.split())
            except Exception:  # noqa: BLE001 - a duck-typed doc in tests
                text = ""
        if not text:
            return self.doc_name
        if len(text) > MEDIA_TITLE_CHARS:
            cut = text[:MEDIA_TITLE_CHARS]
            space = cut.rfind(" ")
            if space > MEDIA_TITLE_CHARS // 2:
                cut = cut[:space]
            text = cut.rstrip(" ,;:-") + "..."
        return text

    def _media_sync(self) -> None:
        """Push state to macOS -- but only what actually changed.

        `tick` runs ~33 times a second; MediaRemote does not need to hear the
        same paragraph 33 times.  The playing flag, though, must be pushed
        after *every* change whatever caused it (spacebar, click, end of the
        document), or the system keeps sending 'play' and the button sticks.
        """
        mk = self.media_keys
        if mk is None:
            return
        playing = bool(self.want_play)
        if playing != self._mk_playing:
            self._mk_playing = playing
            try:
                mk.set_playing(playing)
            except Exception:  # noqa: BLE001,S110
                pass
        cur = self._current_chunk()
        if cur != self._mk_chunk:
            self._mk_chunk = cur
            self._mk_elapsed = 0.0
            try:
                mk.set_now_playing(title=self._media_title(cur),
                                   elapsed=self._media_elapsed(),
                                   duration=self._media_duration(),
                                   rate=1.0 if playing else 0.0)
            except Exception:  # noqa: BLE001,S110
                pass
            return
        # The chunk has not changed, but the scrubber in Control Center and on
        # the lock screen is driven by elapsed time: publish it once and it sits
        # frozen at 0:00 for the whole session.  Once a second is plenty -- the
        # system interpolates between updates from the playback rate -- and it
        # keeps this off the 33-per-second path.
        now = time.monotonic()
        if playing and now - self._mk_elapsed_at >= MEDIA_ELAPSED_EVERY:
            self._mk_elapsed_at = now
            try:
                mk.set_now_playing(elapsed=self._media_elapsed(),
                                   duration=self._media_duration(), rate=1.0)
            except Exception:  # noqa: BLE001,S110
                pass

    # -- playback ----------------------------------------------------------

    def _set_target(self, idx: int | None, slot: int | None) -> None:
        if idx is None:
            return
        self._target = (idx, slot)
        self.prefetch.set_current(idx)
        # Silence immediately so a jump feels instant; _pump starts the new
        # chunk as soon as the worker hands it over.
        self.player.stop()
        self._spoken = None

    def _advance(self) -> None:
        cur = self._active
        nxt = (self.doc.next_speakable_chunk(cur) if cur is not None
               else self.doc.first_speakable_chunk())
        if nxt is None:
            self.want_play = False
            self.notify("end of document")
            return
        self._set_target(nxt, None)

    def _start_playback(self) -> None:
        if self._target is not None:
            return
        if self._active is None:
            self._set_target(self._start_chunk, None)
            return
        sp = self._spoken
        if sp is not None and self.player.spoken is sp and not self.player.finished:
            self.player.resume()
            return
        if self.player.finished:
            nxt = self.doc.next_speakable_chunk(self._active)
            self._set_target(self._active if nxt is None else nxt, None)
        else:
            self._set_target(self._active, None)

    def _pump(self) -> None:
        """Hand a freshly synthesized chunk to the player, if one is waiting."""
        if self._target is None:
            return
        idx, slot = self._target
        sp = self.prefetch.get(idx)
        if sp is None:
            return
        self._target = None
        self._active = idx

        if sp is _FAILED or getattr(sp, "audio", None) is None or len(sp.audio) == 0:
            err = self.prefetch.synth_error or "no audio"
            self.notify(f"chunk {self._label(idx)} failed ({err}), skipping")
            self._spoken = None
            if self.want_play:
                self._advance()
            return

        self._spoken = sp
        t0 = 0.0
        if slot is not None:
            for tm in sp.timings:
                if tm.word_slot == slot:
                    t0 = max(0.0, float(tm.start))
                    break
        try:
            self.player.play(sp, t0)
        except Exception as exc:  # noqa: BLE001 - PlayerError and friends
            self.want_play = False
            self.notify(str(exc).splitlines()[0])
            return
        if not self.want_play:
            self.player.pause()
        # show the target word immediately rather than waiting for the callback
        if slot is not None:
            self._set_word_from_slot(idx, slot)

    def _label(self, idx: int) -> str:
        return str(self._rank.get(idx, idx) + 1)

    def _set_word_from_slot(self, cidx: int, slot: int) -> None:
        chunk = self.doc.chunks[cidx]
        if 0 <= slot < len(chunk.words):
            self.cur_word = chunk.words[slot]

    def _update_word(self) -> None:
        sp = self._spoken
        if sp is None or self._active is None:
            return
        if self.player.chunk_idx != self._active:
            return
        slot = sp.word_at(self.player.position)
        if slot is None:
            return
        self._set_word_from_slot(self._active, slot)

    # -- the frame ---------------------------------------------------------

    def status(self) -> Status:
        cur = self._current_chunk()
        loading = not self.prefetch.ready.is_set()
        msg = self.message
        if self.prefetch.load_error:
            msg = f"voice failed to load: {self.prefetch.load_error}"
        elif loading:
            msg = msg or "loading voice..."
        elif self._target is not None and not self.prefetch.has(self._target[0]):
            msg = msg or f"synthesizing chunk {self._label(self._target[0])}..."
        return Status(
            voice=self.voice,
            speed=float(getattr(self.engine, "speed", 1.0)),
            playing=bool(self.player.playing),
            loading=loading,
            chunk=self._rank.get(cur, 0) if cur is not None else 0,
            nchunks=len(self._speakable),
            follow=self.follow,
            message=msg,
            prompt=self.keymap.prompt,
        )

    def _visible_matches(self) -> list[tuple[int, int, int]]:
        if not self.matches or not self._show_matches:
            return []
        rows = self.screen.rows
        body = self.screen.body_height
        lo = self.top
        hi = min(len(rows), lo + body)
        if lo >= hi:
            return []
        lines = {rows[r].line for r in range(lo, hi)}
        return [m for m in self.matches if m[0] in lines]

    def signature(self) -> tuple:
        """Everything that can change the frame; the loop redraws only on change."""
        st = self.status()
        return (
            self.top,
            self.cur_word,
            self._current_chunk(),
            st.voice, round(st.speed, 3), st.playing, st.loading,
            st.chunk, st.nchunks, st.follow, st.message, st.prompt,
            len(self.matches) if self._show_matches else -1,
            self._force,
            self.screen.height, self.screen.width,
        )

    def draw(self) -> None:
        self.screen.draw(
            self.doc,
            self.top,
            self.cur_word,
            self._current_chunk(),
            self.status(),
            self._visible_matches(),
        )

    # -- commands ----------------------------------------------------------

    def _scroll(self, delta: int) -> None:
        self._move_view(self.top + delta)

    def _move_view(self, top: int) -> None:
        """Put the viewport at `top` by explicit user command.

        Moving the view by hand also moves where the next `n` searches from, so
        the match cursor goes back to following the viewport.
        """
        self.top = self.screen.clamp_top(top)
        self._match_row = None
        self.follow = False

    def handle(self, cmd: Command) -> None:  # noqa: C901 - a command table
        a = cmd.action
        n = max(1, int(cmd.count))
        body = max(1, self.screen.body_height)

        if a is Action.QUIT:
            self.quit = True
            return

        if a in SCROLL_ACTIONS:
            # `less`: a count before d/u/f/b is a *line* count, not a
            # multiplier -- `3f` scrolls three lines, not three windows.  With
            # no count the step is the window / half window, except that a
            # count given to d/u "becomes the new default for subsequent d and
            # u commands", as `less` documents.
            if cmd.has_count and a in (Action.HALF_PAGE_DOWN, Action.HALF_PAGE_UP):
                self._half_step = n
            step_half = (n if cmd.has_count
                         else self._half_step or max(1, body // 2))
            step_full = n if cmd.has_count else body
            if a is Action.LINE_DOWN:
                self._scroll(n)
            elif a is Action.LINE_UP:
                self._scroll(-n)
            elif a is Action.HALF_PAGE_DOWN:
                self._scroll(step_half)
            elif a is Action.HALF_PAGE_UP:
                self._scroll(-step_half)
            elif a is Action.PAGE_DOWN:
                self._scroll(step_full)
            elif a is Action.PAGE_UP:
                self._scroll(-step_full)
            elif a is Action.TOP:
                self._move_view(n - 1 if cmd.has_count else 0)
            elif a is Action.BOTTOM:
                self._move_view(n - 1 if cmd.has_count else self.screen.max_top)
            return

        if a is Action.PLAY_PAUSE:
            if self.prefetch.load_error:
                self.notify("cannot play: the voice model failed to load")
                return
            self.want_play = not self.want_play
            if self.want_play:
                self._start_playback()
            else:
                self.player.pause()
            return

        if a is Action.NEXT_CHUNK or a is Action.PREV_CHUNK:
            step = (self.doc.next_speakable_chunk if a is Action.NEXT_CHUNK
                    else self.doc.prev_speakable_chunk)
            cur = self._current_chunk()
            if cur is None:
                self._set_target(self._start_chunk, None)
                return
            nxt = cur
            for _ in range(n):
                cand = step(nxt)
                if cand is None:
                    break
                nxt = cand
            if nxt == cur and a is Action.NEXT_CHUNK:
                self.notify("last chunk")
                return
            if nxt == cur and a is Action.PREV_CHUNK:
                self.notify("first chunk")
                return
            self._set_target(nxt, None)
            self.cur_word = None
            if self.follow:
                # React, don't reposition: a chunk already on screen should
                # not move the page.  A table cell is on screen when its whole
                # row is, or the first word read would move it anyway.
                row = self.screen.first_row_of_line(self.doc.chunks[nxt].line_start)
                first, last = self._row_span(nxt) or (row, row)
                self.top = self.screen.top_for_span(
                    first, last, self.top, self.follow_margin, self.follow_lead
                )
            return

        if a is Action.SPEED_UP or a is Action.SPEED_DOWN:
            delta = SPEED_STEP * n * (1 if a is Action.SPEED_UP else -1)
            new = round(min(MAX_SPEED, max(MIN_SPEED, self.engine.speed + delta)), 2)
            if abs(new - self.engine.speed) < 1e-9:
                return
            self.engine.speed = new
            slot = self._current_slot()
            self.prefetch.invalidate()
            cur = self._current_chunk()
            self.notify(f"speed {new:.2f}x")
            if cur is not None:
                self._set_target(cur, slot)
            return

        if a is Action.TOGGLE_FOLLOW:
            self.follow = not self.follow
            if self.follow and self.cur_word is not None:
                self.top = self.screen.center_on_word(self.cur_word)
            self.notify("follow " + ("on" if self.follow else "off"), ttl=1.5)
            return

        if a is Action.CENTER:
            # `c` is "put me back where the reading is".  It parks the view
            # exactly where follow mode would -- the spoken line `follow_lead`
            # rows down with the upcoming text below it -- so the view does not
            # jump a second time on the very next auto-scroll.  `F` still
            # centres, which is what makes the two keys usefully different.
            self.follow = True
            cur = (self.doc.chunk_of_word(self.cur_word)
                   if self.cur_word is not None else self._current_chunk())
            span = self._row_span(cur)
            if span is not None:
                # a table row: where tick's top_for_span leaves it
                self.top = self.screen.follow_top_for_span(
                    *span, self.follow_margin, self.follow_lead
                )
            elif self.cur_word is not None:
                self.top = self.screen.follow_top_for_word(
                    self.cur_word, self.follow_margin, self.follow_lead
                )
            elif cur is not None:
                row = self.screen.first_row_of_line(self.doc.chunks[cur].line_start)
                self.top = self.screen.follow_top_for_row(
                    row, self.follow_margin, self.follow_lead
                )
            return

        if a is Action.CLICK_WORD:
            self._click(cmd.y, cmd.x)
            return

        if a is Action.RESIZE:
            anchor = self.screen.row_line_col(self.top)
            self.screen.handle_resize(self.doc)
            if anchor is not None:
                self.top = self.screen.clamp_top(self.screen.row_for(*anchor))
            else:
                self.top = self.screen.clamp_top(self.top)
            self._force += 1
            return

        if a is Action.REDRAW:
            self.screen.invalidate()
            self._force += 1
            return

        if a is Action.CANCEL:
            # Escape dismisses the highlighting, but the pattern and the match
            # list survive so `n`/`N` keep working (as in `less`).
            self._show_matches = False
            self._message = ""
            self._force += 1
            return

        if a in (Action.SEARCH_FORWARD, Action.SEARCH_BACKWARD,
                 Action.SEARCH_TYPING, Action.SEARCH_CANCEL):
            self._force += 1
            return

        if a is Action.SEARCH_SUBMIT:
            self._search(cmd.text or self._pattern, cmd.backward)
            return

        if a is Action.SEARCH_NEXT or a is Action.SEARCH_PREV:
            back = self._search_back if a is Action.SEARCH_NEXT else not self._search_back
            if not self._pattern:
                self.notify("no previous search")
                return
            if not self.matches:
                # e.g. the layout changed under us: recompute rather than
                # claiming a pattern that does match is "not found"
                self.matches = self._find(self._pattern)
            self._show_matches = True
            if not self.matches:
                self.notify(f"pattern not found: {self._pattern}")
                return
            for _ in range(n):
                self._jump_match(back)
            return

    def _current_slot(self) -> int | None:
        """Word slot of the highlighted word inside the current chunk."""
        cur = self._current_chunk()
        if cur is None or self.cur_word is None:
            return None
        try:
            if self.doc.chunk_of_word(self.cur_word) != cur:
                return None
            return self.doc.slot_of_word(self.cur_word)
        except Exception:  # noqa: BLE001
            return None

    def _click(self, y: int | None, x: int | None) -> None:
        if y is None or x is None:
            return
        widx = self.screen.hit_test(y, x)
        if widx is None:
            loc = self.screen.hit_test_line_col(y, x)
            if loc is not None:
                widx = self.doc.nearest_word(*loc)
        if widx is None:
            return
        cidx = self.doc.chunk_of_word(widx)
        if not getattr(self.doc.chunks[cidx], "speakable", True):
            return          # nothing there to play; playback skips it too
        slot = self.doc.slot_of_word(widx)
        self.cur_word = widx
        self.want_play = True
        self._set_target(cidx, slot)

    # -- search ------------------------------------------------------------

    def _compile(self, pattern: str):
        # `less` treats the pattern as a regex; fall back to a literal search
        # when it does not compile.  Smart-case: any capital => case sensitive.
        flags = 0 if any(c.isupper() for c in pattern) else re.IGNORECASE
        try:
            return re.compile(pattern, flags)
        except re.error:
            return re.compile(re.escape(pattern), flags)

    def _find(self, pattern: str) -> list[tuple[int, int, int]]:
        """All (line, start, end) matches of `pattern`, capped at MAX_MATCHES."""
        rx = self._compile(pattern)
        found: list[tuple[int, int, int]] = []
        for li, text in enumerate(self.doc.plain):
            for m in rx.finditer(text):
                if m.end() > m.start():
                    found.append((li, m.start(), m.end()))
                    if len(found) >= MAX_MATCHES:
                        break
            if len(found) >= MAX_MATCHES:
                break
        return found

    def _search(self, pattern: str, backward: bool) -> None:
        pattern = pattern or self._pattern
        if not pattern:
            # `/` + Enter with nothing typed and nothing remembered: say so
            # rather than closing the prompt in silence.
            self.notify("no previous search")
            return
        self._pattern = pattern
        self._search_back = bool(backward)
        self._show_matches = True
        # a fresh search starts from wherever the viewport is now
        self._match_row = None
        self.matches = self._find(pattern)
        if not self.matches:
            self.notify(f"pattern not found: {pattern}")
            return
        self._jump_match(self._search_back)

    def _jump_match(self, backward: bool) -> None:
        if not self.matches:
            self.notify("pattern not found")
            return
        rows = sorted({self.screen.row_for(li, s) for li, s, _e in self.matches})
        # The cursor is the row of the match we last landed on, NOT `self.top`:
        # near the end of the document clamp_top() pins `top` above that row, so
        # using `top` would re-select the same match forever and never wrap.
        cursor = self._match_row if self._match_row is not None else self.top
        if not backward:
            after = [r for r in rows if r > cursor]
            target = after[0] if after else rows[0]
            if not after:
                self.notify("search wrapped to the top", ttl=2.0)
        else:
            before = [r for r in rows if r < cursor]
            target = before[-1] if before else rows[-1]
            if not before:
                self.notify("search wrapped to the bottom", ttl=2.0)
        self._match_row = target
        self.top = self.screen.clamp_top(target)
        self.follow = False

    # -- the loop ----------------------------------------------------------

    def tick(self) -> None:
        """One round of housekeeping: playback state, highlight, follow-scroll."""
        self._media_poll()
        self._pump()
        self._update_word()
        if (self.want_play and self._target is None and self._active is not None
                and self.player.finished):
            self._advance()
        if self.follow and self.cur_word is not None:
            span = self._row_span(self.doc.chunk_of_word(self.cur_word))
            if span is not None:
                self.top = self.screen.top_for_span(
                    *span, self.top, self.follow_margin, self.follow_lead
                )
            else:
                self.top = self.screen.top_for_word(
                    self.cur_word, self.top, self.follow_margin, self.follow_lead
                )
        # last: whatever changed the playback state this frame -- the button, a
        # keystroke, a click, the end of the document -- is reported from here.
        self._media_sync()

    def step(self, timeout_ms: int = POLL_MS) -> None:
        """Read at most one event, act on it, then tick."""
        event = self.screen.read_event(timeout_ms)
        cmd = self.keymap.feed(event)
        if cmd:
            self.handle(cmd)
        self.tick()

    def run(self) -> None:
        self.draw()
        last = self.signature()
        while not self.quit:
            self.step(POLL_MS)
            sig = self.signature()
            if sig != last:
                self.draw()
                last = sig


# --------------------------------------------------------------------------- #
# entry point used by cli.py
# --------------------------------------------------------------------------- #


@contextmanager
def quiet_stderr():
    """Park fd 2 on a temp file for the life of the curses session.

    The model stack is chatty on stderr *after* curses owns the screen: the
    HuggingFace fetch progress bar, torch's `torch.jit.script` FutureWarning and
    phonemizer's "words count mismatch" warning all land mid-frame and shred the
    display.  Everything the user needs to know reaches them through the status
    bar instead; the captured text is only replayed if the app dies.
    """
    tmp = tempfile.TemporaryFile(mode="w+b")
    saved = None
    try:
        try:
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            saved = os.dup(2)
            os.dup2(tmp.fileno(), 2)
        except OSError:
            saved = None
        yield tmp
    finally:
        if saved is not None:
            try:
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            try:
                os.dup2(saved, 2)
            finally:
                os.close(saved)


def _replay_stderr(tmp, limit: int = 4000) -> None:
    try:
        tmp.seek(0)
        text = tmp.read().decode("utf-8", "replace").strip()
    except Exception:  # noqa: BLE001
        return
    if text:
        print(text[-limit:], file=sys.stderr)


def _close_engine_async(engine: Any) -> None:
    """Release the model without ever blocking the quit path.

    `Engine.close()` takes the same lock `Engine.load()` holds for its whole
    duration, so calling it inline while the prefetch worker is still loading
    would keep the process alive for the rest of the load -- seconds on a warm
    model, the length of a download on a cold one -- long after curses gave the
    terminal back.  Hand it to a daemon thread instead; if it is still waiting
    when the interpreter exits, the process exits anyway.
    """
    def _close() -> None:
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        threading.Thread(target=_close, name="readaloud-engine-close",
                         daemon=True).start()
    except Exception:  # noqa: BLE001 - can't spawn: fall back to a direct call
        _close()


def run(doc: Any, *, voice: str = "af_heart", speed: float = 1.0,
        lang: str = "a", repo: str | None = None, prefetch: int = 2,
        device: Any = None, start_chunk: int = 0,
        no_color: bool = False, follow_lead: int = FOLLOW_LEAD,
        follow_margin: int = FOLLOW_MARGIN, media_keys: bool = False,
        media_keys_explicit: bool = False,
        doc_name: str = "", notices: Sequence[str] = ()) -> int:
    """Open the audio device, enter curses, and run until the user quits."""
    from .player import Player, PlayerError
    from .speech import DEFAULT_REPO_ID, Engine

    engine = Engine(voice=voice, speed=speed, lang_code=lang,
                    repo_id=repo or DEFAULT_REPO_ID)

    try:
        player = Player(device=device)
    except PlayerError as exc:
        print(f"readaloud: {exc}", file=sys.stderr)
        return 1

    # The HF hub progress bar would draw straight over the document.
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    # The Now Playing role, if the user wants it and PyObjC is here to give it.
    # Built on the main thread and never anywhere else: `MediaKeys.start` makes
    # an NSApplication, which AppKit only allows from the main thread.
    keys: Any = None
    media_keys_error = ""
    if media_keys:
        try:
            from . import mediakeys as mediakeys_mod

            if mediakeys_mod.available():
                keys = mediakeys_mod.MediaKeys(title="readaloud",
                                               artist=doc_name or "readaloud")
            elif media_keys_explicit:
                # Explicitly asked for, but PyObjC is not installed.  Say so,
                # or the user presses a button that will never work with no
                # clue why.  Only on an explicit request: media_keys defaults
                # to true, so doing this unprompted would nag everyone who
                # never wanted the feature, and a long message pushes the
                # position indicator off an 80-column status bar.
                media_keys_error = "install the [mediakeys] extra"
        except Exception:  # noqa: BLE001 - an import that only ever adds a nicety
            keys = None

    theme = Theme(chunk_bg=None) if no_color else Theme()
    app: App | None = None
    status = 0
    crashed = False
    clean = True
    message = ""
    with quiet_stderr() as captured:
        try:
            with screen_session(theme) as screen:
                app = App(doc, screen, player, engine, ahead=prefetch,
                          start_chunk=start_chunk, voice=voice,
                          follow_lead=follow_lead, follow_margin=follow_margin,
                          media_keys=keys, doc_name=doc_name)
                app.start()
                if media_keys_error:
                    app.notify(f"media keys unavailable: {media_keys_error}",
                               ttl=NOTICE_TTL)
                if notices:
                    # config-file complaints: the status bar, never a print()
                    # -- stdout belongs to curses from here on.
                    first, extra = notices[0], len(notices) - 1
                    app.notify(first + (f"  (+{extra} more)" if extra else ""),
                               ttl=NOTICE_TTL)
                try:
                    app.run()
                except KeyboardInterrupt:
                    pass
        except KeyboardInterrupt:
            status = 130
        except Exception as exc:  # noqa: BLE001 - the terminal is restored by now
            message = f"readaloud: {type(exc).__name__}: {exc}"
            status = 1
            crashed = True
        finally:
            try:
                player.close()
            except Exception:  # noqa: BLE001
                pass
            clean = True
            if app is not None:
                # Never wait on the worker here: it may be deep inside a model
                # load or a synthesis that cannot be interrupted, and the user
                # has already got their terminal back.  It is a daemon thread,
                # so asking it to stop is enough.
                clean = app.close(timeout=0.0)
            if keys is not None:
                # `App.close` already did this; repeat it for the paths where
                # the App was never built.  `stop()` is idempotent.
                try:
                    keys.stop()
                except Exception:  # noqa: BLE001,S110
                    pass
                time.sleep(MEDIA_STOP_SETTLE)
            _close_engine_async(engine)

    if crashed:
        print(message, file=sys.stderr)
        _replay_stderr(captured)
    if not clean and os.environ.get("READALOUD_DEBUG"):
        # Only a diagnostic: the worker is a daemon blocked inside Kokoro, and
        # nothing on the quit path joins it, so it cannot delay the exit.
        print("readaloud: the synthesis worker was still busy at exit",
              file=sys.stderr)
    try:
        captured.close()
    except Exception:  # noqa: BLE001
        pass
    return status
