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

from readaloud import ansi, cli, config
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


MD_CONF = "[config.markdown]\nrenderer = mdcat\n"


@pytest.fixture()
def md_conf(tmp_path):
    """A config that draws Markdown with mdcat, as the shipped markdown.conf does."""
    return str(write_conf(tmp_path, MD_CONF))


@pytest.mark.parametrize("argv, file, text, conf", [
    ([], None, [], None),
    (["-f", "x.md"], "x.md", [], None),
    (["--conf", "md"], None, [], "md"),
    (["--conf", "python", "-f", "x.txt"], "x.txt", [], "python"),
    (["-f", "-"], "-", [], None),
    (["--", "| a | b |"], None, ["| a | b |"], None),
])
def test_the_input_flags_parse(argv, file, text, conf):
    args = cli.build_parser().parse_args(argv)
    assert (args.file, args.text, args.conf) == (file, text, conf)


def test_md_is_gone_with_a_hint(capsys, monkeypatch):
    def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("refused before anything is read")

    monkeypatch.setattr(cli, "read_input", never)
    for argv in (["-md", "notes.md"], ["--markdown"], ["-f", "x.md", "-md"]):
        assert cli.main([*argv, "--no-config"]) == cli.EXIT_USAGE
        err = capsys.readouterr().err
        assert err.startswith("readaloud: -md is gone: ") and "--conf md" in err


def test_conf_and_a_bare_file_are_in_the_help(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    assert "--conf NAME" in out and "--conf md" in out
    assert "lone TEXT" in out and "readaloud notes.md" in out


def test_markdown_without_mdcat_is_read_as_written_with_a_notice(
        monkeypatch, tmp_path, launched, md_conf):
    def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("nothing is rendered without mdcat")

    monkeypatch.setattr("readaloud.markdown.find_mdcat", lambda: None)
    monkeypatch.setattr("readaloud.markdown.render_markdown", never)
    notes = tmp_path / "notes.md"
    notes.write_text("# Title\n\nSome **bold** words.\n", encoding="utf-8")
    assert cli.main(["--config", md_conf, str(notes)]) == 0
    doc = launched["doc"]
    assert doc.plain[0] == "Title"             # format = markdown: stripped
    assert launched["notices"] == [
        "config: renderer = mdcat, but mdcat is not installed (brew install "
        "mdcat); the Markdown is read without it"]


def test_reading_without_a_renderer_never_looks_for_mdcat(monkeypatch, launched):
    def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("mdcat is only for renderer = mdcat")

    monkeypatch.setattr("readaloud.markdown.find_mdcat", never)
    monkeypatch.setattr("readaloud.markdown.render_markdown", never)
    assert cli.main(["--no-config"]) == 0
    doc = launched["doc"]
    assert doc.references is True and doc.tables == []
    assert cell_texts(doc) == []


def test_input_mdcat_already_rendered_is_read_as_it_came(
        tmp_path, monkeypatch, launched, fake_mdcat, md_conf):
    """Piped or saved to a file: coloured text is never drawn a second time."""
    render = tmp_path / "notes.ansi"
    render.write_text(TEAM_ANSI, encoding="utf-8")
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: TEAM_ANSI)
    for argv in (["--conf", "md"], ["--conf", "md", "-f", str(render)]):
        assert cli.main(["--config", md_conf, *argv]) == 0
        assert fake_mdcat["render"] == []
        doc = launched["doc"]
        assert doc.tables == [] and "Alice" in "".join(doc.plain)
        assert any(r.style.fg is not None for line in doc.lines for r in line)
        assert launched["notices"] == []


def test_document_name_follows_the_file():
    parse = cli.build_parser().parse_args
    assert cli.document_name(parse(["-f", "/tmp/notes.md"])) == "notes.md"
    assert cli.document_name(parse(["-f", "-"])) == "readaloud"
    assert cli.document_name(parse(["--conf", "md"])) == "readaloud"
    assert cli.document_name(parse(["--", "| a |"])) == "readaloud"


