"""Take over the system Play/Pause button (headphones, Control Center, media keys).

Pressing play/pause on a Bluetooth headset does NOT reach the terminal.  macOS
routes it through MediaRemote to whichever process holds the "Now Playing" role,
and when nothing holds it the system launches Music.  So the only way to get the
button is to claim that role, which is what this module does via the public
MediaPlayer framework.

Established empirically on macOS 26.6 / M1, and each point cost a probe:

* `NSApplication` **must** exist, on the **main thread**, with the accessory
  activation policy.  Without it the process is not an app as far as MediaRemote
  is concerned and the press launches Music instead.  A background thread running
  its own run loop receives nothing.
* A live CoreAudio stream makes the claim stick; readaloud always has one.
* Presses arrive as discrete `play` / `pause` commands, not `togglePlayPause`.
  If the reported playback state is never updated the system sends `play` every
  single time, so the button stops toggling -- push the real state back after
  every change and macOS alternates correctly.
* The run loop only needs *pumping*, not owning: calling `pump()` from the curses
  poll tick is enough, so the TUI keeps the main thread.

Everything here is optional.  If PyObjC is missing the module reports
`available() is False` and readaloud carries on with the spacebar alone.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

__all__ = ["MediaKeys", "available", "unavailable_reason"]

_IMPORT_ERROR: str | None = None
try:  # pragma: no cover - the import result is the thing being tested
    import AppKit  # type: ignore
    import Foundation  # type: ignore
    import MediaPlayer  # type: ignore
except Exception as exc:  # noqa: BLE001 - any import failure means "not available"
    AppKit = Foundation = MediaPlayer = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def available() -> bool:
    """True when the MediaPlayer bindings imported."""
    return MediaPlayer is not None


def unavailable_reason() -> str:
    """Why `available()` is False, for the status bar."""
    if available():
        return ""
    return _IMPORT_ERROR or "PyObjC (pyobjc-framework-MediaPlayer) is not installed"


class MediaKeys:
    """Claim the Now Playing role and surface its commands as a queue.

    Commands are queued rather than dispatched straight into a callback: the
    handlers fire inside `pump()`, which the curses loop calls, and the loop
    should stay in charge of when state actually changes.

    Every method is a no-op when the bindings are missing, so callers never have
    to guard.
    """

    def __init__(self, title: str = "readaloud", artist: str = "readaloud") -> None:
        self.title = title
        self.artist = artist
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._center: Any = None
        self._info: Any = None
        self._app: Any = None
        self._started = False
        self._playing = False
        self._handlers: list[Any] = []  # keep handler blocks alive
        self.error: str | None = None if available() else unavailable_reason()

    # -- lifecycle ---------------------------------------------------------

    def start(self, duration: float = 0.0) -> bool:
        """Claim the role. Returns True on success. Never raises.

        MUST be called from the main thread -- AppKit requires it, and so does
        MediaRemote's idea of what an app is.
        """
        if not available() or self._started:
            return self._started
        try:
            self._app = AppKit.NSApplication.sharedApplication()
            # Accessory: a real app to the system, but no Dock icon and no menu
            # bar stealing focus from the terminal.
            self._app.setActivationPolicy_(
                AppKit.NSApplicationActivationPolicyAccessory
            )
            self._center = MediaPlayer.MPRemoteCommandCenter.sharedCommandCenter()
            self._info = MediaPlayer.MPNowPlayingInfoCenter.defaultCenter()

            for attr, label in (
                ("togglePlayPauseCommand", "toggle"),
                ("playCommand", "play"),
                ("pauseCommand", "pause"),
                ("stopCommand", "pause"),
                ("nextTrackCommand", "next"),
                ("previousTrackCommand", "previous"),
            ):
                try:
                    command = getattr(self._center, attr)()
                except AttributeError:
                    continue
                command.setEnabled_(True)
                handler = self._make_handler(label)
                self._handlers.append(handler)
                command.addTargetWithHandler_(handler)

            self._playing = True
            self._publish(duration=duration, elapsed=0.0)
            self._started = True
        except Exception as exc:  # noqa: BLE001 - a broken claim must not stop the reader
            self.error = f"{type(exc).__name__}: {exc}"
            self._started = False
        return self._started

    def stop(self) -> None:
        """Release the role so the next app can have it. Never raises."""
        if not self._started:
            return
        try:
            for attr in ("togglePlayPauseCommand", "playCommand", "pauseCommand",
                         "stopCommand", "nextTrackCommand", "previousTrackCommand"):
                try:
                    command = getattr(self._center, attr)()
                    command.setEnabled_(False)
                    # Disabling is not detaching: without removeTarget_ a second
                    # start() stacks a second set of handlers on the same
                    # commands and every press fires them all.
                    command.removeTarget_(None)
                except Exception:  # noqa: BLE001,S110 - best effort teardown
                    pass
            self._info.setPlaybackState_(
                MediaPlayer.MPNowPlayingPlaybackStateStopped
            )
            self._info.setNowPlayingInfo_(None)
        except Exception:  # noqa: BLE001,S110
            pass
        finally:
            self._started = False
            self._handlers.clear()

    # -- main-loop integration --------------------------------------------

    def pump(self, seconds: float = 0.0) -> None:
        """Give the run loop a slice so queued commands get delivered.

        Call once per curses tick.  `seconds` of 0 means "deliver whatever is
        ready and return immediately", which is what a 30 ms poll loop wants.
        """
        if not self._started:
            return
        try:
            Foundation.NSRunLoop.currentRunLoop().runMode_beforeDate_(
                Foundation.NSDefaultRunLoopMode,
                Foundation.NSDate.dateWithTimeIntervalSinceNow_(seconds),
            )
        except Exception:  # noqa: BLE001,S110 - a pump failure must not kill the UI
            pass

    def poll(self) -> list[str]:
        """Drain queued commands: 'toggle', 'play', 'pause', 'next', 'previous'."""
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out

    # -- state -------------------------------------------------------------

    def set_playing(self, playing: bool) -> None:
        """Report the real state. Without this the button stops toggling."""
        if not self._started or playing == self._playing:
            return
        self._playing = playing
        try:
            self._info.setPlaybackState_(
                MediaPlayer.MPNowPlayingPlaybackStatePlaying if playing
                else MediaPlayer.MPNowPlayingPlaybackStatePaused
            )
        except Exception:  # noqa: BLE001,S110
            pass

    def set_now_playing(self, title: str | None = None, *, elapsed: float | None = None,
                        duration: float | None = None, rate: float | None = None) -> None:
        """Update what Control Center and the lock screen show."""
        if not self._started:
            return
        if title is not None:
            self.title = title
        self._publish(duration=duration, elapsed=elapsed, rate=rate)

    # -- internals ---------------------------------------------------------

    def _make_handler(self, label: str):
        def handler(_event) -> int:
            with self._lock:
                self._queue.append(label)
            return 0  # MPRemoteCommandHandlerStatusSuccess
        return handler

    def _publish(self, *, duration: float | None = None, elapsed: float | None = None,
                 rate: float | None = None) -> None:
        try:
            info = {
                MediaPlayer.MPMediaItemPropertyTitle: self.title,
                MediaPlayer.MPMediaItemPropertyArtist: self.artist,
                MediaPlayer.MPNowPlayingInfoPropertyPlaybackRate:
                    float(rate if rate is not None else (1.0 if self._playing else 0.0)),
            }
            if duration is not None:
                info[MediaPlayer.MPMediaItemPropertyPlaybackDuration] = float(duration)
            if elapsed is not None:
                info[MediaPlayer.MPNowPlayingInfoPropertyElapsedPlaybackTime] = float(elapsed)
            self._info.setNowPlayingInfo_(info)
            self._info.setPlaybackState_(
                MediaPlayer.MPNowPlayingPlaybackStatePlaying if self._playing
                else MediaPlayer.MPNowPlayingPlaybackStatePaused
            )
        except Exception:  # noqa: BLE001,S110
            pass

    def __enter__(self) -> MediaKeys:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
