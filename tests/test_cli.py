"""Regression tests for `readaloud.cli` argument and destination validation.

Two defects are covered:

1. `--voice` was never checked against the repo's voice list, so a typo loaded
   the model fine and then failed at synthesis time, once per chunk -- the TUI
   raced silently to the end of the document and exited 0.
2. `--save` to an unwritable path only discovered the problem after the whole
   document had been synthesized, and `wave.open(path, "wb")` then printed an
   "Exception ignored in Wave_write.__del__" traceback on top of the friendly
   message.

And two features' plumbing:

3. ``-md``: the flag's spellings, where the Markdown source comes from, the
   one-line refusals (``-md FILE -f FILE``, no mdcat, input mdcat already
   rendered), and a Document with cell chunks reaching the reader.  mdcat is
   never run: `readaloud.markdown`'s seams return a canned render.
4. ``[pronunciations]``: the config file's pairs reaching the Document on
   either path, ``--no-config`` leaving them out, and their problems reaching
   stderr or the status bar.

Nothing here loads the TTS model: `list_voices` is stubbed and the `--save`
pre-flight must return before `Engine` is ever constructed.
"""

from __future__ import annotations

import gc
import os
import sys

import pytest

from readaloud import ansi, cli
from readaloud.document import Table, TableCell

VOICES = ["af_heart", "af_sarah", "am_adam", "bf_emma", "zf_xiaobei"]


@pytest.fixture()
def stub_voices(monkeypatch):
    """Make `list_voices` deterministic and offline."""
    calls = []

    def fake(repo_id="r", lang_code=None, allow_download=False):
        calls.append((repo_id, lang_code))
        if lang_code:
            pre = str(lang_code)[:1]
            return sorted(v for v in VOICES if v[:1] == pre)
        return sorted(VOICES)

    monkeypatch.setattr("readaloud.speech.list_voices", fake)
    return calls


def _doc(text="Hello world. This is a short document.\n"):
    return cli.build_document(text, max_sentences=4, max_chars=380,
                              no_color=False)


# --------------------------------------------------------------------------- #
# 1. --voice validation
# --------------------------------------------------------------------------- #


def test_check_voice_accepts_a_known_voice(stub_voices):
    assert cli.check_voice("af_heart", "repo", "a") is None
    assert cli.check_voice("bf_emma", "repo", "b") is None


def test_check_voice_rejects_a_typo_and_suggests_the_real_name(stub_voices):
    msg = cli.check_voice("af_sarahh", "repo", "a")
    assert msg is not None
    assert "af_sarahh" in msg and "af_sarah" in msg and "--list-voices" in msg


def test_check_voice_rejects_a_voice_with_an_unknown_language_letter(stub_voices):
    # `zzz-not-a-voice` used to pick lang "z" and die inside the pipeline with
    # a "pip install misaki" message.
    assert cli.check_voice("zzz-not-a-voice", "repo", "z") is not None


def test_check_voice_allows_an_unknown_repo(monkeypatch):
    monkeypatch.setattr("readaloud.speech.list_voices",
                        lambda *a, **k: [])
    assert cli.check_voice("whatever", "some/other-repo", "a") is None


def test_check_voice_survives_a_broken_voice_listing(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no snapshot")

    monkeypatch.setattr("readaloud.speech.list_voices", boom)
    assert cli.check_voice("af_heart", "repo", "a") is None


def test_main_rejects_an_unknown_voice_before_doing_any_work(
        monkeypatch, capsys, stub_voices):
    monkeypatch.setattr("readaloud.ui.read_stdin_text",
                        lambda *a, **k: "hello world.")

    def no_engine(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("the model must not be loaded for a bad voice")

    monkeypatch.setattr("readaloud.speech.Engine", no_engine)

    rc = cli.main(["--voice", "af_sarahh", "--save", "/tmp/never-written.wav"])
    assert rc == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "unknown voice" in err and "af_sarahh" in err
    assert "Traceback" not in err


def test_main_still_accepts_a_valid_voice(monkeypatch, capsys, stub_voices):
    # Validation must not stand in the way of a real voice: this run gets past
    # it and fails later, on the empty input, not on the voice.
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "")
    assert cli.main(["--voice", "bf_emma"]) == cli.EXIT_USAGE
    assert "unknown voice" not in capsys.readouterr().err


def test_main_lang_override_does_not_invalidate_the_voice(
        monkeypatch, capsys, stub_voices):
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "")
    assert cli.main(["--voice", "af_heart", "--lang", "b"]) == cli.EXIT_USAGE
    assert "unknown voice" not in capsys.readouterr().err