def test_read_input_takes_the_file_then_stdin_then_text(tmp_path, monkeypatch):
    f = tmp_path / "notes.md"
    f.write_text("from the file\n", encoding="utf-8")
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "from stdin")
    parse = cli.build_parser().parse_args

    assert cli.read_input(parse(["-f", str(f), "ignored"])) == "from the file\n"
    assert cli.read_input(parse(["-f", "-"])) == "from stdin"
    assert cli.read_input(parse(["--conf", "md"])) == "from stdin"
    assert cli.read_input(parse(["--", "some", "words"])) == "some words"
    with pytest.raises(OSError):
        cli.read_input(parse(["-f", str(tmp_path / "missing.md")]))


def test_layers_for_input_follows_conf_then_the_suffix():
    cfg = config.Config()
    parse = cli.build_parser().parse_args
    assert cli.layers_for_input(parse(["-f", "a.py"]), cfg) == ("code", "python")
    assert cli.layers_for_input(parse(["-f", "a.PY"]), cfg) == ("code", "python")
    assert cli.layers_for_input(parse(["-f", "a.txt"]), cfg) == ("plain",)
    assert cli.layers_for_input(parse(["-f", "Makefile"]), cfg) == ()
    assert cli.layers_for_input(parse(["-f", "-"]), cfg) == ()
    assert cli.layers_for_input(parse([]), cfg) == ()
    # --conf: an extension's layers, or a layer by name, whatever the input
    assert cli.layers_for_input(parse(["--conf", "md"]), cfg) == ("markdown",)
    assert cli.layers_for_input(parse(["--conf", ".py", "-f", "x.txt"]), cfg) == ("code", "python")
    assert cli.layers_for_input(parse(["--conf", "Python"]), cfg) == ("python",)


def test_a_markdown_file_is_drawn_by_mdcat_and_its_tables_read_by_cell(
        tmp_path, launched, fake_mdcat, md_conf):
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    assert cli.main([str(notes), "--config", md_conf]) == 0

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
    (["-f", "{notes}"], "notes.md"),
    (["{notes}"], "notes.md"),                   # a lone TEXT naming a file
    (["--conf", "md", "-f", "-"], "readaloud"),
    (["--conf", "md"], "readaloud"),             # cat notes.md | readaloud --conf md
    (["--conf", "md", "--", TEAM_MD], "readaloud"),   # TEXT is the source
    (["--conf", "markdown"], "readaloud"),       # the layer by name
])
def test_markdown_source_can_come_from_any_input(argv, name, tmp_path, launched,
                                                 fake_mdcat, md_conf):
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    argv = [a.replace("{notes}", str(notes)) for a in argv]
    assert cli.main(["--config", md_conf, *argv]) == 0     # nothing after a "--"
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == [TEAM_MD]
    assert cell_texts(launched["doc"]) == TEAM_CELLS
    assert launched["doc_name"] == name


def never_stdin(*a, **k):  # pragma: no cover - must never run
    raise AssertionError("the file is the input, not stdin")


def test_a_lone_text_that_names_a_file_is_that_file_wherever_it_stands(
        tmp_path, monkeypatch, launched, fake_mdcat, md_conf):
    """"notes dot md" is never the document.  The flags round it do not
    matter: --start and --save take their own values."""
    monkeypatch.setattr("readaloud.ui.read_stdin_text", never_stdin)
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    assert cli.main(["--config", md_conf, "--start", "3", str(notes)]) == 0
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == [TEAM_MD]
    doc = launched["doc"]
    assert doc.chunks[launched["start_chunk"]].cell == (0, 0, 1)
    assert launched["doc_name"] == "notes.md"


