"""readaloud/keys.py — terminal input decoding and the ``less``-style keymap.

Two layers live here:

1. **Event decoding** (`read_event`, `MouseEvent`).  This ncurses build
   (6.0.20150808, NCURSES_MOUSE_VERSION=1) cannot report wheel-down at all and
   only knows the *application*-mode forms of the arrow / Home / End keys, and
   `curses.define_key` / `curses.newterm` / `curses.set_term` are all missing.
   So we enable SGR (1006) mouse reporting ourselves and hand-decode CSI
   sequences out of `getch()`, keeping the plain `KEY_MOUSE` path alive as a
   fallback for terminals that ignore 1006.

2. **The keymap** (`Action`, `Command`, `Keymap`).  `Keymap.feed(event)` turns
   one decoded event into a `Command`, and owns the two pieces of modal state a
   ``less`` clone needs: a pending numeric count prefix (``10j``) and the
   ``/`` ``?`` search input line.

Everything here is stdlib-only.  Verified end to end under a pty with piped
stdin; see `Screen`/`screen_session` in `readaloud.ui` for the terminal
bootstrap that must run before `curses.initscr()`.
"""

from __future__ import annotations

import curses
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

__all__ = [
    "MouseEvent",
    "WHEEL_UP",
    "WHEEL_DOWN",
    "SGR_MOUSE_ON",
    "SGR_MOUSE_OFF",
    "read_event",
    "Action",
    "Command",
    "Keymap",
    "SCROLL_ACTIONS",
    "KEYMAP",
    "describe_keys",
]

# --------------------------------------------------------------------------
# mouse
# --------------------------------------------------------------------------

# ncurses only ever turns on \e[?1000h (never 1002/1003/1006), and its
# mouse-v1 encoding has no BUTTON5 at all: wheel-down collides with
# REPORT_MOUSE_POSITION.  Ask the terminal for SGR encoding ourselves.
SGR_MOUSE_ON = "\x1b[?1000h\x1b[?1006h"
SGR_MOUSE_OFF = "\x1b[?1006l\x1b[?1000l"

WHEEL_UP = 4
WHEEL_DOWN = 5


@dataclass(frozen=True)
class MouseEvent:
    """A decoded mouse report.  Coordinates are 0-based screen cells."""

    button: int  # 1=left 2=middle 3=right 4=wheel-up 5=wheel-down 0=motion
    x: int
    y: int
    pressed: bool
    shift: bool = False
    alt: bool = False
    ctrl: bool = False

    @property
    def wheel(self) -> int:
        """-1 for wheel-up, +1 for wheel-down, 0 for a non-wheel event."""
        if self.button == WHEEL_UP:
            return -1
        if self.button == WHEEL_DOWN:
            return 1
        return 0


# Sequences this terminfo does NOT carry (it only knows the application-mode
# \EOA..\EOD / \EOH / \EOF forms) and which `curses.define_key` cannot be used
# to teach it, because that function does not exist in this Python build.
_CSI_KEYS: dict[str, int] = {
    "A": curses.KEY_UP,
    "B": curses.KEY_DOWN,
    "C": curses.KEY_RIGHT,
    "D": curses.KEY_LEFT,
    "H": curses.KEY_HOME,
    "F": curses.KEY_END,
    "1~": curses.KEY_HOME,
    "7~": curses.KEY_HOME,
    "4~": curses.KEY_END,
    "8~": curses.KEY_END,
    "5~": curses.KEY_PPAGE,
    "6~": curses.KEY_NPAGE,
    "3~": curses.KEY_DC,
    "2~": curses.KEY_IC,
    "Z": curses.KEY_BTAB,
}


