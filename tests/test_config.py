"""Tests for `readaloud.config` -- the ``~/.readaloud.conf`` preferences file.

Two properties matter more than any individual key:

1. `load` never raises.  It is called before the TUI starts, on a file the user
   hand-edits, so a stray line, a binary blob, a directory where the file
   should be or a file nobody may read must all degrade to "defaults plus a
   warning" rather than to a traceback in front of the reader.
2. The generated template round-trips.  Parsing `template` -- as written, and
   with every key uncommented -- must yield exactly `Config()`, otherwise the
   documented defaults and the real defaults have drifted apart.

The ``[pronunciations]`` section adds a third: a line there that makes no
sense costs that line and nothing else, never the settings.

Every test writes to `tmp_path`; the autouse fixture below also re-points
`config.DEFAULT_PATH` there, so even a test that forgets to pass a path cannot
touch the real ``~/.readaloud.conf``.
"""

from __future__ import annotations

import os
import random
import re
import stat
import unicodedata
from pathlib import Path

import pytest

from readaloud import config
from readaloud.config import Config

DEFAULTS = Config()

#: Captured at import time, before the autouse fixture re-points the module
#: attribute at `tmp_path`.  Reloading the module to recover it would rebind
#: `Config` to a fresh class and quietly break every `cfg == DEFAULTS` below.
REAL_DEFAULT_PATH = config.DEFAULT_PATH


@pytest.fixture(autouse=True)
def _sandbox_default_path(tmp_path, monkeypatch):
    """No test may read or write the developer's own preferences file."""
    monkeypatch.setattr(config, "DEFAULT_PATH", tmp_path / "sandbox.conf")
    yield


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #


def test_default_path_is_dot_readaloud_conf_in_home():
    assert REAL_DEFAULT_PATH == Path.home() / ".readaloud.conf"


def test_the_real_conf_file_is_never_touched(tmp_path):
    # Belt and braces: the sandbox fixture must be what every no-argument call
    # sees, so no test in this file can read or write the developer's own file.
    assert config.DEFAULT_PATH == tmp_path / "sandbox.conf"
    assert config.DEFAULT_PATH != REAL_DEFAULT_PATH


def test_missing_file_is_not_a_warning(tmp_path):
    cfg, warnings = config.load(tmp_path / "nope.conf")
    assert cfg == DEFAULTS
    assert warnings == []


def test_load_with_no_argument_uses_default_path(tmp_path):
    write(tmp_path / "sandbox.conf", "[readaloud]\nspeed = 1.5\n")
    cfg, warnings = config.load()
    assert (cfg.speed, warnings) == (1.5, [])


def test_empty_file_yields_defaults(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf", ""))
    assert cfg == DEFAULTS
    assert warnings == []


def test_comments_only_file_yields_defaults(tmp_path):
    body = "# just a note\n; and another\n\n[readaloud]\n# nothing set\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg == DEFAULTS
    assert warnings == []


def test_config_dataclass_defaults():
    assert DEFAULTS == Config(
        voice="af_heart", speed=1.0, lang="", chars=380,
        repo="mlx-community/Kokoro-82M-4bit", sentences=4, prefetch=2,
        device="", color=True, follow_lead=20, follow_margin=2,
        media_keys=True,
    )


# --------------------------------------------------------------------------- #
# every key
# --------------------------------------------------------------------------- #


ALL_KEYS = """\
[readaloud]
voice = bf_emma
speed = 1.25
lang = b
repo = hexgrad/Kokoro-82M
sentences = 7
chars = 500
prefetch = 0
device = MacBook Pro Speakers
color = false
follow_lead = 12
follow_margin = 5
media_keys = false
"""


def test_every_key_is_read(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf", ALL_KEYS))
    assert warnings == []
    assert cfg == Config(
        voice="bf_emma", speed=1.25, lang="b", repo="hexgrad/Kokoro-82M",
        sentences=7, chars=500, prefetch=0, device="MacBook Pro Speakers",
        color=False, follow_lead=12, follow_margin=5, media_keys=False,
    )


@pytest.mark.parametrize("key", ["color", "media_keys"])
@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("yes", True), ("on", True), ("1", True),
    ("false", False), ("FALSE", False), ("no", False), ("off", False),
    ("0", False),
])
def test_boolean_spellings(tmp_path, key, raw, expected):
    body = f"[readaloud]\n{key} = {raw}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (getattr(cfg, key), warnings) == (expected, [])


def test_keys_are_case_insensitive_and_values_are_stripped(tmp_path):
    body = "[readaloud]\nVOICE =   am_adam   \nSpeed = 2\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed, warnings) == ("am_adam", 2.0, [])


