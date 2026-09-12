"""End-to-end tests for readaloud.cli and readaloud.app.

Three layers, cheapest first:

1. **CLI plumbing** — argument parsing, input selection, the four "exit cleanly
   with a message" paths, and ``--save`` against a stub engine.
2. **App state machine** — `App` driven with a fake screen/player/engine, so the
   playback logic, follow mode, search and click-to-jump are all deterministic
   and take milliseconds.
3. **The real thing under a pty** — `curses` cannot initialise without a tty and
   skipping the ``/dev/tty`` reopen fails *silently* (every ``getch()`` returns
   -1 forever), so the only honest check is to run the installed entry point
   with a pipe on stdin and a pseudo-terminal on stdout, then replay the escape
   stream through a small terminal emulator and look at the resulting screen.

The tests marked ``slow`` need the Kokoro weights and a working audio device.
Run the fast suite with ``-m "not slow"``.
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import wave
from typing import Sequence

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from readaloud import cli, config  # noqa: E402
from readaloud.app import (  # noqa: E402
    App, FOLLOW_LEAD, FOLLOW_MARGIN, MAX_SPEED, MIN_SPEED, Prefetcher,
    MEDIA_ELAPSED_EVERY,
)
from readaloud.document import Document, Table, TableCell  # noqa: E402
from readaloud.keys import Action, MouseEvent  # noqa: E402
from readaloud.speech import Spoken, Timed  # noqa: E402
from readaloud.ui import Row  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAMPLE = (
    "# Read Aloud Demo\n"
    "\n"
    "The quick brown fox jumps over the lazy dog. Pack my box with five dozen\n"
    "liquor jugs. How vexingly quick daft zebras jump.\n"
    "\n"
    "---\n"
    "\n"
    "A second paragraph follows here, with several more words in it.\n"
    "\n"
    "- bullet one\n"
    "- bullet two\n"
    "\n"
    "Final paragraph, short and sweet.\n"
)


#: 200 lines, so `max_top` is far from zero and scrolling is observable
LONG = "\n".join(
    f"Paragraph line number {i} with a few plain words on it."
    + ("\n" if i % 3 == 0 else "")
    for i in range(1, 201)
) + "\n"


def make_doc(text: str = SAMPLE) -> Document:
    return cli.build_document(text, max_sentences=4, max_chars=380, no_color=False)


#: `mdcat --ansi --columns 40` of
#:   | Name | Role | Notes |
#:   |------|------|-------|
#:   | Alice | Engineer | short |
#:   | Bob | Designer with a very long title that wraps around the column | code here |
#:   | Carol |  | on leave |
#: Bob's Role wraps over three lines and his Notes over two; Carol has no Role.
CREW = [
    "─" * 40,
    " Name   Role                      Notes ",
    "─" * 40,
    " Alice  Engineer                  short ",
    " Bob    Designer with a very      code  ",
    "        long title that wraps     here  ",
    "        around the column               ",
    " Carol                            on    ",
    "                                  leave ",
    "─" * 40,
]


def crew_doc(before: Sequence[str] = (), after: Sequence[str] = ()) -> Document:
    """A Document of `before`, the CREW table and `after`, as ``-md`` maps it.

    The Table is built by hand the way readaloud.markdown builds one: rows as
    line ranges, and every cell's column on every line of its row.  CREW is all
    single width, so its display columns are char offsets.
    """
    top = len(before)
    rows = [(1, 2), (3, 4), (4, 7), (7, 9)]
    cells = [TableCell(r, c, [(top + line, x, x + w) for line in range(a, b)])
             for r, (a, b) in enumerate(rows)
             for c, (x, w) in enumerate(zip([1, 8, 34], [5, 24, 5]))]
    table = Table(top, top + len(CREW), 3,
                  [(top + a, top + b) for a, b in rows], cells)
    doc = Document.from_text("\n".join([*before, *CREW, *after]),
                             tables=[table], references=False)
    assert doc.tables == [table], "the CREW table no longer fits its lines"
    return doc


def prose(n: int, name: str = "Intro") -> list[str]:
    """`n` one-line sentences, one display row each on a FakeScreen."""
    return [f"{name} line {i} has a few words." for i in range(n)]


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class FakeEngine:
    """Deterministic stand-in for `speech.Engine`: 0.1 s of audio per word."""

    def __init__(self, voice="af_heart", speed=1.0, lang_code="a", repo_id="",
                 fail_on=(), raise_on=(), load_error=None):
        self.voice = voice
        self.speed = float(speed)
        self.lang_code = lang_code
        self.repo_id = repo_id
        self.sample_rate = 24000
        self.last_error = None
        self.loaded = False
        self.load_calls = 0
        self.synth_calls: list[int] = []
        self._fail_on = set(fail_on)
        self._raise_on = set(raise_on)
        self._load_error = load_error

    def load(self):
        self.load_calls += 1
        if self._load_error:
            raise RuntimeError(self._load_error)
        self.loaded = True

    def close(self):
        self.loaded = False

    def synth(self, chunk):
        self.synth_calls.append(chunk.idx)
        if chunk.idx in self._raise_on:
            raise RuntimeError("synthesis exploded")
        n = len(chunk.words)
        if chunk.idx in self._fail_on:
            self.last_error = "boom"
            return Spoken(chunk.idx, np.zeros(0, dtype=np.float32), [])
        self.last_error = None
        per = 0.1 / max(0.1, self.speed)
        timings = [Timed(i, i * per, (i + 1) * per) for i in range(n)]
        frames = int(self.sample_rate * per * max(1, n))
        audio = np.full(frames, 0.001, dtype=np.float32)
        return Spoken(chunk.idx, audio, timings)


class FakePlayer:
    """`player.Player`'s surface, driven by an explicit clock instead of audio."""

    def __init__(self):
        self.spoken = None
        self._start = 0.0
        self.now = 0.0
        self._paused = False
        self.closed = False
        self.plays: list[tuple[int, float]] = []

    # -- the surface App uses ---------------------------------------------
    def play(self, spoken, start_time=0.0):
        self.spoken = spoken
        self._start = float(start_time)
        self.now = float(start_time)
        self._paused = False
        self.plays.append((spoken.chunk_idx, float(start_time)))

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self.spoken = None
        self._paused = False

    def close(self):
        self.closed = True
        self.spoken = None

    @property
    def position(self):
        return self.now

    @property
    def playing(self):
        return self.spoken is not None and not self._paused and not self.finished

    @property
    def finished(self):
        return self.spoken is not None and self.now >= self.spoken.duration

    @property
    def chunk_idx(self):
        return None if self.spoken is None else self.spoken.chunk_idx

    # -- test control ------------------------------------------------------
    def advance(self, seconds):
        self.now += seconds

    def finish(self):
        if self.spoken is not None:
            self.now = self.spoken.duration


