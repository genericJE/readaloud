"""Regression tests for `readaloud.app`: less-style counts, search, quit.

These drive the real `App` through its real `Keymap`, with the fake
screen/player/engine from `test_integration` — the same fakes the rest of the
app-level suite uses, so a keystroke here goes through exactly the code path a
key from curses would.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_integration import (  # noqa: E402
    FakeEngine, FakePlayer, FakeScreen, make_doc,
)

from readaloud import cli  # noqa: E402
from readaloud.app import App  # noqa: E402


ESC = 27
CR = 13


def build(text=None, *, height=24, autoplay=False):
    """An App on a fake screen, one display row per logical line."""
    doc = (make_doc() if text is None
           else cli.build_document(text, max_sentences=4, max_chars=380,
                                   no_color=False))
    engine = FakeEngine()
    player = FakePlayer()
    screen = FakeScreen(doc, height=height)
    app = App(doc, screen, player, engine, ahead=0, voice="af_heart",
              autoplay=autoplay)
    return app, doc, screen


def press(app, keys) -> None:
    """Feed literal keystrokes through the real keymap."""
    for k in keys:
        cmd = app.keymap.feed(k if isinstance(k, int) else ord(k))
        if cmd:
            app.handle(cmd)


# --------------------------------------------------------------------------- #
# 1. a count before d/u/f/b is a line count, like `less`
# --------------------------------------------------------------------------- #


SCROLLDOC = "\n".join(f"Line number {i} of the scrolling document."
                      for i in range(1, 401)) + "\n"


def test_count_before_page_and_half_page_is_a_line_count():
    app, _doc, screen = build(SCROLLDOC, height=30)
    body = screen.body_height
    assert body > 5  # otherwise the multiplication bug would be invisible

    press(app, "3f")
    assert app.top == 3, "3f must scroll three lines, not three windows"

    press(app, "2d")
    assert app.top == 5

    press(app, "3u")
    assert app.top == 2

    press(app, "3b")
    assert app.top == 0


def test_a_count_given_to_d_becomes_the_new_default_for_d_and_u():
    # `less`: "If N is specified, it becomes the new default for subsequent
    # d and u commands."
    app, _doc, _screen = build(SCROLLDOC, height=30)
    press(app, "2d")
    assert app.top == 2
    press(app, "d")
    assert app.top == 4
    press(app, "u")
    assert app.top == 2
    press(app, "f")          # f is not affected by d's default
    assert app.top == 2 + _screen.body_height


def test_bare_page_keys_still_move_a_window():
    app, _doc, screen = build(SCROLLDOC, height=30)
    body = screen.body_height
    press(app, "f")
    assert app.top == body
    press(app, "d")
    assert app.top == body + max(1, body // 2)
    press(app, "u")
    assert app.top == body
    press(app, "b")
    assert app.top == 0


def test_count_before_j_and_k_is_unchanged():
    app, _doc, _screen = build(SCROLLDOC, height=30)
    press(app, "5j")
    assert app.top == 5
    press(app, "2k")
    assert app.top == 3


# --------------------------------------------------------------------------- #
# 2. `n` reaches trailing matches and wraps
# --------------------------------------------------------------------------- #


SEARCHDOC = "\n".join(
    f"This is paragraph {i} of the search test with the word zebra in it.\n"
    for i in range(1, 41)
)


def test_n_reaches_matches_in_the_last_screenful_and_wraps():
    app, doc, screen = build(SEARCHDOC, height=24)
    match_rows = sorted({screen.row_for(li, 0)
                         for li, text in enumerate(doc.plain)
                         if "zebra" in text})
    assert len(match_rows) >= 10
    # the last few matches live below max_top, which is what used to trap `n`
    assert match_rows[-1] > screen.max_top

    press(app, "/zebra")
    press(app, [CR])
    assert app.matches

    seen = [app._match_row]
    for _ in range(len(match_rows) + 2):
        press(app, "n")
        seen.append(app._match_row)

    # every match was visited, including the ones past max_top
    assert set(match_rows) <= set(seen)
    assert match_rows[-1] in seen
    # and the search wrapped instead of dying at the end
    last = seen.index(match_rows[-1])
    assert last < len(seen) - 1
    assert seen[last + 1] == match_rows[0], "n must wrap to the first match"
    assert app.message == "search wrapped to the top"


def test_n_wraps_in_a_document_shorter_than_the_screen():
    short = "alpha zebra one.\n\nbeta zebra two.\n\ngamma zebra three.\n"
    app, _doc, screen = build(short, height=24)
    assert screen.max_top == 0  # every row is on screen: top can never move

    press(app, "/zebra")
    press(app, [CR])
    first = app._match_row
    rows = [first]
    for _ in range(5):
        press(app, "n")
        rows.append(app._match_row)
    assert len(set(rows)) >= 3, "n must step between matches even when top is pinned"
    assert app.message in ("", "search wrapped to the top")


def test_scrolling_moves_where_the_next_n_searches_from():
    app, _doc, _screen = build(SEARCHDOC, height=24)
    press(app, "/zebra")
    press(app, [CR])
    for _ in range(6):
        press(app, "n")
    assert app._match_row > 0
    press(app, "g")                       # jump back to the top by hand
    assert app.top == 0
    press(app, "n")
    assert app._match_row < 12, "n must resume from the viewport after a scroll"


def test_backward_search_still_wraps():
    app, _doc, _screen = build(SEARCHDOC, height=24)
    press(app, "g")
    press(app, "?zebra")
    press(app, [CR])
    assert app.message == "search wrapped to the bottom"


# --------------------------------------------------------------------------- #
# 3. Escape must not break search-repeat
# --------------------------------------------------------------------------- #


def test_escape_dismisses_highlighting_but_keeps_search_repeat_alive():
    app, _doc, _screen = build(SEARCHDOC, height=24)
    press(app, "/zebra")
    press(app, [CR])
    assert app.matches
    assert app._visible_matches()

    press(app, [ESC])
    assert app._visible_matches() == [], "Escape should stop drawing the matches"
    assert app.message == ""

    before = app._match_row
    press(app, "n")
    assert app.message != "pattern not found"
    assert not app.message.startswith("pattern not found")
    assert app._match_row != before, "n must still move after Escape"
    assert app._visible_matches(), "repeating the search brings highlighting back"


def test_search_repeat_recovers_if_the_match_list_is_lost():
    app, _doc, _screen = build(SEARCHDOC, height=24)
    press(app, "/zebra")
    press(app, [CR])
    app.matches = []  # e.g. a re-layout dropped them
    before = app._match_row
    press(app, "n")
    assert app.matches, "n must recompute rather than report a false miss"
    assert not app.message.startswith("pattern not found")
    assert app._match_row != before


# --------------------------------------------------------------------------- #
# 4. `/` + Enter with nothing to search for says so
# --------------------------------------------------------------------------- #


def test_empty_search_with_no_previous_pattern_reports_it():
    app, _doc, _screen = build(SEARCHDOC, height=24)
    press(app, "/")
    press(app, [CR])
    assert app.message == "no previous search"


def test_empty_backward_search_with_no_previous_pattern_reports_it():
    app, _doc, _screen = build(SEARCHDOC, height=24)
    press(app, "?")
    press(app, [CR])
    assert app.message == "no previous search"


def test_empty_search_with_a_previous_pattern_repeats_it():
    app, _doc, _screen = build(SEARCHDOC, height=24)
    press(app, "/zebra")
    press(app, [CR])
    before = app._match_row
    press(app, "/")
    press(app, [CR])
    assert app.message != "no previous search"
    assert app._pattern == "zebra"
    assert app._match_row != before


# --------------------------------------------------------------------------- #
# 5. quitting never waits for the voice model
# --------------------------------------------------------------------------- #


class BlockingEngine(FakeEngine):
    """Mimics `speech.Engine`: `close()` waits on the lock `load()` holds."""

    def __init__(self, delay=4.0):
        super().__init__()
        self.delay = float(delay)
        self._lock = threading.Lock()
        self.load_started = threading.Event()
        self.closed = threading.Event()

    def load(self):
        with self._lock:
            self.load_started.set()
            time.sleep(self.delay)
            self.loaded = True

    def close(self):
        with self._lock:
            self.closed.set()
            self.loaded = False


class QuitScreen(FakeScreen):
    """Sends `q`, but only once the model load is genuinely in flight."""

    def __init__(self, doc, gate):
        super().__init__(doc)
        self._gate = gate

    def read_event(self, timeout_ms=50):
        if self._gate.wait(5.0):
            return ord("q")
        return None


def test_quit_does_not_wait_for_the_voice_model(monkeypatch):
    from readaloud import app as app_mod
    from readaloud import player as player_mod
    from readaloud import speech as speech_mod

    doc = make_doc()
    engine = BlockingEngine(delay=4.0)
    screen = QuitScreen(doc, engine.load_started)
    player = FakePlayer()

    monkeypatch.setattr(speech_mod, "Engine", lambda **kw: engine)
    monkeypatch.setattr(player_mod, "Player", lambda **kw: player)

    @contextmanager
    def fake_session(theme=None):
        yield screen

    monkeypatch.setattr(app_mod, "screen_session", fake_session)

    t0 = time.monotonic()
    status = app_mod.run(doc)
    elapsed = time.monotonic() - t0

    assert status == 0
    assert engine.load_started.is_set()
    assert elapsed < 1.5, (
        f"q waited {elapsed:.2f}s for a {engine.delay}s model load"
    )
    # the engine is released in the background, not on the way out
    assert not engine.closed.is_set()
    assert engine.closed.wait(15.0)
    assert player.closed


def test_close_engine_async_returns_immediately():
    from readaloud.app import _close_engine_async

    engine = BlockingEngine(delay=3.0)
    started = threading.Event()

    def hold():
        started.set()
        engine.load()

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert started.wait(2.0)
    assert engine.load_started.wait(2.0)

    t0 = time.monotonic()
    _close_engine_async(engine)
    assert time.monotonic() - t0 < 0.5
    assert engine.closed.wait(10.0)
    t.join(10.0)