def _drain_escape(stdscr, deadline_s: float = 0.05) -> tuple[str, str]:
    """`getch()` just returned 27; collect the rest of the escape sequence."""
    buf = ""
    end = time.time() + deadline_s
    while time.time() < end and len(buf) < 32:
        ch = stdscr.getch()
        if ch == -1:
            time.sleep(0.002)
            continue
        if ch > 255:  # a real key snuck in: hand it back
            curses.ungetch(ch)
            break
        buf += chr(ch)
        if len(buf) == 1 and buf != "[":
            break  # Alt-<key>, or ESC followed by an ordinary key
        if len(buf) >= 2:
            if buf[1] == "<":  # SGR mouse: \e[<b;x;y M|m
                if buf[-1] in "Mm":
                    return ("mouse", buf)
                continue
            if buf[-1].isalpha() or buf[-1] == "~":
                return ("csi", buf[1:])
    return ("raw", buf)


def _sgr_to_event(body: str) -> MouseEvent | None:
    """Decode a `\\e[<b;x;yM` / `...m` body into a MouseEvent (1-based -> 0-based)."""
    press = body[-1] == "M"
    try:
        b, x, y = (int(v) for v in body[2:-1].split(";"))
    except ValueError:
        return None
    if b & 64:
        button = WHEEL_UP if (b & 3) == 0 else WHEEL_DOWN
    elif b & 32:
        button = 0  # motion / drag
    else:
        button = (b & 3) + 1
        if button == 4:
            button = 0
    return MouseEvent(
        button, x - 1, y - 1, press, bool(b & 4), bool(b & 8), bool(b & 16)
    )


def _bstate_to_event(x: int, y: int, bstate: int) -> MouseEvent | None:
    """Fallback for terminals that ignore SGR 1006 and send X10 mouse reports."""
    if bstate & curses.BUTTON4_PRESSED:
        return MouseEvent(WHEEL_UP, x, y, True)
    if bstate == curses.REPORT_MOUSE_POSITION:
        # mouse-v1 has no BUTTON5_PRESSED constant at all (referencing it raises
        # AttributeError at import time), and wheel-down lands on this exact
        # bit.  ncurses never enables 1002/1003, so a bare position report can
        # only be a wheel event.
        return MouseEvent(WHEEL_DOWN, x, y, True)
    for bit, btn in (
        (curses.BUTTON1_PRESSED, 1),
        (curses.BUTTON2_PRESSED, 2),
        (curses.BUTTON3_PRESSED, 3),
    ):
        if bstate & bit:
            return MouseEvent(
                btn,
                x,
                y,
                True,
                bool(bstate & curses.BUTTON_SHIFT),
                bool(bstate & curses.BUTTON_ALT),
                bool(bstate & curses.BUTTON_CTRL),
            )
    for bit, btn in (
        (curses.BUTTON1_RELEASED, 1),
        (curses.BUTTON2_RELEASED, 2),
        (curses.BUTTON3_RELEASED, 3),
    ):
        if bstate & bit:
            return MouseEvent(btn, x, y, False)
    return None


def read_event(stdscr, timeout_ms: int = 50) -> int | MouseEvent | None:
    """Read one input event, or ``None`` when `timeout_ms` elapses with no input.

    Returns an ``int`` (a character code or a ``curses.KEY_*`` constant,
    including ``curses.KEY_RESIZE``) or a `MouseEvent`.
    """
    stdscr.timeout(timeout_ms)
    ch = stdscr.getch()
    if ch == -1:
        return None
    if ch == curses.KEY_MOUSE:
        try:
            _id, x, y, _z, bstate = curses.getmouse()
        except curses.error:
            return None
        return _bstate_to_event(x, y, bstate)
    if ch == 27:
        stdscr.timeout(0)
        try:
            kind, body = _drain_escape(stdscr)
        finally:
            stdscr.timeout(timeout_ms)
        if kind == "mouse":
            return _sgr_to_event(body)
        if kind == "csi":
            key = _CSI_KEYS.get(body)
            if key is not None:
                return key
            return 27
        if body:  # ESC then an ordinary key: give the key back, report ESC
            for c in reversed(body):
                curses.ungetch(ord(c))
        return 27
    return ch


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------