def test_quoted_values_and_inline_comments(tmp_path):
    body = '[readaloud]\nvoice = "af_sarah"   # the calm one\nspeed = 1.5 ; faster\n'
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed, warnings) == ("af_sarah", 1.5, [])


def test_empty_string_keys_stay_empty(tmp_path):
    body = "[readaloud]\nlang =\ndevice =\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.lang, cfg.device, warnings) == ("", "", [])


def test_follow_lead_zero_is_kept_not_treated_as_unset(tmp_path):
    body = "[readaloud]\nfollow_lead = 0\nfollow_margin = 0\nprefetch = 0\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.follow_lead, cfg.follow_margin, cfg.prefetch) == (0, 0, 0)
    assert warnings == []


# --------------------------------------------------------------------------- #
# clamping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key,given,expected", [
    ("speed", "0.1", 0.5),
    ("speed", "9", 3.0),
    ("sentences", "0", 1),
    ("sentences", "-4", 1),
    ("chars", "0", 1),
    ("prefetch", "-1", 0),
    ("follow_lead", "-5", 0),
    ("follow_margin", "-2", 0),
])
def test_out_of_range_values_clamp_and_warn(tmp_path, key, given, expected):
    body = f"[readaloud]\n{key} = {given}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert getattr(cfg, key) == expected
    assert len(warnings) == 1
    assert key in warnings[0]


@pytest.mark.parametrize("value", ["0.5", "1.0", "3.0"])
def test_speed_edges_are_in_range(tmp_path, value):
    body = f"[readaloud]\nspeed = {value}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, warnings) == (float(value), [])


def test_clamping_one_key_leaves_the_others_alone(tmp_path):
    body = "[readaloud]\nspeed = 99\nvoice = bf_emma\nfollow_lead = 30\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.voice, cfg.follow_lead) == (3.0, "bf_emma", 30)
    assert len(warnings) == 1 and "speed" in warnings[0]


# --------------------------------------------------------------------------- #
# malformed values
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key,given", [
    ("speed", "fast"),
    ("speed", ""),
    ("sentences", "3.7"),
    ("sentences", "four"),
    ("chars", "lots"),
    ("prefetch", "yes"),
    ("follow_lead", "20 rows"),
    ("follow_margin", "?"),
    ("color", "maybe"),
    ("media_keys", "sometimes"),
])
def test_malformed_value_falls_back_to_default_and_warns(tmp_path, key, given):
    body = f"[readaloud]\n{key} = {given}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert getattr(cfg, key) == getattr(DEFAULTS, key)
    assert len(warnings) == 1
    assert key in warnings[0]