def test_list_voices_flag_is_not_blocked_by_the_check(capsys, stub_voices):
    assert cli.main(["--list-voices", "--voice", "af_sarahh"]) == cli.EXIT_OK
    assert "unknown voice" not in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# 2. --save destination pre-flight
# --------------------------------------------------------------------------- #


def test_check_writable_accepts_a_new_file_without_leaving_it_behind(tmp_path):
    target = tmp_path / "out.wav"
    assert cli.check_writable(str(target)) is None
    assert not target.exists()


def test_check_writable_does_not_truncate_an_existing_file(tmp_path):
    target = tmp_path / "out.wav"
    target.write_bytes(b"previous contents")
    assert cli.check_writable(str(target)) is None
    assert target.read_bytes() == b"previous contents"


def test_check_writable_reports_a_missing_directory(tmp_path):
    msg = cli.check_writable(str(tmp_path / "nope" / "out.wav"))
    assert msg is not None and "could not write" in msg


def test_check_writable_reports_a_directory_destination(tmp_path):
    msg = cli.check_writable(str(tmp_path))
    assert msg is not None and "could not write" in msg


def test_save_wav_checks_the_path_before_synthesizing(monkeypatch, capsys,
                                                      tmp_path):
    def no_engine(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("the destination must be checked first")

    monkeypatch.setattr("readaloud.speech.Engine", no_engine)

    rc = cli.save_wav(_doc(), str(tmp_path / "nope" / "out.wav"),
                      voice="af_heart", speed=1.0, lang="a", repo="repo")
    assert rc == cli.EXIT_ERROR
    assert "could not write" in capsys.readouterr().err


def test_save_wav_bad_path_prints_no_traceback(monkeypatch, capsys, tmp_path):
    """`wave.open(str)` used to leave a half-built Wave_write whose __del__
    raised AttributeError, dumped by CPython as an ignored-exception
    traceback."""
    monkeypatch.setattr("readaloud.speech.Engine",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("not reached")))

    unraisable = []
    old_hook = sys.unraisablehook
    sys.unraisablehook = unraisable.append
    try:
        rc = cli.save_wav(_doc(), str(tmp_path),  # a directory
                          voice="af_heart", speed=1.0, lang="a", repo="repo")
        gc.collect()
    finally:
        sys.unraisablehook = old_hook

    assert rc == cli.EXIT_ERROR
    assert unraisable == []
    err = capsys.readouterr().err
    assert "could not write" in err
    assert "Traceback" not in err and "Wave_write" not in err