class Action(Enum):
    """Every command the TUI understands."""

    NONE = auto()          # idle tick / swallowed key
    QUIT = auto()

    # viewport (moves independently of playback)
    LINE_DOWN = auto()
    LINE_UP = auto()
    HALF_PAGE_DOWN = auto()
    HALF_PAGE_UP = auto()
    PAGE_DOWN = auto()
    PAGE_UP = auto()
    TOP = auto()
    BOTTOM = auto()

    # search
    SEARCH_FORWARD = auto()   # '/' pressed: prompt opened
    SEARCH_BACKWARD = auto()  # '?' pressed: prompt opened
    SEARCH_TYPING = auto()    # prompt text changed; redraw the status bar
    SEARCH_SUBMIT = auto()    # Enter: `text` + `backward` carry the query
    SEARCH_CANCEL = auto()    # Escape / backspace past the start
    SEARCH_NEXT = auto()      # 'n'
    SEARCH_PREV = auto()      # 'N'

    # reader
    PLAY_PAUSE = auto()
    NEXT_CHUNK = auto()
    PREV_CHUNK = auto()
    SPEED_UP = auto()
    SPEED_DOWN = auto()
    TOGGLE_FOLLOW = auto()
    CENTER = auto()
    CLICK_WORD = auto()       # `y`/`x` are screen cells; feed them to hit_test

    # housekeeping
    RESIZE = auto()
    REDRAW = auto()
    CANCEL = auto()           # bare Escape outside the search prompt


#: Actions that move the viewport by hand and therefore switch follow-mode off.
SCROLL_ACTIONS = frozenset(
    {
        Action.LINE_DOWN,
        Action.LINE_UP,
        Action.HALF_PAGE_DOWN,
        Action.HALF_PAGE_UP,
        Action.PAGE_DOWN,
        Action.PAGE_UP,
        Action.TOP,
        Action.BOTTOM,
    }
)


@dataclass(frozen=True)
class Command:
    """One resolved user command."""

    action: Action
    count: int = 1                 # repeat count (already defaulted to 1)
    has_count: bool = False        # True only when the user typed digits
    text: str | None = None        # SEARCH_SUBMIT: the pattern
    backward: bool = False         # SEARCH_SUBMIT / SEARCH_*: direction
    y: int | None = None           # CLICK_WORD: screen row
    x: int | None = None           # CLICK_WORD: screen column
    mouse: bool = False            # event came from the mouse (wheel/click)
    key: int | None = None         # raw key code, for diagnostics

    def __bool__(self) -> bool:
        return self.action is not Action.NONE


_NOOP = Command(Action.NONE)

# The keymap proper.  Ctrl-<letter> is simply ord(letter) & 0x1f; Enter arrives
# as 10 (ncurses is in nl mode, so both CR and LF collapse to LF) and as
# curses.KEY_ENTER=343 from the numeric keypad — never as 13.
KEYMAP: dict[int, Action] = {
    # --- less viewport keys ---
    ord("j"): Action.LINE_DOWN,
    curses.KEY_DOWN: Action.LINE_DOWN,
    10: Action.LINE_DOWN,
    13: Action.LINE_DOWN,
    curses.KEY_ENTER: Action.LINE_DOWN,
    ord("k"): Action.LINE_UP,
    curses.KEY_UP: Action.LINE_UP,
    ord("d"): Action.HALF_PAGE_DOWN,
    0x04: Action.HALF_PAGE_DOWN,  # Ctrl-D
    ord("u"): Action.HALF_PAGE_UP,
    0x15: Action.HALF_PAGE_UP,  # Ctrl-U
    ord("f"): Action.PAGE_DOWN,
    0x06: Action.PAGE_DOWN,  # Ctrl-F
    curses.KEY_NPAGE: Action.PAGE_DOWN,
    ord("b"): Action.PAGE_UP,
    0x02: Action.PAGE_UP,  # Ctrl-B
    curses.KEY_PPAGE: Action.PAGE_UP,
    ord("g"): Action.TOP,
    curses.KEY_HOME: Action.TOP,
    ord("G"): Action.BOTTOM,
    curses.KEY_END: Action.BOTTOM,
    ord("/"): Action.SEARCH_FORWARD,
    ord("?"): Action.SEARCH_BACKWARD,
    ord("n"): Action.SEARCH_NEXT,
    ord("N"): Action.SEARCH_PREV,
    ord("q"): Action.QUIT,
    # --- reader keys ---
    ord(" "): Action.PLAY_PAUSE,
    ord("."): Action.NEXT_CHUNK,
    curses.KEY_RIGHT: Action.NEXT_CHUNK,
    ord(","): Action.PREV_CHUNK,
    curses.KEY_LEFT: Action.PREV_CHUNK,
    ord("]"): Action.SPEED_UP,
    ord("["): Action.SPEED_DOWN,
    ord("F"): Action.TOGGLE_FOLLOW,
    ord("c"): Action.CENTER,
    # --- housekeeping ---
    curses.KEY_RESIZE: Action.RESIZE,
    0x0C: Action.REDRAW,  # Ctrl-L
    0x12: Action.REDRAW,  # Ctrl-R
    27: Action.CANCEL,
}

