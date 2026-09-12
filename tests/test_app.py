"""Regression tests for `readaloud.app`: less-style counts, search, quit,
clicks, table follow keys, and startup notices.

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
    FakeEngine, FakePlayer, FakeScreen, crew_doc, make_doc, prose,
)

from readaloud import cli  # noqa: E402
from readaloud.app import MESSAGE_TTL, NOTICE_TTL, App  # noqa: E402


ESC = 27
CR = 13


def build(text=None, *, doc=None, height=24, autoplay=False):
    """An App on a fake screen, one display row per logical line."""
    if doc is None:
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


# --------------------------------------------------------------------------- #
# 6. a table read one cell at a time: clicks and the follow keys
#
# `crew_doc` puts `test_integration.CREW` (a real mdcat render) between lines
# of prose.  FakeScreen shows one row per line and one cell per char, and with
# one line of prose before it Bob's wrapped row is lines 5-7:
#
#    5  " Bob    Designer with a very      code  "
#    6  "        long title that wraps     here  "
#    7  "        around the column               "
# --------------------------------------------------------------------------- #


ROLE = "Designer with a very long title that wraps around the column"


def cell(doc, text):
    """The cell chunk whose text is `text`."""
    return next(c for c in doc.chunks if c.kind == "cell" and c.text == text)


def test_clicking_a_cells_padding_plays_that_cell():
    app, doc, _screen = build(doc=crew_doc(["Intro."], ["Outro."]))
    notes, role = cell(doc, "code here"), cell(doc, ROLE)
    here = next(i for i in notes.words if doc.words[i].text == "here")

    app._click(7, 36)                    # the Notes cell's blank third line
    assert app._target == (notes.idx, doc.slot_of_word(here))
    assert app.cur_word == here and app.want_play

    # right of "very", inside the Role column: the nearest word on the line
    # is the neighbour's "code", but the click is in Role
    very = next(i for i in role.words if doc.words[i].text == "very")
    app._click(5, 31)
    assert app._target == (role.idx, doc.slot_of_word(very))

    app._click(5, 33)                    # the gutter: the nearer column wins
    assert app._target == (notes.idx, 0)


def test_clicking_an_empty_cell_or_a_rule_does_nothing():
    app, doc, _screen = build(doc=crew_doc(["Intro."], ["Outro."]))
    assert doc.plain[8] == " Carol                            on    "
    for line, col in [(8, 12), (9, 20), (3, 5), (10, 39)]:
        app._click(line, col)
        assert app._target is None, (line, col)
        assert app.cur_word is None and not app.want_play


def test_a_click_never_starts_a_chunk_with_nothing_to_say():
    app, doc, _screen = build(doc=crew_doc(["Intro."], ["Outro."]))
    short = cell(doc, "short")
    short.speakable = False              # as if the Document had flagged it
    app._click(4, 36)
    assert app._target is None and not app.want_play
    short.speakable = True
    app._click(4, 36)
    assert app._target == (short.idx, 0)


def test_stepping_onto_a_wrapped_row_brings_the_whole_row_into_view():
    app, doc, screen = build(doc=crew_doc(prose(30), prose(40, "Outro")))
    body, margin = screen.body_height, app.follow_margin
    first, last = 34, 36                 # Bob's row after 30 lines of prose
    app._set_target(cell(doc, "short").idx, None)
    # Bob's first line sits on the bottom margin, his other two below it
    app.top = first - (body - 1 - margin)
    press(app, ".")
    assert app._target[0] == cell(doc, "Bob").idx
    assert app.follow
    assert app.top + margin <= first and last <= app.top + body - 1 - margin
    # ... and reading on across the row does not move the view again
    top = app.top
    for c in (cell(doc, "Bob"), cell(doc, ROLE), cell(doc, "code here")):
        for widx in c.words:
            app.cur_word = widx
            app.tick()
            assert app.top == top, doc.words[widx].text


def test_c_on_a_table_row_parks_the_view_where_follow_mode_leaves_it():
    app, doc, screen = build(doc=crew_doc(prose(30), prose(40, "Outro")))
    body, margin = screen.body_height, app.follow_margin
    first, last = 34, 36
    app.cur_word = next(w.idx for w in doc.words if w.text == "title")
    app.follow = False
    press(app, "c")
    assert not app.follow, "c looked at the row, it did not follow it"
    assert app.top + margin <= first and last <= app.top + body - 1 - margin
    before = app.top
    app.tick()
    assert app.top == before, "c left the view where follow mode moves it again"


# --------------------------------------------------------------------------- #
# 7. startup notices take turns in the status bar
#
# There used to be room for one: the rest became "(+N more)", so the first
# config warning hid every other notice.
# --------------------------------------------------------------------------- #


NOTICES = [
    "config: follow_lead: 'abc' is not a whole number; using 20",
    "config: speed: 9.0 is out of range; clamped to 3.0",
    "-md: table 1 is read line by line (a cell does not match its source text)",
]


class Clock:
    """`time.monotonic`, standing still until a test moves it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "monotonic", c)
    return c