def test_save_wav_writes_a_playable_wav(monkeypatch, tmp_path):
    """The happy path still produces a real WAV through the file-object form
    of `wave.open`."""
    import numpy as np

    from readaloud.speech import SAMPLE_RATE, Spoken

    class FakeEngine:
        last_error = None

        def __init__(self, *a, **k):
            pass

        def load(self):
            pass

        def synth(self, chunk):
            return Spoken(chunk_idx=chunk.idx,
                          audio=np.zeros(SAMPLE_RATE // 10, dtype=np.float32),
                          timings=[])

        def close(self):
            pass

    monkeypatch.setattr("readaloud.speech.Engine", FakeEngine)

    target = tmp_path / "out.wav"
    rc = cli.save_wav(_doc(), str(target), voice="af_heart", speed=1.0,
                      lang="a", repo="repo", quiet=True)
    assert rc == cli.EXIT_OK

    import wave

    with wave.open(str(target), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == SAMPLE_RATE
        assert wf.getnframes() > 0


# --------------------------------------------------------------------------- #
# 3. -md
# --------------------------------------------------------------------------- #

TEAM_MD = """\
# Team

| Name | Role | Notes |
|------|:----:|------:|
| Alice | Engineer | short[^1] |
| Bob | Designer with a very long title that wraps | code here |

[^1]: Only on Tuesdays.
"""

RULE = "─" * 40

#: ``mdcat --ansi --columns 40`` of TEAM_MD, byte for byte (mdcat 2.16)
TEAM_ANSI = (
    "\n\n\x1b[94m\x1b[104m \x1b[0m\x1b[1m\x1b[97m\x1b[104mTeam\x1b[0m"
    "\x1b[94m\x1b[104m \x1b[0m\n"
    "\n\n"
    f"{RULE}\n"
    " \x1b[1mName\x1b[0m           \x1b[1mRole\x1b[0m              "
    "\x1b[1mNotes\x1b[0m \n"
    f"{RULE}\n"
    " Alice        Engineer         short[1] \n"
    " Bob    Designer with a very       code \n"
    "        long title that wraps      here \n"
    f"{RULE}\n"
    "\n"
    "\x1b[36m[1]: Only on Tuesdays.\x1b[0m\n"
    "\n"
)

NOTICES = ["-md: table 2 is read line by line (rows overflow the column width)"]

FAKE_MDCAT = "/opt/fake/bin/mdcat"


def team_table() -> Table:
    """TEAM_ANSI's table, mapped the way `readaloud.markdown` maps it.

    Every column's char range on every physical line of its row; the text is
    ASCII, so char offsets are display columns.
    """
    rows = [(6, 7), (8, 9), (9, 11)]
    columns = [(1, 6), (8, 29), (31, 39)]
    cells = [TableCell(r, c, [(line, a, b) for line in range(first, end)])
             for r, (first, end) in enumerate(rows)
             for c, (a, b) in enumerate(columns)]
    return Table(5, 12, 3, rows, cells)


@pytest.fixture()
def fake_mdcat(monkeypatch):
    """mdcat "installed", with a canned render; records every call it gets."""
    from readaloud import markdown

    calls: dict = {"render": [], "width": 0}

    def render(text, *, mdcat, columns):
        calls["render"].append((text, mdcat, columns))
        return markdown.Rendered(ansi.parse(TEAM_ANSI), [team_table()],
                                 list(NOTICES))

    def width(default=80):
        calls["width"] += 1
        return 40

    monkeypatch.setattr("readaloud.markdown.find_mdcat", lambda: FAKE_MDCAT)
    monkeypatch.setattr("readaloud.markdown.render_markdown", render)
    monkeypatch.setattr("readaloud.markdown.render_width", width)
    return calls


@pytest.fixture()
def launched(monkeypatch, stub_voices):
    """Intercept the TUI: `cli.main` returns the kwargs it would have run with.

    Piped stdin holds TEAM_MD.
    """
    from readaloud import app as app_mod

    seen: dict = {}

    def fake_run(doc, **kw):
        seen.clear()
        seen.update(kw)
        seen["doc"] = doc
        return 0

    monkeypatch.setattr(app_mod, "run", fake_run)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: TEAM_MD)
    monkeypatch.setattr("readaloud.ui.reopen_tty", lambda: None)
    monkeypatch.setattr(os, "isatty", lambda fd: True)
    return seen


def cell_texts(doc) -> list[str]:
    return [c.text for c in doc.chunks if c.kind == "cell"]


TEAM_CELLS = ["Name", "Role", "Notes", "Alice", "Engineer", "short[1]", "Bob",
              "Designer with a very long title that wraps", "code here"]


@pytest.mark.parametrize("argv, markdown, file, text", [
    ([], None, None, []),
    (["-md", "x.md"], "x.md", None, []),
    (["--markdown", "x.md"], "x.md", None, []),
    (["-md=x.md"], "x.md", None, []),
    (["-f", "x.md", "-md"], True, "x.md", []),
    (["-md", "-f", "x.md"], True, "x.md", []),
    (["-md"], True, None, []),
    (["-md", "-"], "-", None, []),
    (["-md", "--", "| a | b |"], True, None, ["| a | b |"]),
    (["-md", ""], "", None, []),
])
def test_md_parses_as_a_file_or_as_a_modifier(argv, markdown, file, text):
    args = cli.build_parser().parse_args(argv)
    assert args.markdown == markdown and type(args.markdown) is type(markdown)
    assert args.file == file
    assert args.text == text


def test_md_is_in_the_help_and_its_examples(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "-md [FILE], --markdown [FILE]" in out
    assert "needs mdcat" in out
    assert "readaloud -md notes.md" in out


@pytest.mark.parametrize("argv", [
    ["-md", "a.md", "-f", "b.md"],
    ["-f", "b.md", "-md", "a.md"],
])
def test_md_file_and_f_file_together_is_a_usage_error(
        argv, monkeypatch, capsys, stub_voices):
    def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("refused before mdcat or the input is touched")

    monkeypatch.setattr("readaloud.markdown.find_mdcat", never)
    monkeypatch.setattr(cli, "read_input", never)
    assert cli.main([*argv, "--no-config"]) == cli.EXIT_USAGE
    assert capsys.readouterr().err == \
        "readaloud: use either -md FILE or -f FILE -md\n"


def test_md_without_mdcat_is_one_line_and_leaves_stdin_alone(
        monkeypatch, capsys, stub_voices, tmp_path):
    def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("nothing is read or rendered without mdcat")

    monkeypatch.setattr("readaloud.markdown.find_mdcat", lambda: None)
    monkeypatch.setattr("readaloud.markdown.render_markdown", never)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", never)
    for argv in (["-md"], ["-md", str(tmp_path / "notes.md")]):
        assert cli.main([*argv, "--no-config"]) == cli.EXIT_ERROR
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and "Traceback" not in err
        assert "-md needs mdcat" in err and "brew install mdcat" in err
        assert "readaloud -f FILE" in err


def test_reading_without_md_never_looks_for_mdcat(monkeypatch, launched):
    def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("mdcat is only for -md")

    monkeypatch.setattr("readaloud.markdown.find_mdcat", never)
    monkeypatch.setattr("readaloud.markdown.render_markdown", never)
    assert cli.main(["--no-config"]) == 0
    doc = launched["doc"]
    assert doc.references is True and doc.tables == []
    assert cell_texts(doc) == []


@pytest.mark.parametrize("argv", [["-md"], ["-md", "{render}"]])
def test_md_refuses_input_that_mdcat_already_rendered(
        argv, tmp_path, monkeypatch, capsys, launched, fake_mdcat):
    """Piped or saved to a file, the one line says what is wrong with the input
    and nothing about a pipe that may not be there."""
    render = tmp_path / "notes.ansi"
    render.write_text(TEAM_ANSI, encoding="utf-8")
    monkeypatch.setattr("readaloud.ui.read_stdin_text",
                        lambda *a, **k: TEAM_ANSI)
    argv = [a.replace("{render}", str(render)) for a in argv]
    assert cli.main([*argv, "--no-config"]) == cli.EXIT_USAGE
    assert capsys.readouterr().err == (
        "readaloud: -md wants Markdown source, not mdcat's output: "
        "readaloud -md notes.md\n")
    assert fake_mdcat["render"] == [] and launched == {}


def test_document_name_follows_the_md_file():
    parse = cli.build_parser().parse_args
    assert cli.document_name(parse(["-md", "/tmp/notes.md"])) == "notes.md"
    assert cli.document_name(parse(["-f", "/tmp/notes.md", "-md"])) == "notes.md"
    assert cli.document_name(parse(["-md", "-"])) == "readaloud"
    assert cli.document_name(parse(["-md", ""])) == "readaloud"
    assert cli.document_name(parse(["-md"])) == "readaloud"
    assert cli.document_name(parse(["-md", "--", "| a |"])) == "readaloud"


def test_read_input_takes_the_md_file_like_f(tmp_path, monkeypatch):
    f = tmp_path / "notes.md"
    f.write_text("from the file\n", encoding="utf-8")
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "from stdin")
    parse = cli.build_parser().parse_args

    assert cli.read_input(parse(["-md", str(f), "ignored"])) == "from the file\n"
    assert cli.read_input(parse(["-f", str(f), "-md"])) == "from the file\n"
    assert cli.read_input(parse(["-md", "-"])) == "from stdin"
    assert cli.read_input(parse(["-md"])) == "from stdin"
    assert cli.read_input(parse(["-md", "--", "some", "words"])) == "some words"
    with pytest.raises(OSError):
        cli.read_input(parse(["-md", str(tmp_path / "missing.md")]))


def test_md_reads_the_file_with_tables_one_cell_at_a_time(
        tmp_path, launched, fake_mdcat):
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    assert cli.main(["-md", str(notes), "--no-config"]) == 0

    assert fake_mdcat["render"] == [(TEAM_MD, FAKE_MDCAT, 40)]
    doc = launched["doc"]
    # mdcat's lines exactly: no Markdown stripping, no reference stripping
    assert doc.plain == ansi.plain_lines(ansi.parse(TEAM_ANSI))
    assert doc.tables == [team_table()]
    assert cell_texts(doc) == TEAM_CELLS
    assert [c.cell for c in doc.chunks if c.kind == "cell"] == [
        (0, r, c) for r in range(3) for c in range(3)]
    # references=False: "[1]" stays in its cell and the footnote is read
    assert doc.references is False
    footnote = next(c for c in doc.chunks if "Tuesdays" in c.text)
    assert footnote.speakable and footnote.kind != "cell"
    assert launched["start_chunk"] == doc.speakable_chunks[0]
    assert launched["doc_name"] == "notes.md"
    assert launched["notices"] == NOTICES


@pytest.mark.parametrize("argv, name", [
    (["-f", "{notes}", "-md"], "notes.md"),
    (["-md", "-"], "readaloud"),
    (["-md"], "readaloud"),                      # cat notes.md | readaloud -md
    (["-md", "--", TEAM_MD], "readaloud"),       # TEXT is the source
])
def test_md_source_can_come_from_any_input(argv, name, tmp_path, launched,
                                           fake_mdcat):
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    argv = [a.replace("{notes}", str(notes)) for a in argv]
    assert cli.main(["--no-config", *argv]) == 0     # nothing after a "--"
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == [TEAM_MD]
    assert cell_texts(launched["doc"]) == TEAM_CELLS
    assert launched["doc_name"] == name


def never_stdin(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("the file is the input, not stdin")


@pytest.mark.parametrize("argv", [
    ["{notes}", "-md"],
    ["-md", "{notes}"],                          # -md FILE, as it always was
])
def test_md_reads_a_lone_text_that_names_a_file_as_that_file(
        argv, tmp_path, monkeypatch, launched, fake_mdcat):
    """A bare -md takes no value from a later token, so argparse leaves
    `notes.md -md` with the file in TEXT.  It is the file: "notes dot md" is
    never the document."""
    monkeypatch.setattr("readaloud.ui.read_stdin_text", never_stdin)
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    argv = [a.replace("{notes}", str(notes)) for a in argv]
    assert cli.main(["--no-config", *argv]) == 0
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == [TEAM_MD]
    assert cell_texts(launched["doc"]) == TEAM_CELLS
    assert launched["doc_name"] == "notes.md"


def test_md_start_counts_the_cells_of_a_file_given_after_the_options(
        tmp_path, monkeypatch, launched, fake_mdcat):
    monkeypatch.setattr("readaloud.ui.read_stdin_text", never_stdin)
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    assert cli.main(["--no-config", "-md", "--start", "3", str(notes)]) == 0
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == [TEAM_MD]
    doc = launched["doc"]
    assert doc.chunks[launched["start_chunk"]].cell == (0, 0, 1)


def test_md_save_reads_a_file_given_after_the_options(
        tmp_path, monkeypatch, capsys, stub_voices, fake_mdcat):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", never_stdin)
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    target = tmp_path / "out.wav"
    assert cli.main(["--no-config", "-md", "--save", str(target),
                     str(notes)]) == cli.EXIT_OK
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == [TEAM_MD]
    assert [c.text for c in RecordingEngine.chunks if c.kind == "cell"] == \
        TEAM_CELLS
    assert capsys.readouterr().out.startswith(f"wrote {target}")


@pytest.mark.parametrize("argv, source", [
    (["-md", "--", "# Title"], "# Title"),
    (["{missing}", "-md"], "{missing}"),
    (["{dir}", "-md"], "{dir}"),
    (["{notes}", "{notes}", "-md"], "{notes} {notes}"),
    (["-f", "{notes}", "{notes}", "-md"], TEAM_MD),     # -f wins, as ever
], ids=["words", "missing", "directory", "two-files", "f-file"])
def test_md_reads_any_other_text_as_the_source(
        argv, source, tmp_path, monkeypatch, launched, fake_mdcat):
    """Only one TEXT naming an existing file is taken for it."""
    monkeypatch.setattr("readaloud.ui.read_stdin_text", never_stdin)
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    names = {"{notes}": str(notes), "{missing}": str(tmp_path / "missing.md"),
             "{dir}": str(tmp_path)}

    def fill(s):
        for key, value in names.items():
            s = s.replace(key, value)
        return s

    assert cli.main(["--no-config", *map(fill, argv)]) == 0
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == \
        [fill(source)]


def test_md_start_counts_cells(launched, fake_mdcat):
    """``--start N`` and "chunk N/M" count cells: the heading, Name, Role."""
    assert cli.main(["-md", "--start", "3", "--no-config"]) == 0
    doc = launched["doc"]
    assert doc.chunks[launched["start_chunk"]].cell == (0, 0, 1)


def test_md_notices_take_their_turn_after_the_config_warnings(
        tmp_path, capsys, monkeypatch, launched, fake_mdcat):
    from test_integration import FakeEngine, FakePlayer, FakeScreen

    from readaloud.app import NOTICE_TTL, App

    conf = tmp_path / "readaloud.conf"
    conf.write_text("[readaloud]\nspeed = 9.0\n", encoding="utf-8")
    assert cli.main(["-md", "--config", str(conf)]) == 0
    notices = list(launched["notices"])
    assert notices[0].startswith("config: ") and "speed" in notices[0]
    assert notices[1:] == NOTICES
    # the reader gets them in the status bar; a print() would land under curses
    assert capsys.readouterr() == ("", "")

    # ... every one of them, in turn: not the first and a "(+1 more)"
    now = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    doc = launched["doc"]
    app = App(doc, FakeScreen(doc), FakePlayer(), FakeEngine(), ahead=0)
    app.queue_notices(notices)
    shown = []
    while app.message:
        shown.append(app.message)
        now[0] += NOTICE_TTL + 0.01
    assert shown == notices


def test_md_no_color_drops_colour_and_keeps_the_tables(launched, fake_mdcat):
    assert cli.main(["-md", "--no-config"]) == 0
    assert any(r.style.fg is not None for line in launched["doc"].lines
               for r in line)

    assert cli.main(["-md", "--no-color", "--no-config"]) == 0
    doc = launched["doc"]
    assert all(r.style.fg is None and r.style.bg is None
               for line in doc.lines for r in line)
    assert any(r.style.bold for line in doc.lines for r in line)
    assert cell_texts(doc) == TEAM_CELLS


def test_md_mdcat_failure_is_one_line(monkeypatch, capsys, launched,
                                      fake_mdcat):
    from readaloud import markdown

    # the line markdown.py makes of a panic
    crashed = markdown._failure(101, "thread 'main' panicked at x.rs:1:1:\nboom")

    def fail(text, *, mdcat, columns):
        raise markdown.MdcatError(crashed)

    monkeypatch.setattr("readaloud.markdown.render_markdown", fail)
    assert cli.main(["-md", "--no-config"]) == cli.EXIT_ERROR
    assert capsys.readouterr().err == f"readaloud: {crashed}\n"
    assert launched == {}


def test_md_empty_and_missing_input_are_the_usual_messages(
        tmp_path, monkeypatch, capsys, launched, fake_mdcat):
    empty = tmp_path / "empty.md"
    empty.write_text("\n\n", encoding="utf-8")
    assert cli.main(["-md", str(empty), "--no-config"]) == cli.EXIT_USAGE
    assert "the input is empty" in capsys.readouterr().err

    missing = tmp_path / "missing.md"
    assert cli.main(["-md", str(missing), "--no-config"]) == cli.EXIT_ERROR
    assert f"could not read {missing}" in capsys.readouterr().err

    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "")
    assert cli.main(["--no-config"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "no input" in err and "readaloud -md notes.md" in err
    assert fake_mdcat["render"] == []


class RecordingEngine:
    """`speech.Engine` for ``--save``: a tenth of a second of silence per chunk."""

    last_error = None
    chunks: list = []

    def __init__(self, *a, **k):
        RecordingEngine.chunks = []

    def load(self):
        pass

    def synth(self, chunk):
        import numpy as np

        from readaloud.speech import SAMPLE_RATE, Spoken

        RecordingEngine.chunks.append(chunk)
        return Spoken(chunk_idx=chunk.idx,
                      audio=np.zeros(SAMPLE_RATE // 10, dtype=np.float32),
                      timings=[])

    def close(self):
        pass


def test_md_save_renders_at_80_columns_and_warns_on_stderr(
        tmp_path, monkeypatch, capsys, stub_voices, fake_mdcat):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: TEAM_MD)
    target = tmp_path / "team.wav"
    assert cli.main(["-md", "--save", str(target), "--no-config"]) == cli.EXIT_OK

    assert [cols for _text, _mdcat, cols in fake_mdcat["render"]] == [80]
    assert fake_mdcat["width"] == 0          # the terminal is not asked
    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {target}") and "-md:" not in out
    assert all(f"readaloud: {notice}\n" in err for notice in NOTICES)
    assert target.exists()
    assert [c.text for c in RecordingEngine.chunks if c.kind == "cell"] == \
        TEAM_CELLS

    # a tenth of a second per chunk, and the 0.2 s breath only after prose: a
    # cell's audio already ends in its own pause
    import wave

    from readaloud.speech import SAMPLE_RATE

    chunks = RecordingEngine.chunks
    prose = sum(c.kind != "cell" for c in chunks)
    with wave.open(str(target), "rb") as wf:
        assert wf.getnframes() == (len(chunks) * (SAMPLE_RATE // 10)
                                   + prose * int(SAMPLE_RATE * 0.20))


def test_build_markdown_document_keeps_mdcat_lines_verbatim(monkeypatch):
    """An unstyled render has no escapes, and `strip_markdown` would eat its
    literal ``*stars*`` -- moving every column after them."""
    from readaloud import markdown

    rendered = markdown.Rendered(ansi.parse("literal *stars* and [1] here\n"),
                                 [], ["a notice"])
    monkeypatch.setattr("readaloud.markdown.render_markdown",
                        lambda text, *, mdcat, columns: rendered)
    doc, notices = cli.build_markdown_document(
        r"literal \*stars\* and [1] here", mdcat=FAKE_MDCAT, columns=40,
        max_sentences=4, max_chars=380, no_color=False)
    assert doc.plain[0] == "literal *stars* and [1] here"
    assert doc.references is False
    assert notices == ["a notice"] and notices is not rendered.notices


# --------------------------------------------------------------------------- #
# 4. [pronunciations]
# --------------------------------------------------------------------------- #


def test_both_builders_respell_with_the_pronunciations_they_are_given(
        monkeypatch):
    from readaloud import markdown
    from readaloud.pronounce import Lexicon

    lexicon = Lexicon([("id", "ID")])
    doc = cli.build_document("Look up foo.id.\n", max_sentences=4,
                             max_chars=380, no_color=False,
                             pronunciations=lexicon)
    assert [c.text for c in doc.chunks] == ["Look up foo.ID."]
    assert doc.plain == ["Look up foo.id."]
    assert _doc("Look up foo.id.\n").chunks[0].text == "Look up foo.id."

    monkeypatch.setattr("readaloud.markdown.render_markdown",
                        lambda text, *, mdcat, columns: markdown.Rendered(
                            ansi.parse(TEAM_ANSI), [team_table()], []))
    doc, _notices = cli.build_markdown_document(
        TEAM_MD, mdcat=FAKE_MDCAT, columns=40, max_sentences=4,
        max_chars=380, no_color=False,
        pronunciations=Lexicon([("Bob", "Robert"), ("short", "brief")]))
    assert cell_texts(doc)[5:7] == ["brief[1]", "Robert"]
    assert doc.tables == [team_table()]


def write_conf(tmp_path, body: str):
    conf = tmp_path / "readaloud.conf"
    conf.write_text(body, encoding="utf-8")
    return conf


def test_save_says_the_pronunciations_of_the_config_file(
        tmp_path, monkeypatch, capsys, stub_voices):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    conf = write_conf(tmp_path, "[pronunciations]\nid = ID\n")
    target = tmp_path / "out.wav"
    assert cli.main(["--config", str(conf), "--save", str(target),
                     "Look up foo.id."]) == cli.EXIT_OK
    assert [c.text for c in RecordingEngine.chunks] == ["Look up foo.ID."]
    assert [c.word_texts for c in RecordingEngine.chunks] == [
        ["Look", "up", "foo.ID"]]
    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {target}") and "readaloud:" not in err

    # --no-config reads it as written
    assert cli.main(["--config", str(conf), "--no-config", "--save",
                     str(target), "Look up foo.id."]) == cli.EXIT_OK
    assert [c.text for c in RecordingEngine.chunks] == ["Look up foo.id."]


def test_md_save_says_the_pronunciations_in_the_cells(
        tmp_path, monkeypatch, stub_voices, fake_mdcat):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: TEAM_MD)
    conf = write_conf(tmp_path, "[pronunciations]\nBob = Robert\n")
    target = tmp_path / "team.wav"
    assert cli.main(["-md", "--config", str(conf), "--save",
                     str(target)]) == cli.EXIT_OK
    cells = [c.text for c in RecordingEngine.chunks if c.kind == "cell"]
    assert cells == [text.replace("Bob", "Robert") for text in TEAM_CELLS]


def test_a_bad_pronunciation_line_is_reported_and_costs_only_itself(
        tmp_path, monkeypatch, capsys, stub_voices):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    conf = write_conf(tmp_path, "[readaloud]\nspeed = 1.5\n\n"
                                "[pronunciations]\nkubectl cube control\n"
                                "id = ID\n")
    target = tmp_path / "out.wav"
    assert cli.main(["--config", str(conf), "--save", str(target),
                     "kubectl and foo.id"]) == cli.EXIT_OK
    assert [c.text for c in RecordingEngine.chunks] == ["kubectl and foo.ID"]
    err = capsys.readouterr().err
    assert err.startswith(f"readaloud: {conf}: [pronunciations] line 5: "
                          "'kubectl cube control' has no \"=\"; "
                          "write it as: text = how to say it\n")
    assert err.count("readaloud:") == 1


def test_the_reader_gets_pronunciation_problems_in_the_status_bar(
        tmp_path, capsys, launched):
    conf = write_conf(tmp_path, "[pronunciations]\nid ID\n")
    assert cli.main(["--config", str(conf), "foo.id"]) == 0
    assert launched["notices"] == [
        "config: [pronunciations] line 2: 'id ID' has no \"=\"; "
        "write it as: text = how to say it"]
    assert capsys.readouterr() == ("", "")


def sabotage_find(monkeypatch):
    def boom(self, text, symbols=()):
        raise RuntimeError("sabotaged")

    monkeypatch.setattr("readaloud.pronounce.Lexicon.find", boom)


def test_chunks_a_pronunciation_fails_on_are_read_as_written_with_a_notice(
        tmp_path, monkeypatch, capsys, stub_voices, launched, fake_mdcat):
    sabotage_find(monkeypatch)
    conf = write_conf(tmp_path, "[pronunciations]\nid = ID\n")
    text = "One id.\n\nTwo ids.\n\nThree."

    assert cli.main(["--config", str(conf), text]) == 0
    doc = launched["doc"]
    assert doc.respell_failures == 3
    assert [c.text for c in doc.chunks if c.speakable] == [
        "One id.", "Two ids.", "Three."]
    assert launched["notices"] == [
        "pronunciations: 3 chunks are read as written "
        "(a pronunciation could not be applied)"]
    assert capsys.readouterr() == ("", "")

    # in the reader it takes its turn after the config warnings and -md's
    conf = write_conf(tmp_path, "[pronunciations]\nid = ID\nBob Robert\n")
    assert cli.main(["-md", "--config", str(conf)]) == 0
    doc = launched["doc"]
    assert doc.respell_failures == len(doc.speakable_chunks) == 11
    assert launched["notices"] == [
        "config: [pronunciations] line 3: 'Bob Robert' has no \"=\"; "
        "write it as: text = how to say it",
        *NOTICES,
        "pronunciations: 11 chunks are read as written "
        "(a pronunciation could not be applied)"]
    assert capsys.readouterr() == ("", "")

    # --save says them on stderr, stdout stays the one line
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    target = tmp_path / "out.wav"
    assert cli.main(["--config", str(conf), "--save", str(target),
                     "One id."]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {target}") and "read as written" not in out
    assert err.split("\n")[:2] == [
        f"readaloud: {conf}: [pronunciations] line 3: 'Bob Robert' has no "
        "\"=\"; write it as: text = how to say it",
        "readaloud: pronunciations: 1 chunk is read as written "
        "(a pronunciation could not be applied)"]
    assert err.count("readaloud:") == 2
    assert [c.text for c in RecordingEngine.chunks] == ["One id."]


def test_no_config_or_no_pronunciations_never_respell(
        tmp_path, monkeypatch, launched):
    sabotage_find(monkeypatch)             # never called without an entry
    conf = write_conf(tmp_path, "[pronunciations]\nid = ID\n")
    assert cli.main(["--config", str(conf), "--no-config", "foo.id"]) == 0
    assert launched["doc"].respell_failures == 0
    assert launched["notices"] == []
    assert cli.main(["foo.id"]) == 0       # the sandbox has no config file
    assert launched["doc"].chunks[0].text == "foo.id"