#: Human-readable help, in the order the contract lists the bindings.
_HELP: tuple[tuple[str, str], ...] = (
    ("j / Down / Enter", "line down"),
    ("k / Up", "line up"),
    ("d / ^D", "half page down"),
    ("u / ^U", "half page up"),
    ("f / ^F / PgDn", "page down"),
    ("b / ^B / PgUp", "page up"),
    ("g / Home", "top"),
    ("G / End", "bottom"),
    ("/  ?  n  N", "search fwd / back / repeat / repeat back"),
    ("Space", "play / pause"),
    (". / Right", "next chunk"),
    (", / Left", "previous chunk"),
    ("[  ]", "speed down / up"),
    ("F", "toggle follow-the-word"),
    ("c", "centre on the current word"),
    ("wheel", "scroll"),
    ("click", "jump playback to that word"),
    ("q", "quit"),
)


def describe_keys() -> list[tuple[str, str]]:
    """`[(keys, description)]` for a help overlay or the README."""
    return list(_HELP)


# --------------------------------------------------------------------------
# the keymap state machine
# --------------------------------------------------------------------------

_BACKSPACES = frozenset({8, 127, curses.KEY_BACKSPACE, curses.KEY_DC})
_ENTERS = frozenset({10, 13, curses.KEY_ENTER})
_MAX_COUNT_DIGITS = 7