def test_save_reads_a_file_given_after_the_options(
        tmp_path, monkeypatch, capsys, stub_voices, fake_mdcat, md_conf):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", never_stdin)
    notes = tmp_path / "notes.md"
    notes.write_text(TEAM_MD, encoding="utf-8")
    target = tmp_path / "out.wav"
    assert cli.main(["--config", md_conf, "--save", str(target),
                     str(notes)]) == cli.EXIT_OK
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == [TEAM_MD]
    assert [c.text for c in RecordingEngine.chunks if c.kind == "cell"] == \
        TEAM_CELLS
    assert capsys.readouterr().out.startswith(f"wrote {target}")


@pytest.mark.parametrize("argv, source", [
    (["--", "# Title"], "# Title"),
    (["{missing}"], "{missing}"),
    (["{dir}"], "{dir}"),
    (["{notes}", "{notes}"], "{notes} {notes}"),
    (["-f", "{notes}", "{notes}"], TEAM_MD),             # -f wins, as ever
], ids=["words", "missing", "directory", "two-files", "f-file"])
def test_any_other_text_is_the_source(
        argv, source, tmp_path, monkeypatch, launched, fake_mdcat, md_conf):
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

    assert cli.main(["--config", md_conf, "--conf", "md", *map(fill, argv)]) == 0
    assert [text for text, _mdcat, _cols in fake_mdcat["render"]] == \
        [fill(source)]


def test_start_counts_cells(launched, fake_mdcat, md_conf):
    """``--start N`` and "chunk N/M" count cells: the heading, Name, Role."""
    assert cli.main(["--conf", "md", "--start", "3", "--config", md_conf]) == 0
    doc = launched["doc"]
    assert doc.chunks[launched["start_chunk"]].cell == (0, 0, 1)


def test_mdcat_notices_take_their_turn_after_the_config_warnings(
        tmp_path, capsys, monkeypatch, launched, fake_mdcat):
    from test_integration import FakeEngine, FakePlayer, FakeScreen

    from readaloud.app import NOTICE_TTL, App

    conf = tmp_path / "readaloud.conf"
    conf.write_text("[config]\nspeed = 9.0\n" + MD_CONF, encoding="utf-8")
    assert cli.main(["--conf", "md", "--config", str(conf)]) == 0
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


def test_no_color_drops_colour_and_keeps_the_tables(launched, fake_mdcat, md_conf):
    assert cli.main(["--conf", "md", "--config", md_conf]) == 0
    assert any(r.style.fg is not None for line in launched["doc"].lines
               for r in line)

    assert cli.main(["--conf", "md", "--no-color", "--config", md_conf]) == 0
    doc = launched["doc"]
    assert all(r.style.fg is None and r.style.bg is None
               for line in doc.lines for r in line)
    assert any(r.style.bold for line in doc.lines for r in line)
    assert cell_texts(doc) == TEAM_CELLS


def test_mdcat_failure_is_one_line(monkeypatch, capsys, launched, fake_mdcat, md_conf):
    from readaloud import markdown

    # the line markdown.py makes of a panic
    crashed = markdown._failure(101, "thread 'main' panicked at x.rs:1:1:\nboom")

    def fail(text, *, mdcat, columns):
        raise markdown.MdcatError(crashed)

    monkeypatch.setattr("readaloud.markdown.render_markdown", fail)
    assert cli.main(["--conf", "md", "--config", md_conf]) == cli.EXIT_ERROR
    assert capsys.readouterr().err == f"readaloud: {crashed}\n"
    assert launched == {}


def test_empty_and_missing_input_are_the_usual_messages(
        tmp_path, monkeypatch, capsys, launched, fake_mdcat, md_conf):
    empty = tmp_path / "empty.md"
    empty.write_text("\n\n", encoding="utf-8")
    assert cli.main([str(empty), "--config", md_conf]) == cli.EXIT_USAGE
    assert "the input is empty" in capsys.readouterr().err

    missing = tmp_path / "missing.md"
    assert cli.main(["-f", str(missing), "--config", md_conf]) == cli.EXIT_ERROR
    assert f"could not read {missing}" in capsys.readouterr().err

    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: "")
    assert cli.main(["--no-config"]) == cli.EXIT_USAGE
    err = capsys.readouterr().err
    assert "no input" in err and "readaloud notes.md" in err and "--conf md" in err
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