def test_one_bad_key_never_discards_the_rest_of_the_file(tmp_path):
    body = ("[readaloud]\nvoice = am_adam\nspeed = quick\n"
            "follow_lead = 40\ncolor = off\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.voice == "am_adam"
    assert cfg.speed == DEFAULTS.speed
    assert cfg.follow_lead == 40
    assert cfg.color is False
    assert len(warnings) == 1 and "speed" in warnings[0]


# --------------------------------------------------------------------------- #
# unknown keys and sections
# --------------------------------------------------------------------------- #


def test_unknown_key_warns_by_name_and_is_ignored(tmp_path):
    body = "[readaloud]\nvolume = 11\nspeed = 1.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 1.5
    assert len(warnings) == 1 and "volume" in warnings[0]
    assert not hasattr(cfg, "volume")


def test_a_pronunciation_under_readaloud_is_pointed_at_its_section(tmp_path):
    # a file from before [pronunciations] existed ends inside [readaloud]
    path = write(tmp_path / "c.conf",
                 "[readaloud]\nspeed = 1.5\nid = ID\ncolour = true\n")
    cfg, warnings = config.load(path)
    assert (cfg.speed, cfg.pronunciations) == (1.5, ())
    assert warnings == [
        f"{path}: unknown key 'id' (ignored; a pronunciation goes under "
        f"[pronunciations])",
        f"{path}: unknown key 'colour' (ignored)"]      # close to a setting


def test_unknown_section_is_ignored_with_a_warning(tmp_path):
    body = "[readaloud]\nspeed = 2\n\n[colors]\nhighlight = red\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 2.0
    assert len(warnings) == 1 and "colors" in warnings[0]


# --------------------------------------------------------------------------- #
# forgiving shapes
# --------------------------------------------------------------------------- #


def test_missing_section_header_is_tolerated(tmp_path):
    body = "speed = 1.75\nvoice = bf_emma\nfollow_lead = 8\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.voice, cfg.follow_lead) == (1.75, "bf_emma", 8)
    assert warnings == []


def test_keys_before_the_section_header_are_tolerated(tmp_path):
    body = "voice = am_adam\n\n[readaloud]\nspeed = 1.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed) == ("am_adam", 1.5)
    assert warnings == []


def test_duplicate_keys_do_not_raise_and_the_last_wins(tmp_path):
    body = "[readaloud]\nspeed = 1.5\nspeed = 2.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 2.5
    assert warnings == []


def test_duplicate_sections_do_not_raise(tmp_path):
    body = "[readaloud]\nspeed = 1.5\n[readaloud]\nvoice = bf_emma\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.voice) == (1.5, "bf_emma")


def test_percent_signs_in_values_do_not_raise(tmp_path):
    # configparser's default interpolation would choke on a bare '%'.
    body = "[readaloud]\ndevice = 100%% Output\nvoice = af_heart\n"
    cfg, _ = config.load(write(tmp_path / "c.conf", body))
    assert "%" in cfg.device


# --------------------------------------------------------------------------- #
# load() never raises
# --------------------------------------------------------------------------- #


def test_binary_garbage_does_not_raise(tmp_path):
    path = tmp_path / "c.conf"
    path.write_bytes(bytes(range(256)) * 8 + b"\x00\xff\xfe\x00")
    cfg, warnings = config.load(path)
    assert isinstance(cfg, Config)
    assert cfg == DEFAULTS
    assert warnings  # the user should hear that the file was ignored


def test_nonsense_text_does_not_raise(tmp_path):
    body = "]]] this is not ini [[[\n== ??? ==\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg == DEFAULTS
    assert warnings


def test_directory_where_the_file_should_be(tmp_path):
    d = tmp_path / "c.conf"
    d.mkdir()
    cfg, warnings = config.load(d)
    assert cfg == DEFAULTS
    assert len(warnings) == 1 and str(d) in warnings[0]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_file(tmp_path):
    path = write(tmp_path / "c.conf", "[readaloud]\nspeed = 2\n")
    path.chmod(0o000)
    try:
        cfg, warnings = config.load(path)
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert cfg == DEFAULTS
    assert len(warnings) == 1 and str(path) in warnings[0]


def test_very_long_and_weird_lines_do_not_raise(tmp_path):
    body = "[readaloud]\n" + "x" * 10000 + " = " + "y" * 10000 + "\n\n\n\t\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg == DEFAULTS
    assert warnings


def test_load_accepts_a_string_path(tmp_path):
    path = write(tmp_path / "c.conf", "[readaloud]\nspeed = 1.5\n")
    cfg, warnings = config.load(str(path))
    assert (cfg.speed, warnings) == (1.5, [])


# --------------------------------------------------------------------------- #
# pronunciations: one line
# --------------------------------------------------------------------------- #


def test_pronunciations_are_empty_by_default(tmp_path):
    assert (config.PRONUNCIATIONS, DEFAULTS.pronunciations) == (
        "pronunciations", ())
    assert "PRONUNCIATIONS" in config.__all__
    cfg, warnings = config.load(write(tmp_path / "c.conf", ALL_KEYS))
    assert (cfg.pronunciations, warnings) == ((), [])


@pytest.mark.parametrize("line,pair", [
    # configparser would lowercase these, split them at ":", or take them
    # for a section header
    ("id = ID", ("id", "ID")),
    ("GIF = jif", ("GIF", "jif")),
    ("C# = C sharp", ("C#", "C sharp")),
    ("std::vector = standard vector", ("std::vector", "standard vector")),
    ("[x] = checkbox", ("[x]", "checkbox")),
    ("[1] = footnote one", ("[1]", "footnote one")),
    # the first "=" with spaces round it separates; failing that, the first
    ("== = equals equals", ("==", "equals equals")),
    ("a == b = x", ("a == b", "x")),
    ("id=ID", ("id", "ID")),
    ("x = C#", ("x", "C#")),
    # quotes
    ('"#include" = hash include', ("#include", "hash include")),
    ("';' = semicolon", (";", "semicolon")),
    ('"a = b" = c', ("a = b", "c")),
    ('"speed" = spead', ("speed", "spead")),
    ('"id"="ID"', ("id", "ID")),
    ('C# = "C # sharp"  # the quotes keep the #', ("C#", "C # sharp")),
    ('id = "/aid/"', ("id", "/aid/")),     # quoted, it is only text
    ('x = "a" b', ("x", '"a" b')),         # quotes that do not wrap it all
    # inline comments
    ("id = ID   # eye dee", ("id", "ID")),
    ("id = ID ; eye dee", ("id", "ID")),
    # whitespace and unicode
    ("  kubectl = cube control  ", ("kubectl", "cube control")),
    ("New\t York   City =\tNYC", ("New York City", "NYC")),
    ("cafe\u0301 = caff ay", ("caf\u00e9", "caff ay")),
    ("id = ID\r", ("id", "ID")),
])
def test_pronunciation_lines(tmp_path, line, pair):
    body = f"[pronunciations]\n{line}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.pronunciations, warnings) == ((pair,), [])


@pytest.mark.parametrize("line", [
    "", "   ", "# a note", "; a note", "   # id = ID",
    "#include = hash include",
])
def test_blank_and_comment_lines_are_nothing(tmp_path, line):
    body = f"[pronunciations]\n{line}\nid = ID\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.pronunciations, warnings) == ((("id", "ID"),), [])


def test_crlf_file(tmp_path):
    path = tmp_path / "c.conf"
    path.write_bytes(b"[readaloud]\r\nspeed = 1.5\r\n\r\n[pronunciations]\r\n"
                     b"id = ID\r\nkubectl = cube control  # k\r\n")
    cfg, warnings = config.load(path)
    assert warnings == []
    assert cfg.speed == 1.5
    assert cfg.pronunciations == (("id", "ID"), ("kubectl", "cube control"))


def test_pairs_keep_the_file_order(tmp_path):
    body = ("[pronunciations]\nkubectl = cube control\nid = ID\n"
            "New York City = NYC\n")
    cfg, _ = config.load(write(tmp_path / "c.conf", body))
    assert cfg.pronunciations == (
        ("kubectl", "cube control"), ("id", "ID"), ("New York City", "NYC"))


# --------------------------------------------------------------------------- #
# pronunciations: problems
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("line,problem", [
    ("no equals here",
     "'no equals here' has no \"=\"; write it as: text = how to say it"),
    ("x" * 50,
     f"'{'x' * 40}...' has no \"=\"; write it as: text = how to say it"),
    ("= equals",
     'nothing before the "="; put quotes round text that has an "=" in it'),
    ('"" = nothing',
     'nothing before the "="; put quotes round text that has an "=" in it'),
    ("x\x00y = z",
     "the text 'x\\x00y' has a control character (U+0000)"),
    ("bell = \x07ding",
     "the pronunciation '\\x07ding' has a control character (U+0007)"),
    ("caf\ufffd = cafe", "the text 'caf\ufffd' is not valid UTF-8"),
    ("id =", "'id' has nothing after the \"=\""),
    ("id = # only a note", "'id' has nothing after the \"=\""),
    ('id = "  "', "'id' has nothing after the \"=\""),
    ("x == y",
     "'x' = '= y': put quotes round text that has an \"=\" in it"),
    ("x ==y",
     "'x' = '=y': put quotes round text that has an \"=\" in it"),
    ("id = /aidi/",
     "'id': phonemes (/.../) are not supported; write it the way it sounds"),
    ("id = [ID](/aidi/)",
     "'id': [text](...) is not supported; write it the way it sounds"),
    ('id = "[ID](/aidi/)"',
     "'id': [text](...) is not supported; write it the way it sounds"),
    ("tick = \u2713",
     "'tick': '\u2713' has nothing to say (no letters or digits)"),
    ("speed = 1.5",
     "'speed' is a setting: move it under [readaloud], or put quotes round "
     "the word to pronounce it"),
    ("Follow_Lead = 3",
     "'Follow_Lead' is a setting: move it under [readaloud], or put quotes "
     "round the word to pronounce it"),
])
def test_each_problem_skips_its_line_with_a_warning(tmp_path, line, problem):
    path = write(tmp_path / "c.conf", f"[pronunciations]\n{line}\nid = ID\n")
    cfg, warnings = config.load(path)
    assert cfg.pronunciations == (("id", "ID"),)
    assert warnings == [f"{path}: [pronunciations] line 2: {problem}"]


def test_bytes_that_are_not_utf8_are_a_problem(tmp_path):
    path = tmp_path / "c.conf"
    path.write_bytes(b"[pronunciations]\ncaf\xe9 = cafe\nid = ID\n")
    cfg, warnings = config.load(path)
    assert cfg.pronunciations == (("id", "ID"),)
    assert len(warnings) == 1 and "line 2" in warnings[0]
    assert "not valid UTF-8" in warnings[0]


def test_a_later_duplicate_wins_and_takes_its_own_place(tmp_path):
    path = write(tmp_path / "c.conf",
                 "[pronunciations]\nid = eye dee\nkubectl = cube control\n"
                 "id  =  ID\n")
    cfg, warnings = config.load(path)
    assert cfg.pronunciations == (("kubectl", "cube control"), ("id", "ID"))
    assert warnings == [
        f"{path}: [pronunciations] line 4: 'id' is also on line 2; "
        f"line 4 wins"]


def test_duplicates_compare_with_case(tmp_path):
    body = "[pronunciations]\nid = ID\nId = ID\nID = I D\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.pronunciations == (("id", "ID"), ("Id", "ID"), ("ID", "I D"))
    assert warnings == []


def test_duplicates_compare_the_normalised_text(tmp_path):
    body = "[pronunciations]\nNew York = the big apple\nNew   York = NYC\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.pronunciations == (("New York", "NYC"),)
    assert len(warnings) == 1 and "also on line 2; line 3 wins" in warnings[0]


def test_only_five_problems_are_listed(tmp_path):
    body = "[pronunciations]\n" + "".join(f"bad line {i}\n" for i in range(8))
    path = write(tmp_path / "c.conf", body + "id = ID\n")
    cfg, warnings = config.load(path)
    assert cfg.pronunciations == (("id", "ID"),)
    assert warnings[:5] == [
        f"{path}: [pronunciations] line {n}: 'bad line {n - 2}' has no \"=\"; "
        f"write it as: text = how to say it" for n in range(2, 7)]
    assert warnings[5:] == [
        f"{path}: [pronunciations]: 3 more problems not shown"]


def test_one_problem_past_five_is_counted_in_the_singular(tmp_path):
    body = "[pronunciations]\n" + "".join(f"bad line {i}\n" for i in range(6))
    path = write(tmp_path / "c.conf", body)
    _, warnings = config.load(path)
    assert warnings[5:] == [
        f"{path}: [pronunciations]: 1 more problem not shown"]


def test_five_problems_are_all_listed(tmp_path):
    body = "[pronunciations]\n" + "".join(f"bad line {i}\n" for i in range(5))
    _, warnings = config.load(write(tmp_path / "c.conf", body))
    assert len(warnings) == 5
    assert not any("not shown" in w for w in warnings)


def test_problems_count_file_line_numbers(tmp_path):
    body = ("# my settings\n"
            "[readaloud]\n"
            "speed = 1.5\n"
            "\n"
            "[pronunciations]\n"
            "id = ID\n"
            "not a pronunciation\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("id", "ID"),))
    assert len(warnings) == 1 and "[pronunciations] line 7:" in warnings[0]


def test_problems_come_after_the_settings_warnings(tmp_path):
    body = "[pronunciations]\nnot a pronunciation\n[readaloud]\nspeed = fast\n"
    path = write(tmp_path / "c.conf", body)
    _, warnings = config.load(path)
    assert len(warnings) == 2
    assert warnings[0].startswith(f"{path}: speed:")
    assert warnings[1].startswith(f"{path}: [pronunciations] line 2:")


# --------------------------------------------------------------------------- #
# pronunciations: next to the settings
# --------------------------------------------------------------------------- #


def test_a_bad_pronunciation_line_keeps_the_settings(tmp_path):
    body = ("[readaloud]\nspeed = 1.5\n\n[pronunciations]\nid = ID\n"
            "this line has no equals\n    kubectl = cube control\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 1.5
    assert cfg.pronunciations == (("id", "ID"), ("kubectl", "cube control"))
    assert len(warnings) == 1 and "line 6" in warnings[0]


def test_settings_before_any_header_and_pronunciations(tmp_path):
    body = "speed = 1.5\n[pronunciations]\nid = ID\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("id", "ID"),))
    assert warnings == []


def test_a_readaloud_header_with_a_comment_ends_the_section(tmp_path):
    body = "[pronunciations]\nid = ID\n[readaloud]  # c\nspeed = 1.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("id", "ID"),))
    assert warnings == []


@pytest.mark.parametrize("header", [
    "[Pronunciations]", "[ pronunciations ]", "[pronunciations] ; words",
])
def test_the_section_name_ignores_case_and_spaces(tmp_path, header):
    body = f"[readaloud]\nspeed = 1.5\n{header}\nid = ID\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("id", "ID"),))
    assert warnings == []


def test_several_pronunciations_sections_are_read_as_one(tmp_path):
    body = ("[pronunciations]\nid = ID\n[readaloud]\nspeed = 2\n"
            "[pronunciations]\nkubectl = cube control\nid = eye dee\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 2.0
    assert cfg.pronunciations == (("kubectl", "cube control"),
                                  ("id", "eye dee"))
    assert len(warnings) == 1 and "also on line 2; line 7 wins" in warnings[0]


def test_an_entry_in_brackets_then_a_bad_line_keeps_the_settings(tmp_path):
    body = ("[readaloud]\nspeed = 1.5\n[pronunciations]\n[1] = footnote one\n"
            "no equals here\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("[1]", "footnote one"),))
    assert len(warnings) == 1 and "line 5" in warnings[0]


def test_a_misspelled_section_keeps_the_settings_and_says_so(tmp_path):
    path = write(tmp_path / "c.conf",
                 "[readaloud]\nspeed = 1.5\n\n[pronunciation]\nid = ID\n"
                 "no equals here\n")
    cfg, warnings = config.load(path)
    assert (cfg.speed, cfg.pronunciations) == (1.5, ())
    assert warnings == [
        f"{path}: ignoring unknown section [pronunciation] "
        f"(did you mean [pronunciations]?)"]


def test_a_misspelled_readaloud_section_says_so(tmp_path):
    path = write(tmp_path / "c.conf", "[readalod]\nspeed = 1.5\n")
    cfg, warnings = config.load(path)
    assert cfg == DEFAULTS
    assert warnings == [
        f"{path}: ignoring unknown section [readalod] "
        f"(did you mean [readaloud]?)"]


@pytest.mark.parametrize("body", [
    "[DEFAULT]\nspeed = 1.5\n[readaloud]\nvoice = bf_emma\n",
    "[readaloud]\nvoice = bf_emma\n[pronunciations]\nid = ID\n"
    "[DEFAULT]\nspeed = 1.5\n",
])
def test_a_default_section_still_fills_the_settings(tmp_path, body):
    # configparser's [DEFAULT] was never an unknown section: its keys are
    # every section's, [readaloud]'s included.
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.voice) == (1.5, "bf_emma")
    assert warnings == []


def test_an_unknown_section_ends_the_pronunciations(tmp_path):
    path = write(tmp_path / "c.conf",
                 "[pronunciations]\nid = ID\n[colors]\nhighlight = red\n")
    cfg, warnings = config.load(path)
    assert cfg.pronunciations == (("id", "ID"),)
    assert warnings == [f"{path}: ignoring unknown section [colors]"]


@pytest.mark.parametrize("line", ["speed = 1.5", "speed: 1.5", "Speed :1.5"])
def test_a_setting_appended_to_the_template_is_not_a_pronunciation(tmp_path,
                                                                   line):
    path = write(tmp_path / "c.conf", config.template() + line + "\n")
    cfg, warnings = config.load(path)
    assert cfg == DEFAULTS
    assert len(warnings) == 1
    assert "is a setting: move it under [readaloud]" in warnings[0]


def test_a_colon_after_a_word_that_is_not_a_setting_has_no_equals(tmp_path):
    path = write(tmp_path / "c.conf", "[pronunciations]\nNote: hi\n")
    _, warnings = config.load(path)
    assert warnings == [f"{path}: [pronunciations] line 2: 'Note: hi' has no "
                        f"\"=\"; write it as: text = how to say it"]


@pytest.mark.parametrize("section", ["[notes]\n# nothing yet\n",
                                     "[pronunciations]\n# id = ID\n"])
def test_an_indented_header_after_a_lifted_section_is_a_header(tmp_path,
                                                                section):
    # configparser reads the empty lines left in place of the section as
    # part of voice's value; the indented header after them must not be
    body = ("[readaloud]\nvoice = af_bella\n\n" + section
            + "\n    [readaloud]\n    speed = 1.5\n")
    cfg, _warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed) == ("af_bella", 1.5)


@pytest.mark.parametrize("body,speed,voice,warnings", [
    # an indented header under a key is more of its value, as configparser
    # has always read it, even right after a lifted section
    ("[readaloud]\nspeed = 1.5\nvoice = af_sky\n  [colors]\n  [readaloud]\n"
     "  more\n", 1.5, "af_sky\n[colors]\n[readaloud]\nmore", []),
    ("[readaloud]\nspeed = 1.5\n  [colors]\nvoice = af_sky\n", 1.0, "af_sky",
     ["speed: '1.5\\n[colors]' is not a number; using 1.0"]),
    ("[colors]\nx = 1\n  [readaloud]\nspeed = 1.5\n", 1.0, "af_heart",
     ["ignoring unknown section [colors]"]),
    # a comment is found the way configparser finds it, # and ; in turns
    ("[readaloud]\nspeed = 2\n[x#] #] ;z\n", 2.0, "af_heart",
     ["ignoring unknown section [x#] #]"]),
    ("[readaloud]\nspeed = 2\n[foo]\n[a#x #b] ;c\n", 2.0, "af_heart",
     ["ignoring unknown section [foo]", "ignoring unknown section [a#x #b]"]),
])
def test_the_split_reads_lines_as_configparser_does(tmp_path, body, speed,
                                                     voice, warnings):
    path = write(tmp_path / "c.conf", body)
    cfg, got = config.load(path)
    assert (cfg.speed, cfg.voice) == (speed, voice)
    assert got == [f"{path}: {warning}" for warning in warnings]


def test_an_indented_pronunciations_header_is_still_a_header(tmp_path):
    # nothing written before [pronunciations] existed meant it as a value
    body = "[readaloud]\nvoice = af_sky\n  [pronunciations]\n  id = ID\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.pronunciations) == ("af_sky", (("id", "ID"),))
    assert warnings == []


def test_a_repeated_unknown_section_is_reported_once(tmp_path):
    path = write(tmp_path / "c.conf", "[readaloud]\nspeed = 1.5\n"
                 "[colors]\na = 1\n[colors]\nb = 2\n")
    cfg, warnings = config.load(path)
    assert cfg.speed == 1.5
    assert warnings == [f"{path}: ignoring unknown section [colors]"]


def test_pronunciations_survive_a_broken_readaloud_section(tmp_path):
    body = ("[readaloud]\nspeed 1.5\n\n[pronunciations]\nid = ID\n"
            "just words\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == DEFAULTS.speed
    assert cfg.pronunciations == (("id", "ID"),)
    assert len(warnings) == 2
    assert "could not be parsed" in warnings[0]
    assert "[pronunciations] line 6:" in warnings[1]


def test_a_bug_in_the_section_costs_only_the_pronunciations(tmp_path,
                                                            monkeypatch):
    def broken(lines):
        raise RuntimeError("boom")

    monkeypatch.setattr(config, "_parse_pronunciations", broken)
    path = write(tmp_path / "c.conf", "[readaloud]\nspeed = 1.5\n")
    cfg, warnings = config.load(path)
    assert (cfg.speed, cfg.pronunciations) == (1.5, ())
    assert warnings == [
        f"{path}: [pronunciations] could not be used (RuntimeError('boom'))"]


def test_split_keeps_configparser_line_numbers():
    text = ("speed = 1\n[pronunciations]\nid = ID\n[readaloud]  # c\n"
            "voice = x\n[nope]\nfoo\n[ Pronunciations ] ; x\n[1] = x\n")
    ini, lines, warnings = config._split(text)
    assert ini.split("\n") == ["speed = 1", "", "", "[readaloud]  # c",
                               "voice = x", "", "", "", "", ""]
    assert lines == [(3, "id = ID"), (9, "[1] = x"), (10, "")]
    assert warnings == ["ignoring unknown section [nope]"]


def test_load_never_raises_on_garbage_in_the_section(tmp_path):
    """Random lines under [pronunciations]: whatever they are, the settings
    above them stay, and every pair that comes out is clean."""
    rng = random.Random(20260911)
    alphabet = (list(" \t=#;\"'[]()/\\:.,aZ9_-\u00e9\u0301\u2713\ufffd")
                + list("\x00\x07\x0c\r\u2028") + [" = "] * 4
                + ["speed", "id", "[readaloud]", "//", "]("])
    ends_the_section = re.compile(r"\[[^\[\]]+\]\s*(?:[#;].*)?")
    lines: list[str] = []
    while len(lines) < 400:
        line = "".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
        if not ends_the_section.fullmatch(line.strip()):
            lines.append(line)
    for start in range(0, len(lines), 20):
        body = ("[readaloud]\nspeed = 1.5\n[pronunciations]\n"
                + "\n".join(lines[start:start + 20]) + "\n")
        cfg, warnings = config.load(write(tmp_path / "c.conf", body))
        assert cfg.speed == 1.5, body
        assert all(isinstance(w, str) for w in warnings)
        assert len(cfg.pronunciations) == len(dict(cfg.pronunciations))
        for written, spoken in cfg.pronunciations:
            for side in (written, spoken):
                assert side and side == " ".join(side.split())
                assert side == unicodedata.normalize("NFC", side)
                assert not any(unicodedata.category(ch) == "Cc" for ch in side)
            assert any(ch.isalnum() for ch in spoken)


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #


def test_template_mentions_every_key_with_its_default():
    text = config.template()
    assert f"[{config.SECTION}]" in text
    for field in ("voice", "speed", "lang", "repo", "sentences", "chars",
                  "prefetch", "device", "color", "follow_lead",
                  "follow_margin", "media_keys"):
        assert re.search(rf"(?m)^#{field} =", text), field
        assert re.search(rf"(?m)^# default:.*\n#{field} =", text), field


def test_template_says_media_keys_needs_the_optional_extra():
    """A user who turns it on and sees nothing happen must be able to find out
    why from the file itself: the feature is inert without PyObjC."""
    text = config.template()
    block = text[text.index("#media_keys ") - 600:text.index("#media_keys ")]
    assert "mediakeys" in block            # the extra's name
    assert "play/pause" in block


def test_template_has_a_comment_above_every_key():
    lines = config.template().splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^#\w+ =", line):
            assert lines[i - 1].startswith("# default:")
            assert lines[i - 2].startswith("# ")


def test_template_round_trips_to_the_defaults(tmp_path):
    path = write(tmp_path / "c.conf", config.template())
    cfg, warnings = config.load(path)
    assert cfg == DEFAULTS
    assert warnings == []


def test_uncommented_template_round_trips_to_the_defaults(tmp_path):
    live = re.sub(r"(?m)^#(\w+ =.*)$", r"\1", config.template())
    assert "\nvoice = af_heart" in live  # the un-commenting really happened
    cfg, warnings = config.load(write(tmp_path / "c.conf", live))
    assert cfg == DEFAULTS
    assert warnings == []


TEMPLATE_EXAMPLES = (
    ("id", "ID"),
    ("kubectl", "cube control"),
    ("GIF", "jif"),
    ("New York City", "NYC"),
    ("#include", "hash include"),
)


def test_template_ends_with_a_live_pronunciations_section():
    text = config.template()
    head, _, block = text.partition(f"\n[{config.PRONUNCIATIONS}]\n")
    assert block, "no live [pronunciations] header"
    assert f"[{config.SECTION}]" in head
    assert all(line.startswith("# ") or line == "#"
               for line in block.splitlines())


def test_template_lines_fit_in_79_columns():
    assert max(len(line) for line in config.template().splitlines()) <= 79


def test_uncommented_pronunciation_examples_are_exactly_the_pairs(tmp_path):
    """Only the examples hold " = ", so taking the "# " off every line that
    does must use the examples and leave every help line a comment."""
    head, header, block = config.template().partition(
        f"\n[{config.PRONUNCIATIONS}]\n")
    live = re.sub(r"(?m)^# (\S.* = .*)$", r"\1", block)
    comments = [line for line in live.splitlines() if line.startswith("#")]
    assert len(comments) == len(block.splitlines()) - len(TEMPLATE_EXAMPLES)
    path = write(tmp_path / "c.conf", head + header + live)
    cfg, warnings = config.load(path)
    assert (cfg.pronunciations, warnings) == (TEMPLATE_EXAMPLES, [])
    assert cfg == Config(pronunciations=TEMPLATE_EXAMPLES)


def test_everything_uncommented_in_the_template(tmp_path):
    live = re.sub(r"(?m)^#(\w+ =.*)$", r"\1", config.template())
    live = re.sub(r"(?m)^# (\S.* = .*)$", r"\1", live)
    cfg, warnings = config.load(write(tmp_path / "c.conf", live))
    assert warnings == []
    assert cfg == Config(pronunciations=TEMPLATE_EXAMPLES)


def test_write_template_returns_the_path_and_writes_the_text(tmp_path):
    path = tmp_path / "written.conf"
    assert config.write_template(path) == path
    assert path.read_text(encoding="utf-8") == config.template()


def test_write_template_with_no_argument_uses_default_path(tmp_path):
    assert config.write_template() == tmp_path / "sandbox.conf"
    assert (tmp_path / "sandbox.conf").exists()


# --------------------------------------------------------------------------- #
# ensure
# --------------------------------------------------------------------------- #


def test_ensure_creates_the_template(tmp_path):
    path = tmp_path / "c.conf"
    assert config.ensure(path) is True
    assert path.read_text(encoding="utf-8") == config.template()
    assert config.load(path) == (DEFAULTS, [])


def test_ensure_is_idempotent_and_does_not_clobber(tmp_path):
    path = write(tmp_path / "c.conf", "[readaloud]\nspeed = 2.5\n")
    assert config.ensure(path) is False
    assert config.ensure(path) is False
    assert path.read_text(encoding="utf-8") == "[readaloud]\nspeed = 2.5\n"
    assert config.load(path)[0].speed == 2.5


def test_ensure_on_a_second_call_after_creating(tmp_path):
    path = tmp_path / "c.conf"
    assert config.ensure(path) is True
    assert config.ensure(path) is False


def test_ensure_with_no_argument_uses_default_path(tmp_path):
    assert config.ensure() is True
    assert (tmp_path / "sandbox.conf").exists()
    assert config.ensure() is False


def test_ensure_never_raises_on_an_unwritable_home(tmp_path):
    home = tmp_path / "ro"
    home.mkdir()
    home.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read + execute, no write
    try:
        assert config.ensure(home / ".readaloud.conf") is False
    finally:
        home.chmod(stat.S_IRWXU)


def test_ensure_never_raises_on_a_missing_parent(tmp_path):
    assert config.ensure(tmp_path / "no" / "such" / "dir" / "c.conf") is False


def test_ensure_never_raises_on_a_directory(tmp_path):
    d = tmp_path / "c.conf"
    d.mkdir()
    assert config.ensure(d) is False
    assert d.is_dir()


def test_parse_error_warning_is_one_short_printable_line(tmp_path):
    path = tmp_path / "c.conf"
    path.write_bytes(bytes(range(256)) * 8)
    _, warnings = config.load(path)
    assert len(warnings) == 1
    message = warnings[0]
    assert "\n" not in message
    assert message.isprintable()
    # the path itself may be long; the parser's complaint must not be
    assert len(message) < len(str(path)) + 220