class Keymap:
    """Turns decoded events into `Command`s.

    Owns two pieces of modal state:

    * a pending numeric count prefix, ``less``-style — ``10j`` scrolls ten
      lines, ``3.`` skips three chunks forward;
    * the ``/`` / ``?`` search input line.  While `searching` is true every key
      goes into the pattern buffer instead of the keymap, and `prompt` is the
      string the status bar should display.  Mouse events are *not* swallowed
      by the prompt: the wheel keeps scrolling while a pattern is being typed.
    """

    __slots__ = ("wheel_lines", "_count", "_searching", "_search_back", "_buf")

    def __init__(self, wheel_lines: int = 3) -> None:
        self.wheel_lines = wheel_lines
        self._count = ""
        self._searching = False
        self._search_back = False
        self._buf = bytearray()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Keymap(wheel_lines={self.wheel_lines}, "
                f"count={self._count!r}, prompt={self.prompt!r})")

    # -- state queries ----------------------------------------------------

    @property
    def searching(self) -> bool:
        """True while the ``/`` or ``?`` input line is open."""
        return self._searching

    @property
    def pattern(self) -> str:
        """The search pattern typed so far."""
        return self._buf.decode("utf-8", "replace")

    @property
    def prompt(self) -> str | None:
        """The status-bar prompt (``/foo``), or ``None`` when not searching."""
        if not self._searching:
            return None
        return ("?" if self._search_back else "/") + self.pattern

    @property
    def pending_count(self) -> str:
        """The digits typed so far, for echoing in the status bar."""
        return self._count

    def reset(self) -> None:
        """Drop the pending count and close the search prompt."""
        self._count = ""
        self._searching = False
        self._buf.clear()

    # -- internals --------------------------------------------------------

    def _take_count(self) -> tuple[int, bool]:
        raw = self._count
        self._count = ""
        if not raw:
            return 1, False
        try:
            n = int(raw)
        except ValueError:  # pragma: no cover - digits only ever land in _count
            return 1, False
        return (n if n > 0 else 1), True

    def _feed_search(self, ch: int) -> Command:
        if ch in _ENTERS:
            text = self.pattern
            back = self._search_back
            self._searching = False
            self._buf.clear()
            return Command(
                Action.SEARCH_SUBMIT, text=text, backward=back, key=ch
            )
        if ch == 27 or ch == 0x03:  # Escape, Ctrl-C
            self._searching = False
            self._buf.clear()
            return Command(Action.SEARCH_CANCEL, backward=self._search_back, key=ch)
        if ch in _BACKSPACES:
            if not self._buf:
                self._searching = False
                return Command(
                    Action.SEARCH_CANCEL, backward=self._search_back, key=ch
                )
            # drop one *character*, not one byte
            txt = self.pattern[:-1]
            self._buf.clear()
            self._buf.extend(txt.encode("utf-8"))
            return Command(Action.SEARCH_TYPING, backward=self._search_back, key=ch)
        if ch == 0x15:  # Ctrl-U clears the line
            self._buf.clear()
            return Command(Action.SEARCH_TYPING, backward=self._search_back, key=ch)
        if 32 <= ch <= 255:
            self._buf.append(ch)
            return Command(Action.SEARCH_TYPING, backward=self._search_back, key=ch)
        if ch == curses.KEY_RESIZE:
            return Command(Action.RESIZE, key=ch)
        return Command(Action.SEARCH_TYPING, backward=self._search_back, key=ch)

    # -- the entry point --------------------------------------------------

    def feed(self, event: Any) -> Command:
        """Turn one event from `read_event` into a `Command`."""
        if event is None:
            return _NOOP

        if isinstance(event, MouseEvent):
            return self._feed_mouse(event)

        if not isinstance(event, int):
            return _NOOP
        ch = event

        if self._searching:
            return self._feed_search(ch)

        # numeric count prefix: 1-9 always starts one, 0 only continues one
        if 0x30 <= ch <= 0x39:
            if ch == 0x30 and not self._count:
                return _NOOP
            if len(self._count) < _MAX_COUNT_DIGITS:
                self._count += chr(ch)
            return Command(Action.NONE, key=ch)

        action = KEYMAP.get(ch)
        if action is None:
            self._count = ""
            return Command(Action.NONE, key=ch)

        if action is Action.SEARCH_FORWARD or action is Action.SEARCH_BACKWARD:
            self._count = ""
            self._searching = True
            self._search_back = action is Action.SEARCH_BACKWARD
            self._buf.clear()
            return Command(action, backward=self._search_back, key=ch)

        if action is Action.CANCEL:
            had = bool(self._count)
            self._count = ""
            return Command(Action.CANCEL, has_count=had, key=ch)

        count, has = self._take_count()
        if action is Action.SEARCH_PREV:
            return Command(action, count, has, backward=True, key=ch)
        return Command(action, count, has, key=ch)

    def _feed_mouse(self, ev: MouseEvent) -> Command:
        wheel = ev.wheel
        if wheel:
            self._count = ""
            action = Action.LINE_UP if wheel < 0 else Action.LINE_DOWN
            return Command(
                action, count=self.wheel_lines, y=ev.y, x=ev.x, mouse=True
            )
        # Every SGR press is followed by a release; acting on both would fire
        # click-to-jump twice.
        if ev.pressed and ev.button == 1:
            self._count = ""
            return Command(Action.CLICK_WORD, y=ev.y, x=ev.x, mouse=True)
        return _NOOP
