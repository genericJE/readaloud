"""Regression tests for `readaloud.cli` argument and destination validation.

Two defects are covered:

1. `--voice` was never checked against the repo's voice list, so a typo loaded
   the model fine and then failed at synthesis time, once per chunk -- the TUI
   raced silently to the end of the document and exited 0.
2. `--save` to an unwritable path only discovered the problem after the whole
   document had been synthesized, and `wave.open(path, "wb")` then printed an
   "Exception ignored in Wave_write.__del__" traceback on top of the friendly
   message.

Nothing here loads the TTS model: `list_voices` is stubbed and the `--save`
pre-flight must return before `Engine` is ever constructed.
"""

from __future__ import annotations

import gc
import sys

import pytest

from readaloud import cli

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