class FakeScreen:
    """One display row per logical line — enough to exercise every App path."""

    def __init__(self, doc, width=80, height=24):
        self._w = width
        self._h = height
        self.top_row = 0
        self.events: list = []
        self.draws = 0
        self.invalidations = 0
        self.resizes = 0
        self.last_draw = None
        #: every (widx, top, margin, lead) follow mode asked for
        self.follow_calls: list[tuple] = []
        #: every (first_row, last_row, top, margin, lead) asked of top_for_span,
        #: a row's (row, row) included
        self.span_calls: list[tuple] = []
        self.layout(doc, width)

    # geometry
    @property
    def width(self):
        return self._w

    @property
    def height(self):
        return self._h

    @property
    def body_height(self):
        return self._h - 1

    @property
    def rows(self):
        return self._rows

    @property
    def max_top(self):
        return max(0, len(self._rows) - self.body_height)

    def clamp_top(self, t):
        return max(0, min(int(t), self.max_top))

    # layout
    def layout(self, doc, width=None):
        self._doc = doc
        plain = list(getattr(doc, "plain", []) or [])
        self._rows = [Row(i, 0, len(plain[i])) for i in range(len(plain))]
        return self._rows

    def handle_resize(self, doc=None):
        self.resizes += 1
        self.layout(doc if doc is not None else self._doc, self._w)
        return True

    def invalidate(self):
        self.invalidations += 1

    # maps
    def row_line_col(self, row):
        if 0 <= row < len(self._rows):
            r = self._rows[row]
            return (r.line, r.col_start)
        return None

    def first_row_of_line(self, line):
        return max(0, min(int(line), len(self._rows) - 1))

    def row_for(self, line, col=0):
        return self.first_row_of_line(line)

    def row_of_word(self, widx):
        w = self._doc.words[widx]
        return self.first_row_of_line(w.line)

    def word_position(self, widx):
        w = self._doc.words[widx]
        return (self.first_row_of_line(w.line), w.start)

    def top_for_word(self, widx, top_row, margin=2, lead=0):
        # mirrors `ui.Screen.top_for_word`, lead and all
        self.follow_calls.append((widx, top_row, margin, lead))
        row = self.row_of_word(widx)
        h = self.body_height
        m = min(margin, max(0, (h - 1) // 2))
        if row < top_row + m:
            return self.clamp_top(row - m)
        if row > top_row + h - 1 - m:
            return self.clamp_top(min(row - m, row - h + 1 + m + lead))
        return self.clamp_top(top_row)

    def follow_top_for_word(self, widx, margin=2, lead=0):
        # mirrors `ui.Screen.follow_top_for_word`: always the scroll-down branch
        row = self.row_of_word(widx)
        h = self.body_height
        m = min(margin, max(0, (h - 1) // 2))
        return self.clamp_top(min(row - m, row - h + 1 + m + lead))

    def follow_top_for_row(self, row, margin=2, lead=0):
        return self.follow_top_for_span(row, row, margin, lead)

    def top_for_span(self, first_row, last_row, top_row, margin=2, lead=0):
        # mirrors `ui.Screen.top_for_span`: react, do not reposition, and keep
        # a table row in view whole
        self.span_calls.append((first_row, last_row, top_row, margin, lead))
        h = self.body_height
        m = min(margin, max(0, (h - 1) // 2))
        if first_row < top_row + m:
            return self.clamp_top(first_row - m)
        if last_row > top_row + h - 1 - m:
            return self.clamp_top(min(first_row - m, last_row - h + 1 + m + lead))
        return self.clamp_top(top_row)

    def follow_top_for_span(self, first_row, last_row, margin=2, lead=0):
        h = self.body_height
        m = min(margin, max(0, (h - 1) // 2))
        return self.clamp_top(min(first_row - m, last_row - h + 1 + m + lead))

    def center_on_word(self, widx):
        return self.clamp_top(self.row_of_word(widx) - self.body_height // 2)

    def center_on_row(self, row):
        return self.clamp_top(row - self.body_height // 2)

    # hit testing
    def hit_test(self, y, x):
        loc = self.hit_test_line_col(y, x)
        if loc is None:
            return None
        return self._doc.word_at(*loc)

    def hit_test_line_col(self, y, x):
        row = self.top_row + y
        if y < 0 or y >= self.body_height or row >= len(self._rows):
            return None
        return (self._rows[row].line, x)

    # drawing / input
    def draw(self, doc, top_row, current_word=None, current_chunk=None,
             status=None, matches=None):
        self.draws += 1
        self.top_row = self.clamp_top(top_row)
        self.last_draw = (self.top_row, current_word, current_chunk, status,
                          list(matches or []))
        return 1

    def read_event(self, timeout_ms=50):
        return self.events.pop(0) if self.events else None


def make_app(doc=None, *, ahead=2, fail_on=(), raise_on=(), load_error=None,
             start_chunk=0, height=24, **kw):
    doc = doc or make_doc()
    engine = FakeEngine(fail_on=fail_on, raise_on=raise_on, load_error=load_error)
    player = FakePlayer()
    screen = FakeScreen(doc, height=height)
    app = App(doc, screen, player, engine, ahead=ahead,
              start_chunk=start_chunk, voice="af_heart", **kw)
    return app, doc, engine, player, screen


def settle(app, tries=60, want=None):
    """Tick until the prefetcher has delivered something (or `want` is true)."""
    for _ in range(tries):
        app.tick()
        if want is not None and want():
            return True
        if want is None and app._target is None and app._spoken is not None:
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------------- #
# 1. CLI plumbing
# --------------------------------------------------------------------------- #


def test_parser_defaults():
    """Every config-backed flag parses to None -- the sentinel that says "the
    user did not pass this", without which the config file could never lose."""
    args = cli.build_parser().parse_args([])
    assert args.voice is None
    assert args.speed is None
    assert args.sentences is None
    assert args.chars is None
    assert args.prefetch is None
    assert args.repo is None
    assert args.lang is None
    assert args.device is None
    assert args.color is None
    assert args.config is None
    assert args.no_config is False
    assert args.write_config is False
    # `--start` has no config counterpart, so it keeps a real default
    assert args.start == 1
    assert args.save is None


def test_parser_defaults_become_the_built_in_defaults():
    args = cli.build_parser().parse_args([])
    cli._apply_config(args, config.Config())
    assert (args.voice, args.speed, args.sentences) == ("af_heart", 1.0, 4)
    assert (args.chars, args.prefetch) == (380, 2)
    assert args.repo == "mlx-community/Kokoro-82M-4bit"
    assert args.lang is None and args.device is None   # "" means "work it out"
    assert args.color is True


def test_parser_flags():
    args = cli.build_parser().parse_args(
        ["hello", "world", "-v", "bf_emma", "-s", "1.5", "--prefetch", "0",
         "--device", "2", "--start", "7", "--no-color", "--save", "out.wav"]
    )
    assert args.text == ["hello", "world"]
    assert args.voice == "bf_emma"
    assert args.speed == 1.5
    assert args.prefetch == 0
    assert args.device == "2"
    assert args.start == 7
    assert args.color is False and args.save == "out.wav"


def test_parser_color_flags_are_two_spellings_of_one_setting():
    parse = cli.build_parser().parse_args
    assert parse([]).color is None
    assert parse(["--color"]).color is True
    assert parse(["--no-color"]).color is False
    # last one wins, like every other argparse flag
    assert parse(["--no-color", "--color"]).color is True


def test_parser_media_key_flags_are_two_spellings_of_one_setting():
    parse = cli.build_parser().parse_args
    assert parse([]).media_keys is None
    assert parse(["--media-keys"]).media_keys is True
    assert parse(["--no-media-keys"]).media_keys is False
    assert parse(["--media-keys", "--no-media-keys"]).media_keys is False


def test_media_keys_default_to_on():
    args = cli.build_parser().parse_args([])
    cli._apply_config(args, config.Config())
    assert args.media_keys is True


def test_document_name_is_the_file_basename_or_the_program_name():
    parse = cli.build_parser().parse_args
    assert cli.document_name(parse(["-f", "/tmp/notes.md"])) == "notes.md"
    assert cli.document_name(parse(["-f", "-"])) == "readaloud"
    assert cli.document_name(parse(["some text"])) == "readaloud"


def test_lang_derived_from_voice():
    assert cli.lang_for("af_heart", None) == "a"
    assert cli.lang_for("bm_george", None) == "b"
    assert cli.lang_for("jf_alpha", None) == "j"
    assert cli.lang_for("qq_unknown", None) == "a"
    assert cli.lang_for("af_heart", "b") == "b"


def test_read_input_prefers_file_then_text(tmp_path, monkeypatch):
    f = tmp_path / "doc.md"
    f.write_text("from the file\n", encoding="utf-8")
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "from stdin")

    args = cli.build_parser().parse_args(["-f", str(f), "ignored"])
    assert cli.read_input(args) == "from the file\n"

    args = cli.build_parser().parse_args(["some", "words"])
    assert cli.read_input(args) == "some words"

    args = cli.build_parser().parse_args([])
    assert cli.read_input(args) == "from stdin"

    args = cli.build_parser().parse_args(["-f", "-"])
    assert cli.read_input(args) == "from stdin"


def test_build_document_strips_markdown_but_keeps_ansi():
    plain = cli.build_document("# Heading\n\n**bold** text\n",
                               max_sentences=4, max_chars=380, no_color=False)
    assert "#" not in plain.plain[0]
    assert "**" not in "\n".join(plain.plain)

    coloured = "\x1b[1mBold\x1b[0m plain\n"
    doc = cli.build_document(coloured, max_sentences=4, max_chars=380,
                             no_color=False)
    assert doc.plain[0] == "Bold plain"
    assert any(r.style.bold for r in doc.lines[0])


def test_no_color_drops_colour_but_keeps_text():
    data = "\x1b[38;5;196mred\x1b[0m and \x1b[1mbold\x1b[0m\n"
    doc = cli.build_document(data, max_sentences=4, max_chars=380, no_color=True)
    assert doc.plain[0] == "red and bold"
    assert all(r.style.fg is None and r.style.bg is None for r in doc.lines[0])
    # bold survives; only colour is dropped
    assert any(r.style.bold for r in doc.lines[0])


def test_main_no_input_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "")
    assert cli.main([]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "no input" in err


def test_main_nothing_speakable_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr("readaloud.ui.read_stdin_text",
                        lambda *a, **k: "-------\n=======\n")
    assert cli.main([]) == cli.EXIT_ERROR
    assert "nothing speakable" in capsys.readouterr().err


def test_main_missing_file_is_an_error(capsys):
    assert cli.main(["-f", "/definitely/not/here.md"]) == cli.EXIT_ERROR
    assert "could not read" in capsys.readouterr().err


def test_main_rejects_bad_numbers(monkeypatch, capsys):
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "hello")
    assert cli.main(["--speed", "0"]) == cli.EXIT_USAGE
    assert cli.main(["--prefetch", "-1"]) == cli.EXIT_USAGE
    assert cli.main(["--chars", "0"]) == cli.EXIT_USAGE
    assert "must" in capsys.readouterr().err


def test_main_no_audio_device_is_a_message_not_a_traceback(monkeypatch, capsys):
    from readaloud import player as player_mod

    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "hello there")
    monkeypatch.setattr("readaloud.ui.reopen_tty", lambda: None)
    monkeypatch.setattr(os, "isatty", lambda fd: True)

    def boom(*a, **k):
        raise player_mod.PlayerError("could not open the audio default output device")

    monkeypatch.setattr(player_mod, "Player", boom)
    assert cli.main([]) == cli.EXIT_ERROR
    assert "could not open the audio" in capsys.readouterr().err


def test_main_without_a_controlling_tty_is_a_message(monkeypatch, capsys):
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "hello there")

    def enxio():
        raise OSError(errno.ENXIO, "Device not configured")

    monkeypatch.setattr("readaloud.ui.reopen_tty", enxio)
    assert cli.main([]) == cli.EXIT_ERROR
    assert "no controlling terminal" in capsys.readouterr().err


def test_main_keyboard_interrupt_becomes_130(monkeypatch, capsys):
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_main", boom)
    assert cli.main([]) == cli.EXIT_INTERRUPT
    assert "interrupted" in capsys.readouterr().err


def test_start_flag_counts_speakable_chunks(monkeypatch):
    """`--start N` must line up with the status bar's "chunk N/M"."""
    from readaloud import app as app_mod

    captured = {}

    def fake_run(doc, **kw):
        captured.update(kw)
        captured["doc"] = doc
        return 0

    monkeypatch.setattr(app_mod, "run", fake_run)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: SAMPLE)
    monkeypatch.setattr("readaloud.ui.reopen_tty", lambda: None)
    monkeypatch.setattr(os, "isatty", lambda fd: True)

    assert cli.main(["--start", "3"]) == 0
    doc = captured["doc"]
    speakable = doc.speakable_chunks
    assert len(speakable) >= 3
    assert captured["start_chunk"] == speakable[2]

    assert cli.main(["--start", "1"]) == 0
    assert captured["start_chunk"] == speakable[0]

    assert cli.main(["--start", "9999"]) == 0     # clamped, not an IndexError
    assert captured["start_chunk"] == speakable[-1]


def test_save_writes_a_valid_wav(tmp_path, monkeypatch):
    from readaloud import speech as speech_mod

    monkeypatch.setattr(speech_mod, "Engine", FakeEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: SAMPLE)
    out = tmp_path / "out.wav"
    assert cli.main(["--save", str(out)]) == cli.EXIT_OK

    with wave.open(str(out)) as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 24000
        frames = wf.getnframes()
        data = np.frombuffer(wf.readframes(frames), dtype="<i2")
    assert frames > 24000  # more than a second
    assert np.abs(data).max() > 0  # not silence


def test_save_reports_a_load_failure(tmp_path, monkeypatch, capsys):
    from readaloud import speech as speech_mod

    monkeypatch.setattr(speech_mod, "Engine",
                        lambda **kw: FakeEngine(load_error="no weights"))
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: SAMPLE)
    out = tmp_path / "out.wav"
    assert cli.main(["--save", str(out)]) == cli.EXIT_ERROR
    assert "could not load the voice model" in capsys.readouterr().err
    assert not out.exists()



# --------------------------------------------------------------------------- #
# 1b. ~/.readaloud.conf  ->  flags  ->  App
# --------------------------------------------------------------------------- #


def write_conf(tmp_path, body: str) -> str:
    """A config file with a real [readaloud] header, returned as a path str."""
    path = tmp_path / "readaloud.conf"
    path.write_text("[readaloud]\n" + body, encoding="utf-8")
    return str(path)


@pytest.fixture()
def known_voices(monkeypatch):
    """`--voice` validation, offline and deterministic."""
    voices = ["af_heart", "af_sarah", "am_adam", "bf_emma", "bm_george"]
    monkeypatch.setattr("readaloud.speech.list_voices",
                        lambda repo_id="r", lang_code=None, allow_download=False:
                        sorted(v for v in voices
                               if not lang_code or v[:1] == str(lang_code)[:1]))


@pytest.fixture()
def launched(monkeypatch, known_voices):
    """Intercept the TUI: `cli.main` returns the kwargs it would have run with."""
    from readaloud import app as app_mod

    seen: dict = {}

    def fake_run(doc, **kw):
        seen.clear()
        seen.update(kw)
        seen["doc"] = doc
        return 0

    monkeypatch.setattr(app_mod, "run", fake_run)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: SAMPLE)
    monkeypatch.setattr("readaloud.ui.reopen_tty", lambda: None)
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    return seen


def test_config_file_supplies_the_defaults(tmp_path, launched):
    conf = write_conf(tmp_path, "speed = 1.4\nvoice = bm_george\nprefetch = 5\n")
    assert cli.main(["--config", conf]) == 0
    assert launched["speed"] == 1.4
    assert launched["voice"] == "bm_george"
    assert launched["prefetch"] == 5
    assert launched["lang"] == "b"          # derived from the config's voice


def test_an_explicit_flag_beats_the_config_file(tmp_path, launched):
    conf = write_conf(tmp_path, "speed = 1.4\nvoice = bm_george\n")
    assert cli.main(["--config", conf, "--speed", "2.0"]) == 0
    assert launched["speed"] == 2.0         # the flag
    assert launched["voice"] == "bm_george"  # still the file, key by key


def test_a_flag_equal_to_the_built_in_default_still_beats_the_config(
        tmp_path, launched):
    """The `default=None` sentinel earns its keep here: `--speed 1.0` is
    indistinguishable from "not passed" if argparse fills in 1.0 itself."""
    conf = write_conf(tmp_path, "speed = 1.4\n")
    assert cli.main(["--config", conf, "--speed", "1.0"]) == 0
    assert launched["speed"] == 1.0


def test_no_config_ignores_the_file(tmp_path, launched):
    conf = write_conf(tmp_path, "speed = 1.4\nvoice = bm_george\n")
    assert cli.main(["--config", conf, "--no-config"]) == 0
    assert launched["speed"] == 1.0
    assert launched["voice"] == "af_heart"


def test_a_missing_config_file_is_not_an_error(tmp_path, launched, capsys):
    conf = str(tmp_path / "nowhere" / "no.conf")
    assert cli.main(["--config", conf]) == 0
    assert launched["speed"] == 1.0
    assert "could not" not in capsys.readouterr().err


def test_no_color_overrides_color_true_and_color_overrides_color_false(
        tmp_path, launched):
    conf_on = tmp_path / "on.conf"
    conf_on.write_text("[readaloud]\ncolor = true\n", encoding="utf-8")
    conf_off = tmp_path / "off.conf"
    conf_off.write_text("[readaloud]\ncolor = false\n", encoding="utf-8")

    assert cli.main(["--config", str(conf_on)]) == 0
    assert launched["no_color"] is False
    assert cli.main(["--config", str(conf_on), "--no-color"]) == 0
    assert launched["no_color"] is True
    assert cli.main(["--config", str(conf_off)]) == 0
    assert launched["no_color"] is True
    assert cli.main(["--config", str(conf_off), "--color"]) == 0
    assert launched["no_color"] is False


def test_no_media_keys_overrides_the_config_in_both_directions(tmp_path, launched):
    conf_on = tmp_path / "on.conf"
    conf_on.write_text("[readaloud]\nmedia_keys = true\n", encoding="utf-8")
    conf_off = tmp_path / "off.conf"
    conf_off.write_text("[readaloud]\nmedia_keys = false\n", encoding="utf-8")

    assert cli.main(["--config", str(conf_on)]) == 0
    assert launched["media_keys"] is True
    assert cli.main(["--config", str(conf_on), "--no-media-keys"]) == 0
    assert launched["media_keys"] is False
    assert cli.main(["--config", str(conf_off)]) == 0
    assert launched["media_keys"] is False
    assert cli.main(["--config", str(conf_off), "--media-keys"]) == 0
    assert launched["media_keys"] is True


def test_media_keys_are_on_by_default_and_the_document_gets_a_name(
        tmp_path, launched):
    doc = tmp_path / "notes.md"
    doc.write_text(SAMPLE, encoding="utf-8")
    assert cli.main(["--config", write_conf(tmp_path, ""), "-f", str(doc)]) == 0
    assert launched["media_keys"] is True
    assert launched["doc_name"] == "notes.md"


def test_config_follow_lead_reaches_the_app(tmp_path, launched):
    conf = write_conf(tmp_path, "follow_lead = 7\nfollow_margin = 3\n")
    assert cli.main(["--config", conf]) == 0
    assert launched["follow_lead"] == 7
    assert launched["follow_margin"] == 3


def test_follow_lead_defaults_to_twenty(tmp_path, launched):
    assert cli.main(["--config", write_conf(tmp_path, "")]) == 0
    assert launched["follow_lead"] == 20
    assert launched["follow_lead"] == FOLLOW_LEAD
    assert launched["follow_margin"] == FOLLOW_MARGIN


def test_config_warnings_reach_the_tui_as_notices_not_stdout(
        tmp_path, launched, capsys):
    """A print() here would land on top of the curses screen."""
    conf = write_conf(tmp_path, "speed = 9.0\nvolume = 11\n")
    assert cli.main(["--config", conf]) == 0
    out, err = capsys.readouterr()
    assert out == "" and err == ""
    notices = list(launched["notices"])
    assert any("speed" in n and "clamped" in n for n in notices)
    assert any("volume" in n for n in notices)
    # the path is stripped: it would fill an 80-column status bar on its own
    assert all(n.startswith("config: ") and str(conf) not in n for n in notices)
    assert launched["speed"] == 3.0                     # clamped, not refused


def test_config_warnings_go_to_stderr_for_save(tmp_path, monkeypatch, capsys,
                                               known_voices):
    from readaloud import speech as speech_mod

    monkeypatch.setattr(speech_mod, "Engine", FakeEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: SAMPLE)
    conf = write_conf(tmp_path, "speed = 9.0\n")
    out = tmp_path / "out.wav"
    assert cli.main(["--config", conf, "--save", str(out)]) == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "speed" in err and "clamped" in err


def test_an_unreadable_config_does_not_stop_the_run(tmp_path, launched):
    conf = tmp_path / "adirectory.conf"
    conf.mkdir()
    assert cli.main(["--config", str(conf)]) == 0
    assert launched["speed"] == 1.0
    assert any("could not be read" in n for n in launched["notices"])


def test_first_run_creates_the_config_and_says_so(tmp_path, launched, capsys):
    conf = tmp_path / "fresh.conf"
    assert cli.main(["--config", str(conf)]) == 0
    assert conf.exists()
    err = capsys.readouterr().err
    assert str(conf) in err and "created" in err

    # second run: the file is there, so nothing is said and nothing is clobbered
    conf.write_text("[readaloud]\nspeed = 1.3\n", encoding="utf-8")
    assert cli.main(["--config", str(conf)]) == 0
    assert capsys.readouterr().err == ""
    assert launched["speed"] == 1.3


def test_first_run_says_nothing_when_saving(tmp_path, monkeypatch, capsys,
                                            known_voices):
    """`--save` prints a machine-readable line; keep the chatter out of it."""
    from readaloud import speech as speech_mod

    monkeypatch.setattr(speech_mod, "Engine", FakeEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: SAMPLE)
    conf = tmp_path / "fresh.conf"
    assert cli.main(["--config", str(conf), "--save",
                     str(tmp_path / "o.wav")]) == cli.EXIT_OK
    assert conf.exists()                    # still created
    assert "created" not in capsys.readouterr().err


def test_no_config_does_not_create_a_file(tmp_path, launched, capsys):
    conf = tmp_path / "never.conf"
    assert cli.main(["--config", str(conf), "--no-config"]) == 0
    assert not conf.exists()


def test_the_default_path_is_used_when_no_config_flag_is_given(
        tmp_path, launched, monkeypatch):
    """`--config` is a detour; the plain run must read ~/.readaloud.conf."""
    conf = tmp_path / "home.conf"
    conf.write_text("[readaloud]\nvoice = bf_emma\n", encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_PATH", conf)
    assert cli.main([]) == 0
    assert launched["voice"] == "bf_emma"


def test_write_config_writes_a_template_that_parses_back_to_defaults(
        tmp_path, capsys):
    conf = tmp_path / "out.conf"
    assert cli.main(["--config", str(conf), "--write-config"]) == cli.EXIT_OK
    assert str(conf) in capsys.readouterr().out
    cfg, warnings = config.load(conf)
    assert warnings == []
    assert cfg == config.Config(
        pronunciations=config.TEMPLATE_PRONUNCIATIONS)
    # and every key is present, commented out, with its default spelled out
    text = conf.read_text(encoding="utf-8")
    for field in ("voice", "speed", "follow_lead", "follow_margin", "color"):
        assert f"#{field} = " in text or f"#{field} =" in text


def test_write_config_overwrites_and_exits_without_reading_input(tmp_path,
                                                                 capsys):
    conf = tmp_path / "out.conf"
    conf.write_text("nonsense that is not a config at all\n", encoding="utf-8")
    assert cli.main(["--config", str(conf), "--write-config"]) == cli.EXIT_OK
    assert conf.read_text(encoding="utf-8").startswith("# ~/.readaloud.conf")


def test_write_config_to_an_impossible_path_is_a_message(tmp_path, capsys):
    conf = tmp_path / "no-such-dir" / "out.conf"
    assert cli.main(["--config", str(conf), "--write-config"]) == cli.EXIT_ERROR
    assert "could not write" in capsys.readouterr().err


def test_config_speed_still_goes_through_the_flag_validation(tmp_path,
                                                             launched):
    """An absurd speed in the file is clamped by `config`, so the run starts;
    the same value on the command line is still a usage error."""
    conf = write_conf(tmp_path, "speed = 99\n")
    assert cli.main(["--config", conf]) == 0
    assert launched["speed"] == 3.0
    assert cli.main(["--config", conf, "--speed", "0"]) == cli.EXIT_USAGE


def test_config_voice_is_validated_like_the_flag(tmp_path, capsys, known_voices,
                                                 monkeypatch):
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: SAMPLE)
    conf = write_conf(tmp_path, "voice = af_sarahh\n")
    assert cli.main(["--config", conf]) == cli.EXIT_USAGE
    assert "unknown voice" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 2. Prefetcher
# --------------------------------------------------------------------------- #


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_prefetcher_synthesizes_current_plus_ahead():
    doc = make_doc()
    engine = FakeEngine()
    pf = Prefetcher(engine, doc, ahead=2)
    first = doc.first_speakable_chunk()
    pf.set_current(first)
    pf.start()
    try:
        assert wait_until(lambda: pf.ready.is_set())
        assert pf.loaded and pf.load_error is None
        assert wait_until(lambda: pf.get(first) is not None)
        nxt = doc.next_speakable_chunk(first)
        assert wait_until(lambda: pf.get(nxt) is not None)
        # it does not run away with the whole document
        assert wait_until(lambda: pf.cached >= 3)
        time.sleep(0.2)
        assert pf.cached <= pf.capacity
    finally:
        pf.stop()
        assert pf.join(3.0)


def test_prefetcher_reprioritises_on_a_jump():
    doc = make_doc()
    engine = FakeEngine()
    pf = Prefetcher(engine, doc, ahead=1)
    pf.set_current(doc.first_speakable_chunk())
    pf.start()
    try:
        assert wait_until(lambda: pf.ready.is_set())
        far = doc.speakable_chunks[-1]
        pf.set_current(far)
        assert wait_until(lambda: pf.get(far) is not None, timeout=5.0)
    finally:
        pf.stop()
        assert pf.join(3.0)


def test_prefetcher_survives_a_load_failure():
    doc = make_doc()
    engine = FakeEngine(load_error="model missing")
    pf = Prefetcher(engine, doc, ahead=2)
    pf.set_current(doc.first_speakable_chunk())
    pf.start()
    try:
        assert wait_until(lambda: pf.ready.is_set())
        assert not pf.loaded
        assert "model missing" in (pf.load_error or "")
        assert pf.get(0) is None
    finally:
        pf.stop()
        assert pf.join(3.0)


def test_prefetcher_invalidate_drops_the_cache():
    doc = make_doc()
    engine = FakeEngine()
    pf = Prefetcher(engine, doc, ahead=1)
    first = doc.first_speakable_chunk()
    pf.set_current(first)
    pf.start()
    try:
        assert wait_until(lambda: pf.get(first) is not None)
        engine.speed = 2.0
        pf.invalidate()
        assert wait_until(lambda: pf.get(first) is not None)
        assert engine.synth_calls.count(first) >= 2
    finally:
        pf.stop()
        assert pf.join(3.0)


def test_prefetcher_stops_promptly():
    doc = make_doc()
    pf = Prefetcher(FakeEngine(), doc, ahead=2)
    pf.set_current(doc.first_speakable_chunk())
    pf.start()
    assert wait_until(lambda: pf.ready.is_set())
    t0 = time.monotonic()
    pf.stop()
    assert pf.join(3.0)
    assert time.monotonic() - t0 < 3.0
    assert not pf.alive


# --------------------------------------------------------------------------- #
# 3. App state machine
# --------------------------------------------------------------------------- #


def test_app_starts_on_the_first_speakable_chunk():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        assert app._active == doc.first_speakable_chunk()
        assert player.plays and player.plays[0][0] == app._active
        assert player.playing
    finally:
        app.close()


def test_app_never_selects_an_unspeakable_chunk():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        seen = set()
        for _ in range(400):
            app.tick()
            if app._active is not None:
                seen.add(app._active)
            player.finish()
            time.sleep(0.005)
        assert seen
        assert all(doc.chunks[c].speakable for c in seen)
        assert all(doc.chunks[c].words for c in seen)
    finally:
        app.close()


def test_app_highlights_the_word_under_the_playhead():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        chunk = doc.chunks[app._active]
        app.tick()
        first = app.cur_word
        assert first == chunk.words[0]
        slot = min(3, len(chunk.words) - 1)
        assert slot >= 1, "the first speakable chunk should have several words"
        player.advance(slot * 0.1 + 0.05)
        app.tick()
        assert app.cur_word == chunk.words[slot]
        assert app.cur_word != first
    finally:
        app.close()


def test_app_advances_to_the_next_chunk_when_one_finishes():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        first = app._active
        player.finish()
        assert settle(app, want=lambda: app._active != first)
        assert app._active == doc.next_speakable_chunk(first)
    finally:
        app.close()


def test_app_stops_at_the_end_of_the_document():
    app, doc, engine, player, screen = make_app(
        start_chunk=make_doc().speakable_chunks[-1])
    app.start()
    try:
        assert settle(app)
        assert app._active == doc.speakable_chunks[-1]
        player.finish()
        for _ in range(20):
            app.tick()
        assert app.want_play is False
        assert "end of document" in app.message
    finally:
        app.close()


def test_app_skips_a_chunk_whose_synthesis_fails():
    doc = make_doc()
    first = doc.first_speakable_chunk()
    app, doc, engine, player, screen = make_app(doc=doc, fail_on={first})
    app.start()
    try:
        assert settle(app, want=lambda: app._spoken is not None)
        assert app._active == doc.next_speakable_chunk(first)
        assert "failed" in app.message
    finally:
        app.close()


def test_app_survives_a_chunk_whose_synthesis_raises():
    """Non-negotiable: `synth` blowing up must not kill the reader."""
    doc = make_doc()
    first = doc.first_speakable_chunk()
    app, doc, engine, player, screen = make_app(doc=doc, raise_on={first})
    app.start()
    try:
        assert settle(app, want=lambda: app._spoken is not None)
        assert app._active == doc.next_speakable_chunk(first)
        assert "synthesis exploded" in (app.prefetch.synth_error or "")
        assert player.playing
    finally:
        app.close()


def test_app_survives_a_document_where_every_chunk_fails():
    doc = make_doc()
    app, doc, engine, player, screen = make_app(
        doc=doc, fail_on=set(doc.speakable_chunks))
    app.start()
    try:
        for _ in range(300):
            app.tick()
            time.sleep(0.003)
        assert app.want_play is False
        assert app._spoken is None
    finally:
        app.close()


def test_scrolling_turns_follow_off_and_f_turns_it_back_on():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app(doc=make_doc(LONG))
    app.start()
    try:
        assert settle(app)
        assert app.follow is True
        app.handle(Command(Action.LINE_DOWN, count=3))
        assert app.follow is False
        assert app.top == 3
        app.handle(Command(Action.TOGGLE_FOLLOW))
        assert app.follow is True
        app.handle(Command(Action.PAGE_DOWN))
        assert app.follow is False
        app.handle(Command(Action.CENTER))
        assert app.follow is True
    finally:
        app.close()


# ---- follow-mode scroll lead ---------------------------------------------


def play_and_watch(app, player, until_top=120, ticks=4000, step=0.2):
    """Run playback until the viewport has scrolled to `until_top`.

    Returns the ``row_of_word(cur_word) - top`` offset seen at every tick: 0 is
    the top body row, ``body_height - 1`` the last one.  Sampling stops at
    `until_top` so the end-of-document clamp -- where `clamp_top` refuses to
    scroll further and the word does drift past the bottom margin -- cannot
    contaminate the measurement.
    """
    offsets = []
    for _ in range(ticks):
        app.tick()
        if app.top >= min(until_top, app.screen.max_top):
            break
        # Only once the view has actually scrolled: before the first scroll the
        # word sits wherever the document starts, which says nothing about the
        # follow policy.
        if app.cur_word is not None and app.top > 0:
            offsets.append(app.screen.row_of_word(app.cur_word) - app.top)
        player.advance(step)
        time.sleep(0.001)
    return offsets


def test_follow_without_a_lead_pins_the_word_to_the_bottom_margin():
    """The behaviour the lead exists to fix: a smallest-possible scroll leaves
    the reading position on the last usable row, hiding what comes next."""
    app, doc, engine, player, screen = make_app(doc=make_doc(LONG), height=30,
                                                follow_lead=0)
    app.start()
    try:
        assert settle(app)
        offsets = play_and_watch(app, player)
        assert app.top >= 120, "playback never got far enough to scroll"
        bottom = screen.body_height - 1 - FOLLOW_MARGIN     # 26 on a 30-row term
        assert max(offsets) == bottom
        # once it has scrolled at all, the word never leaves that row again
        tail = offsets[len(offsets) // 2:]
        assert set(tail) == {bottom}, sorted(set(tail))
    finally:
        app.close()


def test_follow_lead_puts_the_word_near_the_top_and_shows_what_is_coming():
    app, doc, engine, player, screen = make_app(doc=make_doc(LONG), height=30,
                                                follow_lead=20)
    app.start()
    try:
        assert settle(app)
        offsets = play_and_watch(app, player)
        assert app.top >= 120, "playback never got far enough to scroll"
        h = screen.body_height                              # 29
        landing = h - 1 - FOLLOW_MARGIN - 20                 # 6
        assert min(offsets) == landing
        assert max(offsets) <= h - 1 - FOLLOW_MARGIN
        # the point of the whole feature: rows of unread text below the word
        below = h - 1 - landing
        assert below >= 20, below
    finally:
        app.close()


def test_the_lead_leaves_more_text_visible_than_no_lead():
    """The two runs side by side, which is the claim the feature makes."""
    seen = {}
    for lead in (0, 20):
        app, doc, engine, player, screen = make_app(doc=make_doc(LONG),
                                                    height=30, follow_lead=lead)
        app.start()
        try:
            assert settle(app)
            offsets = play_and_watch(app, player)
            seen[lead] = sum(offsets) / len(offsets)
        finally:
            app.close()
    assert seen[20] < seen[0] - 5, seen


def test_the_lead_never_pushes_the_spoken_word_off_the_top():
    """A lead far larger than the screen must saturate, not overscroll."""
    app, doc, engine, player, screen = make_app(doc=make_doc(LONG), height=24,
                                                follow_lead=500)
    app.start()
    try:
        assert settle(app)
        offsets = play_and_watch(app, player)
        assert app.top >= 120
        assert min(offsets) == FOLLOW_MARGIN        # parked at the top margin
        assert all(o >= 0 for o in offsets)
    finally:
        app.close()


def test_the_app_passes_its_lead_and_margin_to_the_screen():
    app, doc, engine, player, screen = make_app(doc=make_doc(LONG),
                                                follow_lead=7, follow_margin=3)
    app.start()
    try:
        assert settle(app)
        play_and_watch(app, player, until_top=1)
        assert screen.follow_calls
        assert {(m, lead) for _, _, m, lead in screen.follow_calls} == {(3, 7)}
    finally:
        app.close()


def test_the_default_lead_is_the_module_constant():
    app, doc, engine, player, screen = make_app(doc=make_doc(LONG))
    app.start()
    try:
        assert (app.follow_lead, app.follow_margin) == (FOLLOW_LEAD,
                                                        FOLLOW_MARGIN)
        assert settle(app)
        play_and_watch(app, player, until_top=1)
        assert {lead for _, _, _, lead in screen.follow_calls} == {FOLLOW_LEAD}
    finally:
        app.close()


def test_c_parks_the_view_where_follow_mode_would():
    """`c` is "put me back where the reading is", so it uses the follow-mode
    placement (lead and all) rather than centring.  The point is that the view
    must NOT jump again on the very next tick."""
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app(doc=make_doc(LONG), height=30,
                                                follow_lead=20)
    app.start()
    try:
        assert settle(app)
        play_and_watch(app, player)
        assert app.top >= 120
        app.handle(Command(Action.CENTER))
        assert app.follow is True
        assert app.top == screen.follow_top_for_word(app.cur_word, 2, 20)
        # the spoken word sits near the top with the upcoming text below it,
        # NOT in the middle -- that is what distinguishes `c` from `F` now
        offset = screen.row_of_word(app.cur_word) - app.top
        assert offset < screen.body_height // 2, (
            f"c centred the word (offset {offset}) instead of using the lead")
        # and the placement is already stable: one tick changes nothing
        before = app.top
        app.tick()
        assert app.top == before, "c left the view somewhere follow mode would move again"
    finally:
        app.close()


def test_F_still_centres_and_differs_from_c():
    """`F` keeps its old behaviour, so the two keys are usefully different."""
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app(doc=make_doc(LONG), height=30,
                                                follow_lead=20)
    app.start()
    try:
        assert settle(app)
        play_and_watch(app, player)
        app.handle(Command(Action.TOGGLE_FOLLOW))   # off
        assert app.follow is False
        app.handle(Command(Action.TOGGLE_FOLLOW))   # on -> centres
        assert app.follow is True
        centred = app.top
        assert centred == screen.center_on_word(app.cur_word)
        app.handle(Command(Action.CENTER))
        assert app.top == screen.follow_top_for_word(app.cur_word, 2, 20)
        assert app.top != centred, "c and F ended up identical"
    finally:
        app.close()


def test_scrolling_does_not_touch_playback():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        active, plays = app._active, len(player.plays)
        for action in (Action.LINE_DOWN, Action.PAGE_DOWN, Action.BOTTOM,
                       Action.TOP, Action.HALF_PAGE_DOWN, Action.HALF_PAGE_UP):
            app.handle(Command(action))
        assert app._active == active
        assert len(player.plays) == plays
        assert player.playing
    finally:
        app.close()


def test_top_and_bottom_and_counts():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app(doc=make_doc(LONG))
    app.start()
    try:
        app.handle(Command(Action.BOTTOM))
        assert app.top == screen.max_top
        app.handle(Command(Action.TOP))
        assert app.top == 0
        app.handle(Command(Action.TOP, count=5, has_count=True))
        assert app.top == 4
    finally:
        app.close()


def test_next_and_prev_chunk_keys():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        first = app._active
        app.handle(Command(Action.NEXT_CHUNK))
        assert app._target[0] == doc.next_speakable_chunk(first)
        assert settle(app)
        second = app._active
        app.handle(Command(Action.PREV_CHUNK))
        assert app._target[0] == first
        assert second != first
    finally:
        app.close()


def test_click_jumps_playback_to_that_word():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        # pick a word late in the document
        target = doc.words[-1]
        row = screen.first_row_of_line(target.line)
        screen.top_row = screen.clamp_top(row)
        app.top = screen.top_row
        y = row - screen.top_row
        app._click(y, target.start)
        widx = doc.word_at(target.line, target.start)
        assert widx is not None
        assert app._target[0] == doc.chunk_of_word(widx)
        assert app._target[1] == doc.slot_of_word(widx)
        assert app.want_play is True
        assert settle(app)
        # it seeks inside the chunk rather than restarting it
        cidx, start = player.plays[-1]
        assert cidx == doc.chunk_of_word(widx)
        assert start == pytest.approx(0.1 * doc.slot_of_word(widx), abs=1e-6)
    finally:
        app.close()


def test_click_on_empty_space_is_ignored():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        before = app._active
        app._click(None, None)
        app._click(9999, 9999)
        assert app._target is None
        assert app._active == before
    finally:
        app.close()


def test_play_pause_toggles():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        assert player.playing
        app.handle(Command(Action.PLAY_PAUSE))
        assert app.want_play is False
        assert not player.playing
        app.handle(Command(Action.PLAY_PAUSE))
        assert app.want_play is True
        assert player.playing
    finally:
        app.close()


def test_speed_keys_change_the_engine_and_drop_the_cache():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        n = len(engine.synth_calls)
        app.handle(Command(Action.SPEED_UP))
        assert engine.speed == pytest.approx(1.1)
        assert app._target is not None          # re-queued at the current word
        assert settle(app)
        assert len(engine.synth_calls) > n
        for _ in range(40):
            app.handle(Command(Action.SPEED_DOWN))
        assert engine.speed == pytest.approx(MIN_SPEED)
        for _ in range(60):
            app.handle(Command(Action.SPEED_UP))
        assert engine.speed == pytest.approx(MAX_SPEED)
    finally:
        app.close()


def test_search_moves_the_viewport_only():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        assert settle(app)
        active = app._active
        app._search("vexingly", False)
        assert app.matches
        line = app.matches[0][0]
        assert "vexingly" in doc.plain[line]
        assert app.follow is False
        assert app._active == active            # playback untouched
        app._search("no-such-text-anywhere", False)
        assert app.matches == []
        assert "not found" in app.message
    finally:
        app.close()


def test_search_is_smart_case_and_repeats():
    from readaloud.keys import Command

    # long enough that the viewport can actually move
    lines = ["alpha filler line %d." % i for i in range(200)]
    lines[3] = "alpha beta three."
    lines[30] = "BETA thirty gamma."
    lines[55] = "delta beta fifty five."
    app, doc, engine, player, screen = make_app(doc=make_doc("\n".join(lines) + "\n"))
    app.start()
    try:
        app._search("beta", False)               # lowercase -> case insensitive
        assert {m[0] for m in app.matches} == {3, 30, 55}
        assert app.top == 3                      # jumped to the first hit below 0
        app.handle(Command(Action.SEARCH_NEXT))
        assert app.top == 30
        app.handle(Command(Action.SEARCH_NEXT))
        assert app.top == 55
        app.handle(Command(Action.SEARCH_PREV))
        assert app.top == 30
        app._search("BETA", False)               # has a capital -> exact
        assert {m[0] for m in app.matches} == {30}
    finally:
        app.close()


def test_search_pattern_that_is_not_a_regex_is_taken_literally():
    app, doc, engine, player, screen = make_app(doc=make_doc("a (b c\nplain\n"))
    app.start()
    try:
        app._search("(b", False)                 # unbalanced paren: not a regex
        assert app.matches and app.matches[0][0] == 0
    finally:
        app.close()


def test_resize_keeps_the_viewport_anchored():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        app.handle(Command(Action.LINE_DOWN, count=4))
        anchor = screen.row_line_col(app.top)
        app.handle(Command(Action.RESIZE))
        assert screen.resizes == 1
        assert screen.row_line_col(app.top) == anchor
    finally:
        app.close()


def test_redraw_forces_a_repaint():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        sig = app.signature()
        app.handle(Command(Action.REDRAW))
        assert screen.invalidations >= 1
        assert app.signature() != sig
    finally:
        app.close()


def test_quit_sets_the_flag_and_run_returns():
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        screen.events = [ord("j"), ord("j"), ord("q")]
        app.run()
        assert app.quit is True
        assert screen.draws >= 1
    finally:
        assert app.close(2.0)


def test_run_redraws_only_when_something_changed():
    app, doc, engine, player, screen = make_app()
    app.start()
    try:
        settle(app)
        app.draw()
        n = screen.draws
        for _ in range(20):                      # idle ticks, nothing moving
            sig = app.signature()
            app.step(0)
            if app.signature() != sig:
                app.draw()
        assert screen.draws - n <= 5             # not one paint per poll
    finally:
        app.close()


def test_status_bar_reports_loading_then_playing():
    app, doc, engine, player, screen = make_app()
    st = app.status()
    assert st.loading is True
    assert "loading voice" in st.message
    app.start()
    try:
        assert settle(app)
        st = app.status()
        assert st.loading is False
        assert st.playing is True
        assert st.voice == "af_heart"
        assert st.nchunks == len(doc.speakable_chunks)
        assert st.speed == pytest.approx(1.0)
    finally:
        app.close()


def test_status_reports_a_load_failure():
    app, doc, engine, player, screen = make_app(load_error="no weights")
    app.start()
    try:
        assert wait_until(lambda: app.prefetch.ready.is_set())
        app.tick()
        assert "failed to load" in app.status().message
    finally:
        app.close()


def test_empty_document_is_survivable():
    doc = make_doc("\n\n---\n\n")
    assert doc.speakable_chunks == []
    app, doc, engine, player, screen = make_app(doc=doc)
    app.start()
    try:
        for _ in range(20):
            app.tick()
        app.draw()
        assert app._active is None
        assert app.want_play is False
    finally:
        app.close()


def test_mouse_wheel_scrolls_and_disables_follow():
    from readaloud.keys import Keymap

    app, doc, engine, player, screen = make_app(doc=make_doc(LONG))
    app.start()
    try:
        km = Keymap(wheel_lines=3)
        cmd = km.feed(MouseEvent(button=5, x=1, y=1, pressed=True))
        assert cmd.action is Action.LINE_DOWN and cmd.count == 3
        app.handle(cmd)
        assert app.top == 3
        assert app.follow is False
    finally:
        app.close()


def test_app_close_joins_the_worker():
    app, doc, engine, player, screen = make_app()
    app.start()
    settle(app)
    assert app.close(3.0) is True
    assert app.prefetch.alive is False


# --------------------------------------------------------------------------- #
# 3b. the system play/pause button
#
# The real button cannot be pressed from a test, so `mediakeys.MediaKeys` is
# replaced by a stub that hands the app scripted commands.  What is being
# tested is the half readaloud owns: the mapping from a MediaRemote command to
# a playback state, and the state reporting that keeps the button alternating.
# --------------------------------------------------------------------------- #


class FakeMediaKeys:
    """`mediakeys.MediaKeys`'s surface, with a scriptable command queue."""

    def __init__(self, *, ok=True, error=None, explode=False):
        self.ok = ok
        self.error = error
        self.explode = explode
        self.started = False
        self.stops = 0
        self.pumps = 0
        self.polls = 0
        self.queue: list[str] = []
        self.playing_calls: list[bool] = []
        self.titles: list[str] = []
        self.scrubs: list[tuple] = []   # every (elapsed, duration, rate) push

    # -- the surface App uses ---------------------------------------------
    def start(self, duration=0.0):
        if self.explode:
            raise RuntimeError("no NSApplication for you")
        self.started = bool(self.ok)
        return self.started

    def stop(self):
        self.stops += 1
        self.started = False

    def pump(self, seconds=0.0):
        self.pumps += 1

    def poll(self):
        self.polls += 1
        out, self.queue = self.queue, []
        return out

    def set_playing(self, playing):
        self.playing_calls.append(bool(playing))

    def set_now_playing(self, title=None, *, elapsed=None, duration=None,
                        rate=None):
        # Mirror the real contract: `title=None` means "leave the title alone",
        # which is what the periodic scrubber refresh sends.  Recording those
        # as titles would both corrupt `titles[-1]` and make the
        # republished-only-on-change assertions count position updates.
        if title is not None:
            self.titles.append(title)
        self.scrubs.append((elapsed, duration, rate))

    # -- test control ------------------------------------------------------
    def press(self, *commands):
        """Queue what MediaRemote would deliver on the next pump."""
        self.queue.extend(commands)


def media_app(**kw):
    keys = FakeMediaKeys(**kw)
    app, doc, engine, player, screen = make_app(media_keys=keys)
    app.start()
    return app, keys, player


def test_the_button_is_claimed_at_startup_and_released_on_quit():
    app, keys, _player = media_app()
    try:
        assert keys.started is True
        assert "media keys" in app.status().message
    finally:
        app.close()
    assert keys.stops >= 1
    assert keys.started is False


def test_a_button_that_cannot_be_claimed_is_a_message_not_a_failure():
    app, keys, player = media_app(ok=False, error="MediaRemote said no")
    try:
        assert settle(app)
        assert player.playing, "the reader must still read"
        assert "MediaRemote said no" in app.status().message
    finally:
        app.close()


def test_a_button_that_raises_on_start_is_dropped_and_the_reader_runs():
    app, keys, player = media_app(explode=True)
    try:
        assert app.media_keys is None      # dropped, so nothing pumps a corpse
        assert "media keys unavailable" in app.status().message
        assert settle(app)
        assert player.playing
    finally:
        app.close()


def test_a_play_arriving_while_playing_is_the_button_asking_for_a_pause():
    """The press that should pause arrives as 'play', not 'pause' — treating
    it as "resume" is what makes the button feel one-way."""
    app, keys, player = media_app()
    try:
        assert settle(app)
        assert app.want_play is True and player.playing
        keys.press("play")
        app.tick()
        assert app.want_play is False
        assert not player.playing
        assert keys.playing_calls[-1] is False
    finally:
        app.close()


def test_a_play_arriving_while_paused_resumes():
    app, keys, player = media_app()
    try:
        assert settle(app)
        keys.press("play")
        app.tick()
        assert app.want_play is False
        keys.press("play")
        app.tick()
        assert app.want_play is True
        assert player.playing
        assert keys.playing_calls[-1] is True
    finally:
        app.close()


def test_pause_pauses_and_a_second_pause_changes_nothing():
    app, keys, player = media_app()
    try:
        assert settle(app)
        keys.press("pause")
        app.tick()
        assert app.want_play is False
        n = len(keys.playing_calls)
        keys.press("pause")
        app.tick()
        assert app.want_play is False
        assert len(keys.playing_calls) == n, "nothing changed; nothing to report"
    finally:
        app.close()


def test_toggle_toggles():
    app, keys, player = media_app()
    try:
        assert settle(app)
        for expected in (False, True, False):
            keys.press("toggle")
            app.tick()
            assert app.want_play is expected
        assert keys.playing_calls[-3:] == [False, True, False]
    finally:
        app.close()


def test_next_and_previous_move_a_chunk():
    app, keys, player = media_app()
    doc = app.doc
    try:
        assert settle(app)
        first = app._active
        keys.press("next")
        app.tick()
        assert app._current_chunk() == doc.next_speakable_chunk(first)
        assert settle(app)
        keys.press("previous")
        app.tick()
        assert app._current_chunk() == first
    finally:
        app.close()


def test_several_commands_in_one_pump_are_all_applied():
    app, keys, player = media_app()
    try:
        assert settle(app)
        keys.press("pause", "play")        # off, then back on
        app.tick()
        assert app.want_play is True
    finally:
        app.close()


def test_the_state_is_reported_after_a_spacebar_press_too():
    """Not just after a button press: macOS keeps sending 'play' until the
    state it holds matches ours, whatever moved ours."""
    from readaloud.keys import Command

    app, keys, player = media_app()
    try:
        assert settle(app)
        assert keys.playing_calls == [True]
        app.handle(Command(Action.PLAY_PAUSE))     # the spacebar
        app.tick()
        assert keys.playing_calls == [True, False]
        app.handle(Command(Action.PLAY_PAUSE))
        app.tick()
        assert keys.playing_calls == [True, False, True]
    finally:
        app.close()


def test_the_state_is_reported_when_the_document_ends():
    app, keys, player = media_app()
    try:
        assert settle(app)
        app._set_target(app.doc.speakable_chunks[-1], None)   # the last chunk
        assert settle(app)
        player.finish()
        app.tick()                                 # ... and run off the end
        assert "end of document" in app.status().message
        assert app.want_play is False
        assert keys.playing_calls[-1] is False
    finally:
        app.close()


def test_the_title_is_the_chunk_trimmed_and_the_artist_is_the_document():
    app, keys, player = media_app()
    try:
        assert settle(app)
        assert keys.titles, "Control Center was never told what is playing"
        title = keys.titles[-1]
        assert len(title) <= 70
        assert "\n" not in title
        assert title.split()[0] in app.doc.chunks[app._active].text
    finally:
        app.close()


def test_the_title_is_republished_only_when_the_chunk_changes():
    """`tick` runs ~33 times a second; MediaRemote does not need to hear the
    same paragraph 33 times."""
    app, keys, player = media_app()
    try:
        assert settle(app)
        before = len(keys.titles)
        assert before <= 2
        for _ in range(100):
            app.tick()
        assert len(keys.titles) == before, "the title was republished while idle"
        assert keys.pumps >= 100, "the run loop still has to be pumped"
        keys.press("next")
        app.tick()
        assert settle(app)
        assert len(keys.titles) == before + 1
    finally:
        app.close()


def test_the_playing_flag_is_not_republished_while_nothing_changes():
    app, keys, player = media_app()
    try:
        assert settle(app)
        n = len(keys.playing_calls)
        for _ in range(100):
            app.tick()
        assert len(keys.playing_calls) == n
    finally:
        app.close()


def test_a_long_chunk_is_trimmed_at_a_word_boundary():
    from readaloud.app import MEDIA_TITLE_CHARS

    long_para = ("Alpha bravo charlie delta echo foxtrot golf hotel india "
                 "juliet kilo lima mike november oscar papa quebec romeo.\n")
    app, doc, engine, player, screen = make_app(doc=make_doc(long_para),
                                                media_keys=FakeMediaKeys())
    keys = app.media_keys
    app.start()
    try:
        assert settle(app)
        title = keys.titles[-1]
        assert title.endswith("...")
        assert len(title) <= MEDIA_TITLE_CHARS + 3
        assert not title[:-3].endswith(" ")
        assert long_para.startswith(title[:-3])
    finally:
        app.close()


def test_an_app_without_media_keys_never_touches_them():
    app, doc, engine, player, screen = make_app()
    assert app.media_keys is None
    app.start()
    try:
        assert settle(app)
        for _ in range(20):
            app.tick()
        assert player.playing
    finally:
        app.close()


# --------------------------------------------------------------------------- #
# 3c. a table read one cell at a time (readaloud -md)
#
# The App knows nothing about Markdown: it plays whatever chunks the Document
# hands it.  What it does own is follow mode, which has to cope with a table
# row whose cells are read left to right while their lines interleave.
# --------------------------------------------------------------------------- #


def test_an_app_reads_a_table_one_cell_at_a_time():
    doc = crew_doc(["The table below lists the crew.", ""],
                   ["", "That was the crew."])
    app, doc, engine, player, screen = make_app(doc=doc)
    app.start()
    try:
        assert settle(app)
        heard = [doc.chunks[app._active].text]
        while True:
            before = app._active
            player.finish()
            assert settle(app, want=lambda: (app._active != before
                                             or not app.want_play))
            if not app.want_play:
                break
            heard.append(doc.chunks[app._active].text)
        assert heard == [
            "The table below lists the crew.",
            "Name", "Role", "Notes",
            "Alice", "Engineer", "short",
            "Bob", "Designer with a very long title that wraps around the column",
            "code here",
            "Carol", "on leave",               # the empty Role is skipped
            "That was the crew.",
        ]
        assert "end of document" in app.message
        assert app.status().nchunks == len(heard)
        assert all(doc.chunks[c].speakable for c in engine.synth_calls)
    finally:
        app.close()


def test_the_highlight_follows_a_wrapped_cell_down_its_lines():
    doc = crew_doc()
    role = next(c for c in doc.chunks if c.text.startswith("Designer"))
    app, doc, engine, player, screen = make_app(doc=doc, start_chunk=role.idx)
    app.start()
    try:
        assert settle(app)
        assert app._active == role.idx
        seen = []
        for slot in range(len(role.words)):
            player.now = slot * 0.1 + 0.05
            app.tick()
            seen.append(doc.words[app.cur_word].text)
        assert " ".join(seen) == role.text
        # "column" is on the cell's third line, below the neighbour's "here"
        assert doc.words[app.cur_word].line == 6
    finally:
        app.close()


def up_scrolls(tops):
    return [(a, b) for a, b in zip(tops, tops[1:]) if b < a]


def test_follow_mode_never_scrolls_up_while_reading_a_table_row():
    """The reproduction: 24 rows, lead 20.  "column" ends Bob's Role cell on
    the row's third line and the view scrolls down to it; "code" starts the
    next cell two lines higher, above the top margin, so following the word
    scrolled the view back up."""
    doc = crew_doc(prose(15), prose(60, "Outro"))
    app, doc, engine, player, screen = make_app(doc=doc, height=24,
                                                follow_lead=20)
    # the old policy, word by word in reading order, over the same layout
    top, tops = 0, []
    for widx in range(len(doc.words)):
        top = screen.top_for_word(widx, top, FOLLOW_MARGIN, 20)
        tops.append(top)
    assert up_scrolls(tops), "this layout no longer reproduces the bug"

    last_cell = max(c.idx for c in doc.chunks if c.kind == "cell")
    app.start()
    try:
        assert settle(app)
        tops = []
        for _ in range(4000):
            app.tick()
            if app._active is not None and app._active > last_cell:
                break
            tops.append(app.top)
            if app.cur_word is not None:
                offset = screen.row_of_word(app.cur_word) - app.top
                assert 0 <= offset < screen.body_height
            player.advance(0.05)
            time.sleep(0.001)
        else:
            pytest.fail("playback never got past the table")
        assert max(tops) > 0, "the view never scrolled"
        assert up_scrolls(tops) == []
        # Bob's three line row, not just a one-row span from a skip key
        assert any(first < last for first, last, *_ in screen.span_calls), \
            "the rows were never followed as rows"
    finally:
        app.close()


def test_following_a_table_never_scrolls_up_whatever_the_layout():
    """Word by word through prose, the table and more prose, at every offset
    of the table against the viewport, for a few heights and leads.  Following
    the word scrolls up somewhere in this sweep; following the row never does.
    At 7 rows Bob's three line row does not fit between the margins, so there
    follow mode keeps to the word, which must still stay on screen."""
    word_ups, row_ups = [], []
    for height in (7, 8, 12, 24, 30):
        for before in range(20):
            for lead in (0, 20):
                doc = crew_doc(prose(before), prose(30, "Outro"))
                screen = FakeScreen(doc, height=height)
                app = App(doc, screen, FakePlayer(), FakeEngine(), ahead=0,
                          follow_lead=lead)
                bob = next(c.idx for c in doc.chunks if c.text == "Bob")
                assert (app._row_span(bob) is not None) == (height > 7)
                word_top, word_tops, row_tops = 0, [], []
                for widx in range(len(doc.words)):
                    word_top = screen.top_for_word(widx, word_top,
                                                   FOLLOW_MARGIN, lead)
                    word_tops.append(word_top)
                    app.cur_word = widx
                    app.tick()               # never started: tick only follows
                    row = screen.row_of_word(widx)
                    assert app.top <= row < app.top + screen.body_height
                    row_tops.append(app.top)
                if up_scrolls(word_tops):
                    word_ups.append((height, before, lead))
                if height > 7 and up_scrolls(row_tops):
                    row_ups.append((height, before, lead))
    assert word_ups, "the sweep no longer reproduces the bug"
    assert row_ups == []


def test_a_row_that_ends_the_document_ends_on_the_last_row():
    """first_row_of_line clamps, so ``first_row_of_line(line_end) - 1`` would
    stop one row short at the end of the document."""
    doc = crew_doc(prose(3))
    app, doc, engine, player, screen = make_app(doc=doc, height=30)
    carol = next(c for c in doc.chunks if c.text == "Carol")
    assert app._row_span(carol.idx) == (10, 11)
    carol.line_end = len(doc.plain)          # as if no rule followed the row
    assert app._row_span(carol.idx) == (10, len(screen.rows) - 1)
    # not a cell, or too tall to fit between the margins: follow the word
    assert app._row_span(doc.chunk_of_word(0)) is None
    small, *_ = make_app(doc=crew_doc(prose(3)), height=7)
    bob = next(c for c in small.doc.chunks if c.text == "Bob")
    assert small._row_span(bob.idx) is None


# --------------------------------------------------------------------------- #
# 4. the real binary under a pty
# --------------------------------------------------------------------------- #


class PtyRun:
    """Run a command with a *pipe* on stdin and a pty on stdout, like a user."""

    def __init__(self, argv, stdin_bytes=b"", rows=24, cols=80, env=None,
                 cwd=None, home=None):
        self.argv = list(argv)
        self.stdin_bytes = stdin_bytes
        self.rows, self.cols = rows, cols
        self.out = bytearray()
        self.pid = self.master = None
        self._env, self._cwd = env, cwd
        # The child reads (and, on a first run, writes) ~/.readaloud.conf.  A
        # throwaway HOME keeps the suite out of the developer's real one and
        # stops their own preferences from steering these assertions.  Pass
        # `home=` to hand the child a directory with a config file in it.
        self._home = home
        self._temp_home = None
        self.status = None

    def __enter__(self):
        if self._home is None:
            self._temp_home = tempfile.mkdtemp(prefix="readaloud-home-")
            self._home = self._temp_home
        r, w = os.pipe()
        pid, master = pty.fork()
        if pid == 0:  # child: pty.fork already gave us a controlling terminal
            try:
                os.close(w)
                os.dup2(r, 0)
                if r > 2:
                    os.close(r)
                env = dict(os.environ)
                env["TERM"] = "xterm-256color"
                env["HOME"] = self._home
                env.pop("LINES", None)
                env.pop("COLUMNS", None)
                env.update(self._env or {})
                if self._cwd:
                    os.chdir(self._cwd)
                os.execvpe(self.argv[0], self.argv, env)
            except BaseException:
                os._exit(127)
        os.close(r)
        self.pid, self.master = pid, master
        fcntl.ioctl(master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", self.rows, self.cols, 0, 0))
        try:
            if self.stdin_bytes:
                os.write(w, self.stdin_bytes)
        finally:
            os.close(w)
        return self

    def pump(self, seconds=0.2):
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            try:
                rl, _, _ = select.select([self.master], [], [], min(0.05, left))
            except (OSError, ValueError):
                return
            if not rl:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    return
                raise
            if not data:
                return
            self.out += data

    def send(self, data, settle=0.15):
        os.write(self.master, data.encode() if isinstance(data, str) else data)
        self.pump(settle)

    def screen(self):
        return render(bytes(self.out), self.rows, self.cols)

    def wait_screen(self, needle, timeout=60.0, where=None):
        """Pump until `needle` shows up on the rendered screen."""
        end = time.time() + timeout
        while time.time() < end:
            rows = self.screen()
            hay = rows[where] if where is not None else "\n".join(rows)
            if needle in hay:
                return True
            self.pump(0.15)
        rows = self.screen()
        hay = rows[where] if where is not None else "\n".join(rows)
        return needle in hay

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def wait(self, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            self.pump(0.1)
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = st
                return st
        return None

    def __exit__(self, *exc):
        try:
            self.pump(0.2)
        except Exception:
            pass
        if self.status is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                _, self.status = os.waitpid(self.pid, 0)
            except OSError:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass
        if self._temp_home:
            shutil.rmtree(self._temp_home, ignore_errors=True)


def render(data: bytes, rows=24, cols=80):
    """A small ANSI terminal emulator — enough to check real curses output."""
    grid = [[" "] * cols for _ in range(rows)]
    cy = cx = 0
    top, bot = 0, rows - 1
    saved = (0, 0)

    def blank():
        return [" "] * cols

    def scroll_up(k=1):
        for _ in range(k):
            del grid[top]
            grid.insert(bot, blank())

    def scroll_down(k=1):
        for _ in range(k):
            del grid[bot]
            grid.insert(top, blank())

    def lf():
        nonlocal cy
        if cy == bot:
            scroll_up(1)
        elif cy < rows - 1:
            cy += 1

    s = data.decode("utf-8", "replace")
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\x1b":
            m = re.match(r"\x1b\[([0-9;?]*)([@-~])", s[i:])
            if m:
                params, fin = m.group(1), m.group(2)
                priv = params.startswith("?")
                nums = [int(p) for p in params.lstrip("?").split(";") if p.isdigit()]
                a = nums[0] if nums else None
                if priv:
                    pass                                  # DEC private modes
                elif fin in "Hf":
                    cy = max(0, min(rows - 1, (nums[0] - 1) if nums else 0))
                    cx = max(0, min(cols - 1, (nums[1] - 1) if len(nums) > 1 else 0))
                elif fin == "J":
                    mode = a or 0
                    if mode == 2:
                        grid[:] = [blank() for _ in range(rows)]
                    elif mode == 0:
                        for x in range(cx, cols):
                            grid[cy][x] = " "
                        for y in range(cy + 1, rows):
                            grid[y] = blank()
                    elif mode == 1:
                        for y in range(0, cy):
                            grid[y] = blank()
                        for x in range(0, cx + 1):
                            grid[cy][x] = " "
                elif fin == "K":
                    mode = a or 0
                    if mode == 0:
                        for x in range(cx, cols):
                            grid[cy][x] = " "
                    elif mode == 1:
                        for x in range(0, cx + 1):
                            grid[cy][x] = " "
                    else:
                        grid[cy] = blank()
                elif fin == "X":
                    for x in range(cx, min(cols, cx + (a or 1))):
                        grid[cy][x] = " "
                elif fin == "P":
                    row = grid[cy]
                    del row[cx:cx + (a or 1)]
                    row.extend([" "] * (cols - len(row)))
                elif fin == "@":
                    row = grid[cy]
                    for _ in range(a or 1):
                        row.insert(cx, " ")
                    del row[cols:]
                elif fin == "L":
                    if top <= cy <= bot:
                        for _ in range(a or 1):
                            del grid[bot]
                            grid.insert(cy, blank())
                elif fin == "M":
                    if top <= cy <= bot:
                        for _ in range(a or 1):
                            del grid[cy]
                            grid.insert(bot, blank())
                elif fin == "S":
                    scroll_up(a or 1)
                elif fin == "T":
                    scroll_down(a or 1)
                elif fin == "r":
                    top = max(0, min(rows - 1, (nums[0] - 1) if nums else 0))
                    bot = max(top, min(rows - 1,
                                       (nums[1] - 1) if len(nums) > 1 else rows - 1))
                    cy = cx = 0
                elif fin == "A":
                    cy = max(0, cy - (a or 1))
                elif fin == "B":
                    cy = min(rows - 1, cy + (a or 1))
                elif fin == "C":
                    cx = min(cols - 1, cx + (a or 1))
                elif fin == "D":
                    cx = max(0, cx - (a or 1))
                elif fin == "d":
                    cy = max(0, min(rows - 1, (a or 1) - 1))
                elif fin in "G`":
                    cx = max(0, min(cols - 1, (a or 1) - 1))
                elif fin == "E":
                    cx = 0
                    cy = min(rows - 1, cy + (a or 1))
                elif fin == "F":
                    cx = 0
                    cy = max(0, cy - (a or 1))
                i += m.end()
                continue
            m = re.match(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", s[i:])
            if m:
                i += m.end()
                continue
            nxt = s[i + 1] if i + 1 < n else ""
            if nxt == "M":
                if cy == top:
                    scroll_down(1)
                elif cy > 0:
                    cy -= 1
                i += 2
                continue
            if nxt in "DE":
                if nxt == "E":
                    cx = 0
                lf()
                i += 2
                continue
            if nxt == "7":
                saved = (cy, cx)
                i += 2
                continue
            if nxt == "8":
                cy, cx = saved
                i += 2
                continue
            m = re.match(r"\x1b[()*+][A-Za-z0-9]|\x1b[=><]", s[i:])
            if m:
                i += m.end()
                continue
            i += 1
            continue
        if ch == "\r":
            cx = 0
        elif ch == "\n":
            lf()
        elif ch == "\b":
            cx = max(0, cx - 1)
        elif ch == "\t":
            cx = min(cols - 1, (cx // 8 + 1) * 8)
        elif ch >= " ":
            if 0 <= cy < rows and 0 <= cx < cols:
                grid[cy][cx] = ch
            cx += 1
            if cx >= cols:
                cx = cols - 1
        i += 1
    return ["".join(r).rstrip() for r in grid]


def test_render_emulator_sanity():
    assert render(b"hi\r\nthere", 3, 20)[:2] == ["hi", "there"]
    assert render(b"abc\x1b[2J", 2, 5) == ["", ""]
    assert render(b"\x1b[2;3Hx", 3, 5)[1] == "  x"


STUB_CHILD = r'''
import os, sys, numpy as np
sys.path.insert(0, {src!r})
import readaloud.speech as speech
import readaloud.player as player
from readaloud.speech import Spoken, Timed


class StubEngine:
    def __init__(self, voice="af_heart", speed=1.0, lang_code="a", repo_id=""):
        self.voice, self.speed, self.lang_code = voice, float(speed), lang_code
        self.repo_id, self.sample_rate, self.last_error = repo_id, 24000, None
    def load(self): pass
    def close(self): pass
    def synth(self, chunk):
        n = max(1, len(chunk.words))
        per = 0.25 / max(0.1, self.speed)
        t = [Timed(i, i * per, (i + 1) * per) for i in range(len(chunk.words))]
        return Spoken(chunk.idx, np.full(int(24000 * per * n), 1e-4, np.float32), t)


class StubPlayer:
    def __init__(self, *a, **k):
        import time
        self._t = time
        self.spoken = None; self._t0 = 0.0; self._off = 0.0; self._paused = False
    def play(self, spoken, start_time=0.0):
        self.spoken = spoken; self._off = float(start_time)
        self._t0 = self._t.monotonic(); self._paused = False
    def pause(self):
        self._off = self.position; self._paused = True
    def resume(self):
        self._t0 = self._t.monotonic(); self._paused = False
    def stop(self): self.spoken = None; self._paused = False
    def close(self): self.spoken = None
    @property
    def position(self):
        if self.spoken is None: return 0.0
        if self._paused: return self._off
        return min(self.spoken.duration, self._off + self._t.monotonic() - self._t0)
    @property
    def playing(self):
        return self.spoken is not None and not self._paused and not self.finished
    @property
    def finished(self):
        return self.spoken is not None and self.position >= self.spoken.duration
    @property
    def chunk_idx(self):
        return None if self.spoken is None else self.spoken.chunk_idx


speech.Engine = StubEngine
player.Player = StubPlayer
from readaloud.cli import main
sys.exit(main())
'''


@pytest.fixture(scope="module")
def stub_child(tmp_path_factory):
    src = os.path.join(REPO, "src")
    path = tmp_path_factory.mktemp("stub") / "stub_main.py"
    path.write_text(STUB_CHILD.format(src=src), encoding="utf-8")
    return str(path)


def stub_argv(stub_child, *extra):
    return [sys.executable, stub_child, *extra]


def test_pty_renders_scrolls_and_quits(stub_child):
    body = "\n\n".join(
        f"Paragraph number {i}. The quick brown fox jumps over the lazy dog."
        for i in range(1, 21)
    ).encode()
    with PtyRun(stub_argv(stub_child), stdin_bytes=b"MARKER LINE\n\n" + body,
                cwd=REPO) as p:
        assert p.wait_screen("MARKER LINE", 30), p.screen()
        # the status bar is the last row and reports a chunk count
        assert p.wait_screen("chunk 1/", 30, where=-1), p.screen()[-1]
        first = p.screen()
        p.send("j", 0.3)
        p.send("j", 0.3)
        assert p.screen() != first, "j did not scroll"
        assert "follow off" in p.screen()[-1]
        p.send("g", 0.3)
        assert "MARKER LINE" in p.screen()[0]
        p.send("G", 0.3)
        assert "END" in p.screen()[-1]
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None, "the app did not exit on q"
    assert os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0


def test_pty_plays_and_highlights(stub_child):
    doc = b"Alpha bravo charlie delta echo foxtrot golf hotel india juliet.\n"
    with PtyRun(stub_argv(stub_child), stdin_bytes=doc, cwd=REPO) as p:
        assert p.wait_screen("Alpha bravo", 30), p.screen()
        assert p.wait_screen("PLAY", 30, where=-1), p.screen()[-1]
        # A_REVERSE on the spoken word has to reach the wire.
        assert p.wait_screen("PLAY", 5, where=-1)
        raw = bytes(p.out)
        assert re.search(rb"\x1b\[[0-9;]*7[;m]", raw), "no reverse-video attribute"
        p.send(" ", 0.4)
        assert "PAUSE" in p.screen()[-1]
        p.send(" ", 0.4)
        assert "PLAY" in p.screen()[-1] or "PAUSE" in p.screen()[-1]
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0


@pytest.mark.parametrize("flag", ["--media-keys", "--no-media-keys"])
def test_pty_media_key_flags_never_disturb_the_reader(stub_child, flag):
    """The degradation path, and the one that matters most: the system
    play/pause button is a nicety on top of an optional PyObjC extra.  With the
    extra absent `--media-keys` must be a silent no-op; with it present the role
    is claimed and released.  Either way the reader reads, and `q` exits 0.
    """
    doc = b"Alpha bravo charlie delta echo foxtrot golf hotel india juliet.\n"
    with PtyRun(stub_argv(stub_child, flag), stdin_bytes=doc, cwd=REPO) as p:
        assert p.wait_screen("Alpha bravo", 30), p.screen()
        assert p.wait_screen("PLAY", 30, where=-1), p.screen()[-1]
        p.send(" ", 0.4)
        assert "PAUSE" in p.screen()[-1]
        p.send(" ", 0.4)
        p.send("q", 0.5)
        st = p.wait(15)
        raw = bytes(p.out)
    assert b"Traceback" not in raw, raw[-2000:]
    assert st is not None and os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0


def test_pty_search_and_resize(stub_child):
    body = ("Intro line.\n\n" + "\n\n".join(
        f"Paragraph {i} carries the needle {i}." for i in range(1, 26))).encode()
    with PtyRun(stub_argv(stub_child), stdin_bytes=body, cwd=REPO) as p:
        assert p.wait_screen("Intro line", 30)
        p.send("/", 0.3)
        assert p.screen()[-1].startswith("/")
        p.send("needle 20", 0.3)
        assert "needle 20" in p.screen()[-1]
        p.send("\r", 0.6)
        assert any("needle 20" in r for r in p.screen()[:-1]), p.screen()
        p.resize(30, 100)
        p.pump(0.8)
        assert len(p.screen()) == 30
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0


def _chunk_no(status_line):
    m = re.search(r"chunk (\d+)/(\d+)", status_line)
    return int(m.group(1)) if m else None


def test_pty_mouse_click_jumps(stub_child):
    body = ("First paragraph here.\n\n" + "\n\n".join(
        f"Paragraph {i} has some words to click on." for i in range(1, 12))).encode()
    with PtyRun(stub_argv(stub_child), stdin_bytes=body, cwd=REPO) as p:
        assert p.wait_screen("First paragraph", 30)
        assert p.wait_screen("chunk 1/", 30, where=-1)
        p.send(" ", 0.4)                           # pause, so nothing auto-advances
        assert "PAUSE" in p.screen()[-1], p.screen()[-1]
        before = _chunk_no(p.screen()[-1])
        rows = p.screen()
        target = next(i for i, r in enumerate(rows) if r.startswith("Paragraph 9"))
        # SGR mouse: a press then a release on (row target, col 4), both 1-based
        p.send(f"\x1b[<0;4;{target + 1}M", 0.2)
        p.send(f"\x1b[<0;4;{target + 1}m", 1.0)
        after = _chunk_no(p.screen()[-1])
        assert after is not None and before is not None
        assert after > before, (before, after, p.screen()[-1])
        assert "PLAY" in p.screen()[-1]            # a click resumes playback
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0


def test_pty_start_flag_lands_on_that_chunk(stub_child):
    body = ("\n\n".join(f"Paragraph {i} of the document." for i in range(1, 16))
            ).encode()
    with PtyRun(stub_argv(stub_child, "--start", "4"), stdin_bytes=body,
                cwd=REPO) as p:
        assert p.wait_screen("chunk ", 30, where=-1), p.screen()[-1]
        p.send(" ", 0.4)                            # pause before it advances
        assert _chunk_no(p.screen()[-1]) == 4, p.screen()[-1]
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0


def test_pty_reads_the_config_file_from_home(stub_child, tmp_path):
    """The whole chain in one run: ~/.readaloud.conf -> flags -> status bar."""
    (tmp_path / ".readaloud.conf").write_text(
        "[readaloud]\nspeed = 1.4\nvoice = bm_george\n", encoding="utf-8")
    body = b"Alpha bravo charlie delta echo foxtrot golf hotel india juliet.\n"
    with PtyRun(stub_argv(stub_child), stdin_bytes=body, cwd=REPO,
                home=str(tmp_path)) as p:
        assert p.wait_screen("Alpha bravo", 30), p.screen()
        assert p.wait_screen("bm_george", 30, where=-1), p.screen()[-1]
        assert "1.40x" in p.screen()[-1], p.screen()[-1]
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0


def test_pty_a_flag_still_beats_the_config_file(stub_child, tmp_path):
    (tmp_path / ".readaloud.conf").write_text(
        "[readaloud]\nspeed = 1.4\nvoice = bm_george\n", encoding="utf-8")
    body = b"Alpha bravo charlie delta echo foxtrot golf hotel india juliet.\n"
    with PtyRun(stub_argv(stub_child, "--voice", "af_heart"), stdin_bytes=body,
                cwd=REPO, home=str(tmp_path)) as p:
        assert p.wait_screen("af_heart", 30, where=-1), p.screen()[-1]
        assert "1.40x" in p.screen()[-1], p.screen()[-1]   # the file still wins here
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0


def test_pty_first_run_leaves_a_config_behind(stub_child, tmp_path):
    conf = tmp_path / ".readaloud.conf"
    with PtyRun(stub_argv(stub_child), stdin_bytes=b"Some words to read.\n",
                cwd=REPO, home=str(tmp_path)) as p:
        assert p.wait_screen("Some words", 30)
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0
    assert conf.exists(), "the first run did not write ~/.readaloud.conf"
    cfg, warnings = config.load(conf)
    # the settings at their defaults, and the pronunciations it ships
    assert (cfg, warnings) == (
        config.Config(pronunciations=config.TEMPLATE_PRONUNCIATIONS), [])


def test_pty_a_broken_config_shows_up_in_the_status_bar(stub_child, tmp_path):
    """It must reach the user, and it must not be printed over the document."""
    (tmp_path / ".readaloud.conf").write_text(
        "[readaloud]\nspeed = maybe\n", encoding="utf-8")
    # long enough that playback does not reach "end of document" and replace
    # the notice while the test is still looking for it
    body = ("Some words to read here.\n\n" + "\n\n".join(
        f"Paragraph {i} of the document, with a few more words."
        for i in range(1, 21))).encode()
    with PtyRun(stub_argv(stub_child), stdin_bytes=body,
                cwd=REPO, home=str(tmp_path)) as p:
        assert p.wait_screen("Some words", 30), p.screen()
        assert p.wait_screen("config: speed", 10, where=-1), p.screen()[-1]
        assert "Some words" in "\n".join(p.screen()[:-1])
        p.send("q", 0.5)
        st = p.wait(10)
    assert st is not None and os.WEXITSTATUS(st) == 0


def test_pty_quit_during_startup_is_clean(stub_child):
    with PtyRun(stub_argv(stub_child), stdin_bytes=b"Some words to read.\n",
                cwd=REPO) as p:
        assert p.wait_screen("Some words", 30)
        p.send("q", 0.3)
        st = p.wait(10)
    assert st is not None, "did not exit"
    assert os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0


def test_pty_ctrl_c_exits_cleanly(stub_child):
    with PtyRun(stub_argv(stub_child), stdin_bytes=b"Some words to read aloud.\n",
                cwd=REPO) as p:
        assert p.wait_screen("Some words", 30)
        p.send("\x03", 0.5)                       # Ctrl-C -> SIGINT
        st = p.wait(10)
        tail = bytes(p.out)
    assert st is not None, "Ctrl-C did not stop the app"
    # the terminal must be restored: alternate screen turned off
    assert b"\x1b[?1049l" in tail or b"\x1b[?1006l" in tail


def test_pty_stdout_redirected_still_draws_on_the_terminal(stub_child, tmp_path):
    """`... | readaloud > log` must draw on /dev/tty, not into the log."""
    log = tmp_path / "log"
    with PtyRun(["/bin/sh", "-c",
                 f"exec {sys.executable} {stub_child} > {log}"],
                stdin_bytes=b"Redirect test paragraph here.\n", cwd=REPO) as p:
        assert p.wait_screen("Redirect test", 30), p.screen()
        p.send("q", 0.5)
        p.wait(10)
    assert log.exists()
    assert log.stat().st_size == 0, "the TUI leaked into the redirected stdout"


# --------------------------------------------------------------------------- #
# 5. the real model (slow)
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_real_save_produces_plausible_audio(tmp_path):
    out = tmp_path / "real.wav"
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); "
         "from readaloud.cli import main; sys.exit(main())" % os.path.join(REPO, "src"),
         "--save", str(out)],
        input=b"Hello there. This is a short test of the read aloud tool.\n",
        capture_output=True, cwd=REPO, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr.decode()[-3000:]
    with wave.open(str(out)) as wf:
        assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, 24000)
        frames = wf.getnframes()
        data = np.frombuffer(wf.readframes(frames), dtype="<i2").astype(np.float32)
    secs = frames / 24000.0
    assert 2.0 < secs < 15.0, secs
    assert np.abs(data / 32768).max() > 0.05
    assert float((np.abs(data) > 300).mean()) > 0.1   # not near-silence


@pytest.mark.slow
def test_real_binary_runs_under_a_pty():
    doc = b"Hello there friend. This is a short test document for the reader.\n"
    with PtyRun(["uv", "run", "readaloud"], stdin_bytes=doc, cwd=REPO) as p:
        assert p.wait_screen("Hello there friend", 180), p.screen()
        assert p.wait_screen("PLAY", 180, where=-1), p.screen()[-1]
        p.send("q", 1.0)
        st = p.wait(20)
    assert st is not None and os.WEXITSTATUS(st) == 0


def test_the_scrubber_keeps_moving_while_a_chunk_plays():
    """Elapsed time was published once at start() and never again, so Control
    Center's scrubber sat frozen at 0:00 for the whole session."""
    import time as _time

    app, keys, player = media_app()
    try:
        assert settle(app)
        before = len(keys.scrubs)
        # idle ticks inside one refresh window must not republish
        for _ in range(50):
            app.tick()
        assert len(keys.scrubs) == before, "position was pushed on the tick path"
        # ... but once the window passes, it is refreshed
        app._mk_elapsed_at -= (MEDIA_ELAPSED_EVERY + 0.01)
        app.tick()
        assert len(keys.scrubs) == before + 1, "the scrubber never advances"
        elapsed, duration, rate = keys.scrubs[-1]
        assert elapsed is not None and elapsed >= 0.0
        assert rate == 1.0
        _ = duration, _time
    finally:
        app.close()


def test_the_scrubber_is_not_refreshed_while_paused():
    from readaloud.keys import Command

    app, keys, player = media_app()
    try:
        assert settle(app)
        app.handle(Command(Action.PLAY_PAUSE))
        app.tick()
        before = len(keys.scrubs)
        app._mk_elapsed_at -= (MEDIA_ELAPSED_EVERY + 0.01)
        for _ in range(10):
            app.tick()
        assert len(keys.scrubs) == before, "position pushed while paused"
    finally:
        app.close()


def test_skipping_to_a_visible_chunk_does_not_move_the_viewport():
    """Skipping to a chunk already on screen must not scroll."""
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app(doc=make_doc(LONG), height=30,
                                                follow_lead=20)
    app.start()
    try:
        assert settle(app)
        assert app.follow is True
        before = app.top
        # the chunk right after the current one is comfortably on screen
        app.handle(Command(Action.NEXT_CHUNK))
        assert app.top == before, (
            f"skipping to a visible chunk scrolled the page {before} -> {app.top}")
        app.handle(Command(Action.PREV_CHUNK))
        assert app.top == before
    finally:
        app.close()


def test_skipping_to_an_offscreen_chunk_lands_where_follow_mode_would():
    """An off-screen target scrolls, but never pins the chunk to row 0."""
    from readaloud.keys import Command

    app, doc, engine, player, screen = make_app(doc=make_doc(LONG), height=30,
                                                follow_lead=20)
    app.start()
    try:
        assert settle(app)
        target = None
        for _ in range(40):
            app.handle(Command(Action.NEXT_CHUNK))
            cur = app._current_chunk()
            row = screen.first_row_of_line(doc.chunks[cur].line_start)
            if row > app.top + screen.body_height - 1 - app.follow_margin:
                target = (cur, row)
                break
            if app.top != 0:
                target = (cur, row)
                break
        assert target is not None, "never pushed a chunk off the bottom"
        cur, row = target
        # it scrolled, and the chunk is NOT pinned to the top row
        assert app.top > 0, "never scrolled at all"
        offset = screen.first_row_of_line(doc.chunks[cur].line_start) - app.top
        assert offset > 0, "the new chunk was pinned to row 0 of the viewport"
    finally:
        app.close()