def test_notices_take_turns_each_for_the_notice_ttl(clock):
    app, _doc, _screen = build()
    app.queue_notices(NOTICES)
    for notice in NOTICES:
        assert app.message == notice
        clock.advance(NOTICE_TTL - 0.01)
        assert app.message == notice
        clock.advance(0.02)
    assert app.message == ""


def test_notices_wait_for_the_message_already_up(clock):
    app, _doc, _screen = build()
    app.notify("media keys on", ttl=2.0)
    app.queue_notices(NOTICES[:1])
    assert app.message == "media keys on"
    clock.advance(2.01)
    assert app.message == NOTICES[0]


def test_a_keystroke_message_shows_in_between_and_the_notices_resume(clock):
    app, _doc, _screen = build(SEARCHDOC)
    app.queue_notices(NOTICES[:2])
    assert app.message == NOTICES[0]
    clock.advance(5.0)
    press(app, "/")
    press(app, [CR])                     # nothing to search for, and it says so
    assert app.message == "no previous search"
    press(app, "/")
    press(app, [CR])                     # said again: still one notice to resume
    clock.advance(MESSAGE_TTL + 0.01)
    # the notice it interrupted comes back, in full, before the next one
    assert app.message == NOTICES[0]
    clock.advance(NOTICE_TTL - 0.01)
    assert app.message == NOTICES[0]
    clock.advance(0.02)
    assert app.message == NOTICES[1]
    clock.advance(NOTICE_TTL + 0.01)
    assert app.message == ""


def test_escape_dismisses_the_notice_on_screen_and_the_next_one_follows(clock):
    app, _doc, _screen = build()
    app.queue_notices(NOTICES[:2])
    assert app.message == NOTICES[0]
    press(app, [ESC])
    assert app.message == NOTICES[1]
    press(app, [ESC])
    assert app.message == ""
    app.notify("speed 1.10x")            # nothing left over to come back
    clock.advance(MESSAGE_TTL + 0.01)
    assert app.message == ""


def test_run_shows_the_media_keys_error_then_every_notice_in_turn(
        monkeypatch, clock):
    from readaloud import app as app_mod
    from readaloud import mediakeys as mediakeys_mod
    from readaloud import player as player_mod
    from readaloud import speech as speech_mod

    doc = make_doc()
    screen = FakeScreen(doc)
    screen.events = [ord("q")]
    apps = []

    class Recorded(App):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            apps.append(self)

    @contextmanager
    def fake_session(theme=None):
        yield screen

    monkeypatch.setattr(speech_mod, "Engine", FakeEngine)
    monkeypatch.setattr(player_mod, "Player", lambda **kw: FakePlayer())
    monkeypatch.setattr(mediakeys_mod, "available", lambda: False)
    monkeypatch.setattr(app_mod, "App", Recorded)
    monkeypatch.setattr(app_mod, "screen_session", fake_session)

    assert app_mod.run(doc, media_keys=True, media_keys_explicit=True,
                       notices=NOTICES) == 0
    (app,) = apps
    shown = []
    while app.message:
        shown.append(app.message)
        clock.advance(NOTICE_TTL + 0.01)
    assert shown == ["media keys unavailable: install the [mediakeys] extra",
                     *NOTICES]