def test_save_without_mdcat_says_so_on_stderr(
        tmp_path, monkeypatch, capsys, stub_voices, md_conf):
    """The missing-mdcat notice is raised before the headless warnings are
    printed, so --save, which has no status bar, still shows it."""
    def never(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("nothing is rendered without mdcat")

    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    monkeypatch.setattr("readaloud.markdown.find_mdcat", lambda: None)
    monkeypatch.setattr("readaloud.markdown.render_markdown", never)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: TEAM_MD)
    target = tmp_path / "team.wav"
    assert cli.main(["--conf", "md", "--save", str(target), "--config", md_conf]) == cli.EXIT_OK

    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {target}")
    assert (f"readaloud: {md_conf}: renderer = mdcat, but mdcat is not installed "
            "(brew install mdcat); the Markdown is read without it\n") in err
    assert target.exists()


def test_save_renders_at_80_columns_and_warns_on_stderr(
        tmp_path, monkeypatch, capsys, stub_voices, fake_mdcat, md_conf):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: TEAM_MD)
    target = tmp_path / "team.wav"
    assert cli.main(["--conf", "md", "--save", str(target), "--config", md_conf]) == cli.EXIT_OK

    assert [cols for _text, _mdcat, cols in fake_mdcat["render"]] == [80]
    assert fake_mdcat["width"] == 0          # the terminal is not asked
    out, err = capsys.readouterr()
    assert out.startswith(f"wrote {target}") and "readaloud:" not in out
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


def test_save_says_the_pronunciations_in_the_cells(
        tmp_path, monkeypatch, stub_voices, fake_mdcat):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    monkeypatch.setattr("readaloud.ui.read_stdin_text", lambda *a, **k: TEAM_MD)
    conf = write_conf(tmp_path, MD_CONF + "[pronunciations]\nBob = Robert\n")
    target = tmp_path / "team.wav"
    assert cli.main(["--conf", "md", "--config", str(conf), "--save",
                     str(target)]) == cli.EXIT_OK
    cells = [c.text for c in RecordingEngine.chunks if c.kind == "cell"]
    assert cells == [text.replace("Bob", "Robert") for text in TEAM_CELLS]


def test_a_bad_pronunciation_line_is_reported_and_costs_only_itself(
        tmp_path, monkeypatch, capsys, stub_voices):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    conf = write_conf(tmp_path, "[config]\nspeed = 1.5\n\n"
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


# ---------------------------------------------------------------------------
# sections for one file type
# ---------------------------------------------------------------------------


TYPE_CONF = r"""[config]
speed = 1.4

[config.python]
speed = 2
format = code

[parts.python]
ident = [A-Za-z_]\w*

[shapes.python]
dflt: /=\s*(?<v>[^,)]+)/ = with a default of $v
param: /(?<name>%{ident})\s*(?<type>:\s*[^,=)]+)?\s*(?<default>=\s*[^,)]+)?/ = $name ${default|dflt} ${type|:=type}
/def\s+(?<fn>%{ident})\s*\((?<params>[^()]*?),?\s*\)\s*(?<ret>->\s*[^:]+)?:/ = function $fn takes ${params|param|,=and} ${ret|->=and returns a}

[pronunciations.python]
str = string

[pronunciations]
"''" = empty string
"""

SIGNATURE = "def foo(\n    bar: int,\n    baz: str = '',\n) -> str:\n"


def test_a_source_files_indented_lines_are_cut_into_chunks(tmp_path, launched):
    body = "".join(f"    field{i}: int = {i}\n" for i in range(40))
    source = tmp_path / "x.py"
    source.write_text("class A:\n    x = 'doc'\n\n" + body, encoding="utf-8")
    # as Markdown, the indented lines after a blank line are one code block
    assert cli.main(["--config", str(write_conf(tmp_path, "")), "-f", str(source)]) == 0
    chunks = [c for c in launched["doc"].chunks if c.speakable]
    assert len(chunks) == 2 and chunks[1].kind == "code"
    assert len(chunks[1].text) > 380
    # as code, they are cut like a paragraph, at whole lines
    assert cli.main(["--config", str(write_conf(tmp_path, "format = code\n")), "-f",
                     str(source)]) == 0
    chunks = [c for c in launched["doc"].chunks if c.speakable]
    # the limit is on the text as written; the voice gets a full stop per line
    assert len(chunks) > 3 and all(len(c.text) <= 380 + c.text.count("\n") for c in chunks)
    # cut at line ends: every chunk opens on a field and closes on its value
    assert all(c.text.lstrip().startswith("field") and c.text.rstrip()[-1].isdigit()
               for c in chunks[1:])


def test_operators_are_words_in_a_source_file(tmp_path, launched):
    source = tmp_path / "x.py"
    source.write_text("return line == self.line and self.start <= col < self.end\n",
                      encoding="utf-8")
    assert cli.main(["--config", str(write_conf(tmp_path, "format = code\n")), "-f",
                     str(source)]) == 0
    assert [w.text for w in launched["doc"].words] == [
        "return", "line", "==", "self.line", "and", "self.start", "<=", "col", "<",
        "self.end"]
    # as Markdown, as before: words with a letter or digit only
    assert cli.main(["--config", str(write_conf(tmp_path, "")), "-f", str(source)]) == 0
    assert "==" not in [w.text for w in launched["doc"].words]


def test_config_may_be_a_directory(tmp_path, launched):
    d = tmp_path / "prefs"
    d.mkdir()
    (d / "readaloud.conf").write_text("[config]\nspeed = 1.4\n", encoding="utf-8")
    (d / "python.conf").write_text("[config]\nspeed = 2\n[pronunciations]\nid ID\n",
                                   encoding="utf-8")
    (tmp_path / "x.py").write_text("hello\n", encoding="utf-8")
    assert cli.main(["--config", str(d), "-f", str(tmp_path / "x.py")]) == 0
    assert launched["speed"] == 2.0
    assert launched["notices"] == [
        "config: python.conf: [pronunciations] line 4: 'id ID' has no \"=\"; "
        "write it as: text = how to say it"]
    assert cli.main(["--config", str(d)]) == 0
    assert launched["speed"] == 1.4



    conf = write_conf(tmp_path, TYPE_CONF)
    for name, speed in [("x.py", 2.0), ("x.md", 1.4)]:
        (tmp_path / name).write_text("hello\n", encoding="utf-8")
        assert cli.main(["--config", str(conf), "-f",
                         str(tmp_path / name)]) == 0
        assert launched["speed"] == speed, name
    assert cli.main(["--config", str(conf)]) == 0      # piped: no type
    assert launched["speed"] == 1.4


def test_a_flag_still_beats_a_type_setting(tmp_path, launched):
    (tmp_path / "x.py").write_text("hello\n", encoding="utf-8")
    assert cli.main(["--config", str(write_conf(tmp_path, TYPE_CONF)),
                     "-s", "1.1", "-f", str(tmp_path / "x.py")]) == 0
    assert launched["speed"] == 1.1


def test_a_markdown_file_named_in_text_gets_its_layer(
        tmp_path, launched, fake_mdcat):
    conf = write_conf(tmp_path, "[config.markdown]\nspeed = 1.7\n")
    (tmp_path / "x.md").write_text(TEAM_MD, encoding="utf-8")
    assert cli.main(["--config", str(conf), str(tmp_path / "x.md")]) == 0
    assert launched["speed"] == 1.7
    assert fake_mdcat["render"] == []          # no renderer set: read as written


def test_a_type_shape_reads_a_python_def(tmp_path, monkeypatch, stub_voices):
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    source = tmp_path / "x.py"
    source.write_text(SIGNATURE, encoding="utf-8")
    assert cli.main(["--config", str(write_conf(tmp_path, TYPE_CONF)),
                     "--save", str(tmp_path / "out.wav"), "-f",
                     str(source)]) == cli.EXIT_OK
    assert [c.text for c in RecordingEngine.chunks] == [
        "function foo\ntakes bar type int\nand baz with a default of empty "
        "string type string\nand returns a string"]


SYNTAX_CONF = TYPE_CONF.replace("[shapes.python]", "[syntax.python]\n"
    "function_definition = function $name $parameters $return_type\n"
    "function_definition.return_type = and returns type $0\n"
    "parameters = takes ${*|,=and}\n"
    "typed_parameter = ${*|:=type}\n"
    "typed_default_parameter = $name type $type with a default of $value\n"
    "\n[shapes.python]")


def test_syntax_rules_read_a_python_file_through_its_grammar(
        tmp_path, monkeypatch, stub_voices):
    pytest.importorskip("tree_sitter_python")
    monkeypatch.setattr("readaloud.speech.Engine", RecordingEngine)
    source = tmp_path / "x.py"
    source.write_text(SIGNATURE, encoding="utf-8")
    assert cli.main(["--config", str(write_conf(tmp_path, SYNTAX_CONF)),
                     "--save", str(tmp_path / "out.wav"), "-f",
                     str(source)]) == cli.EXIT_OK
    # the rules win over the regex shapes for the same def
    assert [c.text for c in RecordingEngine.chunks] == [
        "function foo\ntakes bar type int\nand baz type string with a default "
        "of empty string\nand returns type string"]


def test_a_python_file_is_coloured_by_its_grammar(tmp_path, launched):
    pytest.importorskip("tree_sitter_python")
    source = tmp_path / "x.py"
    source.write_text("def f():\n    pass  # c\n", encoding="utf-8")
    conf = write_conf(tmp_path, "[config.python]\nformat = code\n")
    assert cli.main(["--config", str(conf), "-f", str(source)]) == 0
    runs = [(r.text, r.style.fg) for r in launched["doc"].lines[0]]
    assert runs[:3] == [("def", 5), (" ", None), ("f", 4)]
    assert launched["notices"] == []
    # --no-color, and highlight = no, leave it plain
    assert cli.main(["--config", str(conf), "--no-color", "-f", str(source)]) == 0
    assert {r.style.fg for r in launched["doc"].lines[0]} == {None}
    conf = write_conf(tmp_path, "[config.python]\nformat = code\nhighlight = no\n")
    assert cli.main(["--config", str(conf), "-f", str(source)]) == 0
    assert {r.style.fg for r in launched["doc"].lines[0]} == {None}


def test_tree_prints_the_files_syntax_tree_and_exits(tmp_path, capsys, launched):
    pytest.importorskip("tree_sitter_python")
    source = tmp_path / "x.py"
    source.write_text("def f(a):\n    pass\n", encoding="utf-8")
    assert cli.main(["--no-config", "--tree", str(source)]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("module [0..")
    assert "  function_definition [0..18]\n" in out
    assert '    name: identifier [4..5]  "f"\n' in out
    assert launched == {}                       # the TUI never ran
    assert cli.main(["--no-config", "-f", str(source), "--tree"]) == cli.EXIT_OK
    assert "function_definition" in capsys.readouterr().out
    (tmp_path / "x.xyz").write_text("def f(): pass\n", encoding="utf-8")
    assert cli.main(["--no-config", "--tree", str(tmp_path / "x.xyz")]) == cli.EXIT_ERROR
    assert "no grammar is known for .xyz" in capsys.readouterr().err
    assert cli.main(["--no-config", "--tree"]) == cli.EXIT_USAGE
    assert "--tree needs a file" in capsys.readouterr().err


def test_a_file_with_no_known_grammar_is_read_without_one(tmp_path, launched):
    conf = write_conf(tmp_path, "[syntax]\nfunction_definition = f $name\n")
    (tmp_path / "x.xyz").write_text("def f(): pass\n", encoding="utf-8")
    assert cli.main(["--config", str(conf), "-f", str(tmp_path / "x.xyz")]) == 0
    assert launched["notices"] == [
        "config: [syntax]: no grammar is known for .xyz files; name one in "
        "[files], or set language"]
    assert launched["doc"].chunks[0].text == "def f(): pass"


def test_a_missing_grammar_and_an_unknown_kind_are_notices(tmp_path, launched):
    pytest.importorskip("tree_sitter_python")
    conf = write_conf(tmp_path, "[config.python]\nlanguage = nothing_at_all\n"
                                "[syntax.python]\ndef_statement = f $name\n")
    (tmp_path / "x.py").write_text("def f(): pass\n", encoding="utf-8")
    assert cli.main(["--config", str(conf), "-f", str(tmp_path / "x.py")]) == 0
    assert launched["notices"] == [
        "config: no grammar for nothing_at_all: install the "
        "tree-sitter-nothing-at-all package next to readaloud (uv tool "
        "install readaloud --with tree-sitter-nothing-at-all)"]
    conf = write_conf(tmp_path, "[syntax.python]\ndef_statement = f $name\n")
    assert cli.main(["--config", str(conf), "-f", str(tmp_path / "x.py")]) == 0
    assert launched["notices"] == [
        "config: [syntax] def_statement: no node is a 'def_statement'"]


def test_a_type_problem_reaches_the_status_bar_with_its_label(
        tmp_path, launched):
    conf = write_conf(tmp_path, "[pronunciations.python]\nid ID\n")
    (tmp_path / "x.py").write_text("hello\n", encoding="utf-8")
    assert cli.main(["--config", str(conf), "-f", str(tmp_path / "x.py")]) == 0
    assert launched["notices"] == [
        "config: [pronunciations.python] line 2: 'id ID' has no \"=\"; "
        "write it as: text = how to say it"]


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

    # in the reader it takes its turn after the config warnings and mdcat's
    conf = write_conf(tmp_path, MD_CONF + "[pronunciations]\nid = ID\nBob Robert\n")
    assert cli.main(["--conf", "md", "--config", str(conf)]) == 0
    doc = launched["doc"]
    assert doc.respell_failures == len(doc.speakable_chunks) == 11
    assert launched["notices"] == [
        "config: [pronunciations] line 5: 'Bob Robert' has no \"=\"; "
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
        f"readaloud: {conf}: [pronunciations] line 5: 'Bob Robert' has no "
        "\"=\"; write it as: text = how to say it",
        "readaloud: pronunciations: 1 chunk is read as written "
        "(a pronunciation could not be applied)"]
    assert err.count("readaloud:") == 2
    assert [c.text for c in RecordingEngine.chunks] == ["One id."]


def test_no_config_or_no_pronunciations_never_respell(
        tmp_path, monkeypatch, launched):
    sabotage_find(monkeypatch)             # never called without an entry
    conf = write_conf(tmp_path, "[pronunciations]\nid = ID\n"
                                "[shapes]\n/(?<f>\\S+):(?<n>\\d+)/ = $f at $n\n")
    assert cli.main(["--config", str(conf), "--no-config", "foo.id"]) == 0
    assert launched["doc"].respell_failures == 0
    assert launched["notices"] == []
    assert cli.main(["--config", str(conf), "--no-config", "a.py:1"]) == 0
    assert launched["doc"].respell_failures == 0
    assert launched["doc"].chunks[0].text == "a.py:1"
    assert cli.main(["foo.id"]) == 0       # the sandbox has no config file
    assert launched["doc"].chunks[0].text == "foo.id"


def test_the_config_file_turns_shapes_on(tmp_path, launched):
    conf = write_conf(tmp_path, "[parts]\nnum = \\d+\n[shapes]\n"
                                "/(?<f>\\S+\\.rs):(?<l>%{num}):(?<c>%{num})/"
                                " = $f line $l, column $c\n")
    assert cli.main(["--config", str(conf), "main.rs:12:5"]) == 0
    assert launched["doc"].chunks[0].text == "main.rs line 12, column 5"
