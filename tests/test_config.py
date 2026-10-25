"""Tests for `readaloud.config` -- the ``~/.readaloud.conf`` preferences file.

Two properties matter more than any individual key:

1. `load` never raises.  It is called before the TUI starts, on a file the user
   hand-edits, so a stray line, a binary blob, a directory where the file
   should be or a file nobody may read must all degrade to "defaults plus a
   warning" rather than to a traceback in front of the reader.
2. The generated template round-trips.  Parsing `template` -- as written, and
   with every key uncommented -- must yield `Config()` for every setting, plus
   exactly the pronunciations it ships (`config.TEMPLATE_PRONUNCIATIONS`),
   otherwise the documented defaults and the real defaults have drifted.

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
import tempfile
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
    monkeypatch.setattr(config, "LEGACY_PATH", tmp_path / "sandbox.legacy.conf")
    yield


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# defaults
# --------------------------------------------------------------------------- #


def test_default_path_is_dot_readaloud_conf_in_home():
    assert REAL_DEFAULT_PATH == Path.home() / ".readaloud"


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
    write(tmp_path / "sandbox.conf", "[config]\nspeed = 1.5\n")
    cfg, warnings = config.load()
    assert (cfg.speed, warnings) == (1.5, [])


def test_empty_file_yields_defaults(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf", ""))
    assert cfg == DEFAULTS
    assert warnings == []


def test_comments_only_file_yields_defaults(tmp_path):
    body = "# just a note\n; and another\n\n[config]\n# nothing set\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg == DEFAULTS
    assert warnings == []


def test_config_dataclass_defaults():
    assert DEFAULTS == Config(
        voice="af_heart", speed=1.0, lang="", chars=380,
        repo="mlx-community/Kokoro-82M-4bit", sentences=4, prefetch=2,
        device="", color=True, follow_lead=20, follow_margin=5,
        media_keys=True, format="markdown", highlight=True, language="",
        parsers="",
    )


# --------------------------------------------------------------------------- #
# every key
# --------------------------------------------------------------------------- #


ALL_KEYS = """\
[config]
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
format = code
highlight = no
language = python
parsers = ~/.local/share/nvim/lazy/nvim-treesitter/parser
"""


def test_every_key_is_read(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf", ALL_KEYS))
    assert warnings == []
    assert cfg == Config(
        voice="bf_emma", speed=1.25, lang="b", repo="hexgrad/Kokoro-82M",
        sentences=7, chars=500, prefetch=0, device="MacBook Pro Speakers",
        color=False, follow_lead=12, follow_margin=5, media_keys=False,
        format="code", highlight=False, language="python",
        parsers="~/.local/share/nvim/lazy/nvim-treesitter/parser",
    )


@pytest.mark.parametrize("key", ["color", "media_keys", "highlight"])
@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("yes", True), ("on", True), ("1", True),
    ("false", False), ("FALSE", False), ("no", False), ("off", False),
    ("0", False),
])
def test_boolean_spellings(tmp_path, key, raw, expected):
    body = f"[config]\n{key} = {raw}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (getattr(cfg, key), warnings) == (expected, [])


def test_keys_are_case_insensitive_and_values_are_stripped(tmp_path):
    body = "[config]\nVOICE =   am_adam   \nSpeed = 2\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed, warnings) == ("am_adam", 2.0, [])


def test_quoted_values_and_inline_comments(tmp_path):
    body = '[config]\nvoice = "af_sarah"   # the calm one\nspeed = 1.5 ; faster\n'
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed, warnings) == ("af_sarah", 1.5, [])


def test_empty_string_keys_stay_empty(tmp_path):
    body = "[config]\nlang =\ndevice =\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.lang, cfg.device, warnings) == ("", "", [])


def test_follow_lead_zero_is_kept_not_treated_as_unset(tmp_path):
    body = "[config]\nfollow_lead = 0\nfollow_margin = 0\nprefetch = 0\n"
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
    body = f"[config]\n{key} = {given}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert getattr(cfg, key) == expected
    assert len(warnings) == 1
    assert key in warnings[0]


@pytest.mark.parametrize("value", ["0.5", "1.0", "3.0"])
def test_speed_edges_are_in_range(tmp_path, value):
    body = f"[config]\nspeed = {value}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, warnings) == (float(value), [])


def test_clamping_one_key_leaves_the_others_alone(tmp_path):
    body = "[config]\nspeed = 99\nvoice = bf_emma\nfollow_lead = 30\n"
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
    body = f"[config]\n{key} = {given}\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert getattr(cfg, key) == getattr(DEFAULTS, key)
    assert len(warnings) == 1
    assert key in warnings[0]


def test_one_bad_key_never_discards_the_rest_of_the_file(tmp_path):
    body = ("[config]\nvoice = am_adam\nspeed = quick\n"
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
    body = "[config]\nvolume = 11\nspeed = 1.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 1.5
    assert len(warnings) == 1 and "volume" in warnings[0]
    assert not hasattr(cfg, "volume")


def test_a_pronunciation_under_config_is_pointed_at_its_section(tmp_path):
    # a file from before [pronunciations] existed ends inside [config]
    path = write(tmp_path / "c.conf",
                 "[config]\nspeed = 1.5\nid = ID\ncolour = true\n")
    cfg, warnings = config.load(path)
    assert (cfg.speed, cfg.pronunciations) == (1.5, ())
    assert warnings == [
        f"{path}: unknown key 'id' (ignored; a pronunciation goes under "
        f"[pronunciations])",
        f"{path}: unknown key 'colour' (ignored)"]      # close to a setting


def test_unknown_section_is_ignored_with_a_warning(tmp_path):
    body = "[config]\nspeed = 2\n\n[colors]\nhighlight = red\n"
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
    body = "voice = am_adam\n\n[config]\nspeed = 1.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed) == ("am_adam", 1.5)
    assert warnings == []


def test_duplicate_keys_do_not_raise_and_the_last_wins(tmp_path):
    body = "[config]\nspeed = 1.5\nspeed = 2.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 2.5
    assert warnings == []


def test_duplicate_sections_do_not_raise(tmp_path):
    body = "[config]\nspeed = 1.5\n[config]\nvoice = bf_emma\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.voice) == (1.5, "bf_emma")


def test_percent_signs_in_values_do_not_raise(tmp_path):
    # configparser's default interpolation would choke on a bare '%'.
    body = "[config]\ndevice = 100%% Output\nvoice = af_heart\n"
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


def test_an_empty_directory_is_a_preferences_directory_with_nothing_in_it(tmp_path):
    d = tmp_path / "c.conf"
    d.mkdir()
    cfg, warnings = config.load(d)
    assert cfg == DEFAULTS
    assert warnings == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_file(tmp_path):
    path = write(tmp_path / "c.conf", "[config]\nspeed = 2\n")
    path.chmod(0o000)
    try:
        cfg, warnings = config.load(path)
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert cfg == DEFAULTS
    assert len(warnings) == 1 and str(path) in warnings[0]


def test_very_long_and_weird_lines_do_not_raise(tmp_path):
    body = "[config]\n" + "x" * 10000 + " = " + "y" * 10000 + "\n\n\n\t\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg == DEFAULTS
    assert warnings


def test_load_accepts_a_string_path(tmp_path):
    path = write(tmp_path / "c.conf", "[config]\nspeed = 1.5\n")
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
    path.write_bytes(b"[config]\r\nspeed = 1.5\r\n\r\n[pronunciations]\r\n"
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
     "'speed' is a setting: move it under [config], or put quotes round "
     "the word to pronounce it"),
    ("Follow_Lead = 3",
     "'Follow_Lead' is a setting: move it under [config], or put quotes "
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
            "[config]\n"
            "speed = 1.5\n"
            "\n"
            "[pronunciations]\n"
            "id = ID\n"
            "not a pronunciation\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("id", "ID"),))
    assert len(warnings) == 1 and "[pronunciations] line 7:" in warnings[0]


def test_problems_come_after_the_settings_warnings(tmp_path):
    body = "[pronunciations]\nnot a pronunciation\n[config]\nspeed = fast\n"
    path = write(tmp_path / "c.conf", body)
    _, warnings = config.load(path)
    assert len(warnings) == 2
    assert warnings[0].startswith(f"{path}: speed:")
    assert warnings[1].startswith(f"{path}: [pronunciations] line 2:")


# --------------------------------------------------------------------------- #
# pronunciations: next to the settings
# --------------------------------------------------------------------------- #


def test_a_bad_pronunciation_line_keeps_the_settings(tmp_path):
    body = ("[config]\nspeed = 1.5\n\n[pronunciations]\nid = ID\n"
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


def test_a_config_header_with_a_comment_ends_the_section(tmp_path):
    body = "[pronunciations]\nid = ID\n[config]  # c\nspeed = 1.5\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("id", "ID"),))
    assert warnings == []


@pytest.mark.parametrize("header", [
    "[Pronunciations]", "[ pronunciations ]", "[pronunciations] ; words",
])
def test_the_section_name_ignores_case_and_spaces(tmp_path, header):
    body = f"[config]\nspeed = 1.5\n{header}\nid = ID\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("id", "ID"),))
    assert warnings == []


def test_several_pronunciations_sections_are_read_as_one(tmp_path):
    body = ("[pronunciations]\nid = ID\n[config]\nspeed = 2\n"
            "[pronunciations]\nkubectl = cube control\nid = eye dee\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert cfg.speed == 2.0
    assert cfg.pronunciations == (("kubectl", "cube control"),
                                  ("id", "eye dee"))
    assert len(warnings) == 1 and "also on line 2; line 7 wins" in warnings[0]


def test_an_entry_in_brackets_then_a_bad_line_keeps_the_settings(tmp_path):
    body = ("[config]\nspeed = 1.5\n[pronunciations]\n[1] = footnote one\n"
            "no equals here\n")
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.speed, cfg.pronunciations) == (1.5, (("[1]", "footnote one"),))
    assert len(warnings) == 1 and "line 5" in warnings[0]


def test_a_misspelled_section_keeps_the_settings_and_says_so(tmp_path):
    path = write(tmp_path / "c.conf",
                 "[config]\nspeed = 1.5\n\n[pronunciation]\nid = ID\n"
                 "no equals here\n")
    cfg, warnings = config.load(path)
    assert (cfg.speed, cfg.pronunciations) == (1.5, ())
    assert warnings == [
        f"{path}: ignoring unknown section [pronunciation] "
        f"(did you mean [pronunciations]?)"]


def test_a_misspelled_config_section_says_so(tmp_path):
    path = write(tmp_path / "c.conf", "[confg]\nspeed = 1.5\n")
    cfg, warnings = config.load(path)
    assert cfg == DEFAULTS
    assert warnings == [
        f"{path}: ignoring unknown section [confg] "
        f"(did you mean [config]?)"]


def test_the_old_readaloud_header_is_pointed_to_config(tmp_path):
    path = write(tmp_path / "c.conf", "[readaloud]\nspeed = 1.5\n")
    cfg, warnings = config.load(path)
    assert cfg == DEFAULTS
    assert warnings == [
        f"{path}: ignoring unknown section [readaloud] "
        f"(the settings section is now [config])"]


@pytest.mark.parametrize("body", [
    "[DEFAULT]\nspeed = 1.5\n[config]\nvoice = bf_emma\n",
    "[config]\nvoice = bf_emma\n[pronunciations]\nid = ID\n"
    "[DEFAULT]\nspeed = 1.5\n",
])
def test_a_default_section_still_fills_the_settings(tmp_path, body):
    # configparser's [DEFAULT] was never an unknown section: its keys are
    # every section's, [config]'s included.
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
    assert cfg == SHIPPED
    assert len(warnings) == 1
    assert "is a setting: move it under [config]" in warnings[0]


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
    body = ("[config]\nvoice = af_bella\n\n" + section
            + "\n    [config]\n    speed = 1.5\n")
    cfg, _warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.speed) == ("af_bella", 1.5)


@pytest.mark.parametrize("body,speed,voice,warnings", [
    # an indented header under a key is more of its value, as configparser
    # has always read it, even right after a lifted section
    ("[config]\nspeed = 1.5\nvoice = af_sky\n  [colors]\n  [config]\n"
     "  more\n", 1.5, "af_sky\n[colors]\n[config]\nmore", []),
    ("[config]\nspeed = 1.5\n  [colors]\nvoice = af_sky\n", 1.0, "af_sky",
     ["speed: '1.5\\n[colors]' is not a number; using 1.0"]),
    ("[colors]\nx = 1\n  [config]\nspeed = 1.5\n", 1.0, "af_heart",
     ["ignoring unknown section [colors]"]),
    # a comment is found the way configparser finds it, # and ; in turns
    ("[config]\nspeed = 2\n[x#] #] ;z\n", 2.0, "af_heart",
     ["ignoring unknown section [x#] #]"]),
    ("[config]\nspeed = 2\n[foo]\n[a#x #b] ;c\n", 2.0, "af_heart",
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
    body = "[config]\nvoice = af_sky\n  [pronunciations]\n  id = ID\n"
    cfg, warnings = config.load(write(tmp_path / "c.conf", body))
    assert (cfg.voice, cfg.pronunciations) == ("af_sky", (("id", "ID"),))
    assert warnings == []


def test_a_repeated_unknown_section_is_reported_once(tmp_path):
    path = write(tmp_path / "c.conf", "[config]\nspeed = 1.5\n"
                 "[colors]\na = 1\n[colors]\nb = 2\n")
    cfg, warnings = config.load(path)
    assert cfg.speed == 1.5
    assert warnings == [f"{path}: ignoring unknown section [colors]"]


def test_pronunciations_survive_a_broken_config_section(tmp_path):
    body = ("[config]\nspeed 1.5\n\n[pronunciations]\nid = ID\n"
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
    path = write(tmp_path / "c.conf", "[config]\nspeed = 1.5\n")
    cfg, warnings = config.load(path)
    assert (cfg.speed, cfg.pronunciations) == (1.5, ())
    assert warnings == [
        f"{path}: [pronunciations] could not be used (RuntimeError('boom'))"]


def test_split_keeps_configparser_line_numbers():
    text = ("speed = 1\n[pronunciations]\nid = ID\n[config]  # c\n"
            "voice = x\n[nope]\nfoo\n[ Pronunciations ] ; x\n[1] = x\n")
    ini, lines, warnings = config._split(text)
    assert ini.split("\n") == ["speed = 1", "", "", "[config]  # c",
                               "voice = x", "", "", "", "", ""]
    assert lines == [(3, "id = ID"), (9, "[1] = x"), (10, "")]
    assert warnings == ["ignoring unknown section [nope]"]


def test_load_never_raises_on_garbage_in_the_section(tmp_path):
    """Random lines under [pronunciations]: whatever they are, the settings
    above them stay, and every pair that comes out is clean."""
    rng = random.Random(20260911)
    alphabet = (list(" \t=#;\"'[]()/\\:.,aZ9_-\u00e9\u0301\u2713\ufffd")
                + list("\x00\x07\x0c\r\u2028") + [" = "] * 4
                + ["speed", "id", "[config]", "//", "]("])
    ends_the_section = re.compile(r"\[[^\[\]]+\]\s*(?:[#;].*)?")
    lines: list[str] = []
    while len(lines) < 400:
        line = "".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
        if not ends_the_section.fullmatch(line.strip()):
            lines.append(line)
    for start in range(0, len(lines), 20):
        body = ("[config]\nspeed = 1.5\n[pronunciations]\n"
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
# layers: [files], and the sections headed with a layer's name
# --------------------------------------------------------------------------- #


LAYERED = """\
[config]
speed = 1.2
voice = bf_emma

[files]
.py .pyi = code python
.rs = code rust
.md = markdown
.xyz = rust

[config.code]
speed = 1.5
format = code

[config.python]
speed = 2

[parts]
num = \\d+

[parts.code]
word = [a-z]+

[shapes]
/(?<n>%{num})/ = general $n

[shapes.python]
item: /(?<n>%{num})/ = item $n
/\\[(?<l>[^\\]]+)\\]/ = list ${l|item|,=and}
/(?<w>%{word}):(?<n>%{num})/ = $w at $n

[pronunciations]
id = I D
api = general

[pronunciations.code]
== = is equal to
| = pipe
api = A P I

[pronunciations.python]
| = or
"""


def test_the_layers_are_read_from_their_headers(tmp_path):
    path = write(tmp_path / "c.conf", LAYERED)
    cfg, warnings = config.load(path)
    assert warnings == []
    assert cfg.files == ((".py", ("code", "python")), (".pyi", ("code", "python")),
                         (".rs", ("code", "rust")), (".md", ("markdown",)),
                         (".xyz", ("rust",)))
    assert [t.name for t in cfg.types] == ["code", "python"]
    code, py = cfg.types
    assert code == config.FileType(
        "code", settings=(("speed", 1.5), ("format", "code")),
        pronunciations=(("==", "is equal to"), ("|", "pipe"), ("api", "A P I")),
        parts=(("word", "[a-z]+"),))
    assert py.settings == (("speed", 2.0),)
    assert py.named_shapes == (("item", r"/(?<n>%{num})/", "item $n"),)
    assert py.shapes == ((r"/\[(?<l>[^\]]+)\]/", "list ${l|item|,=and}"),
                         (r"/(?<w>%{word}):(?<n>%{num})/", "$w at $n"))
    # the general config is untouched
    assert (cfg.speed, cfg.format) == (1.2, "markdown")
    assert cfg.pronunciations == (("id", "I D"), ("api", "general"))
    assert cfg.shapes == ((r"/(?<n>%{num})/", "general $n"),)


def test_layers_for_and_grammar_name_follow_the_files_map(tmp_path):
    cfg, _ = config.load(write(tmp_path / "c.conf", LAYERED))
    assert cfg.layers_for(".py") == cfg.layers_for(".PY") == ("code", "python")
    assert cfg.layers_for(".xyz") == ("rust",) and cfg.layers_for(".txt") == ()
    assert cfg.grammar_name(".py") == "python" and cfg.grammar_name(".xyz") == "rust"
    assert cfg.grammar_name(".txt") is None
    assert config.Config(language="Tree-Sitter").grammar_name(".py") == "tree_sitter"


def test_the_layers_lie_over_the_general_config_in_files_order(tmp_path):
    cfg, _ = config.load(write(tmp_path / "c.conf", LAYERED))
    py = cfg.for_suffix(".PY")
    assert (py.speed, py.voice, py.format, py.types) == (2.0, "bf_emma", "code", ())
    # a later layer's shapes are tried first; its pronunciations and parts
    # follow the earlier ones, so the later wins a tie
    assert py.shapes == cfg.types[1].shapes + cfg.shapes
    assert py.pronunciations == (("id", "I D"), ("api", "general"), ("==", "is equal to"),
                                 ("|", "pipe"), ("api", "A P I"), ("|", "or"))
    assert py.parts == (("num", r"\d+"), ("word", "[a-z]+"))
    # a python shape may use a code part, which was headed before it
    assert py.shape_patterns[1] == (r"(?<w>(?:[a-z]+)):(?<n>(?:\d+))", "$w at $n")
    assert py.sub_patterns == (("item", r"(?<n>(?:\d+))", "item $n"),)
    rs = cfg.for_suffix(".rs")                      # code, and a rust layer nobody wrote
    assert (rs.speed, rs.format) == (1.5, "code")
    assert rs.pronunciations == (("id", "I D"), ("api", "general"), ("==", "is equal to"),
                                 ("|", "pipe"), ("api", "A P I"))
    # a layer with no sections, or a suffix [files] does not name: as it is
    assert cfg.for_suffix(".md") is cfg and cfg.for_suffix(".txt") is cfg
    assert cfg.for_layers(["python"]).speed == 2.0  # --conf python: that layer alone
    assert cfg.for_layers(["python"]).format == "markdown"


@pytest.mark.parametrize("path", [None, "-", "notes.txt", "Makefile"])
def test_a_file_the_files_map_does_not_name_gets_the_general_config(tmp_path, path):
    cfg, _ = config.load(write(tmp_path / "c.conf", LAYERED))
    assert cfg.for_file(path) == cfg or cfg.for_file(path) is cfg


def test_for_file_goes_by_the_suffix(tmp_path):
    cfg, _ = config.load(write(tmp_path / "c.conf", LAYERED))
    assert cfg.for_file("src/a.py").speed == 2.0
    assert cfg.for_file("src/A.PY").speed == 2.0


def test_the_shipped_files_map_is_the_default(tmp_path):
    cfg, _ = config.load(write(tmp_path / "c.conf", "[config]\nspeed = 1.2\n"))
    assert cfg.files == config.TEMPLATE_FILES == config.Config().files
    assert cfg.layers_for(".py") == ("code", "python")
    assert cfg.layers_for(".md") == ("markdown",) and cfg.layers_for(".txt") == ("plain",)
    assert cfg.grammar_name(".rs") == "rust"


def test_each_files_problem_says_what_is_wrong(tmp_path):
    for line, problem in [
        ("py = python", "'py' is not an extension: write it with its dot, as .py"),
        (".py = Py Thon!", "'thon!' is not a layer's name: letters, digits and _, "
                           "as code or python"),
        ("nothing", "'nothing' has no \"=\"; write it as: .ext = layer layer"),
    ]:
        path = write(tmp_path / "c.conf", f"[files]\n{line}\n")
        cfg, warnings = config.load(path)
        assert cfg.files == config.TEMPLATE_FILES, line
        assert warnings == [f"{path}: [files] line 2: {problem}"]
    path = write(tmp_path / "c.conf", "[files]\n.py = a\n.rs .py = b\n")
    cfg, warnings = config.load(path)
    assert cfg.files == ((".rs", ("b",)), (".py", ("b",)))
    assert warnings == [f"{path}: [files] line 3: .py is also on line 2; line 3 wins"]


def folder(tmp_path, **files: str) -> Path:
    """A preferences directory holding `files` (``readaloud_conf="..."``)."""
    d = tmp_path / "prefs"
    d.mkdir(exist_ok=True)
    for name, body in files.items():
        (d / name.replace("_conf", ".conf")).write_text(body, encoding="utf-8")
    return d


def test_a_directory_holds_the_main_file_and_a_file_per_layer(tmp_path):
    d = folder(
        tmp_path,
        readaloud_conf="[config]\nspeed = 1.2\n[files]\n.py = code python\n.rs = code\n"
                       "[parts]\nident = [a-z]+\n[pronunciations]\nid = I D\n"
                       "[config.code]\nformat = code\n",
        code_conf="[parts]\nop = [=<>]+\n[pronunciations]\n== = is equal to\n| = pipe\n",
        python_conf="[config]\nspeed = 2\n[shapes]\n/(?<o>%{op})(?<i>%{ident})/ = $o $i\n"
                    "[syntax]\nfunction_definition = function $name\n"
                    "[pronunciations]\n| = or\n",
    )
    cfg, warnings = config.load(d)
    assert warnings == []
    assert (cfg.speed, cfg.format, cfg.pronunciations) == (1.2, "markdown", (("id", "I D"),))
    # the code layer comes from a header in the main file and from code.conf
    assert [t.name for t in cfg.types] == ["code", "code", "python"]
    py = cfg.for_file("app.py")
    assert (py.speed, py.format) == (2.0, "code")
    assert py.pronunciations == (("id", "I D"), ("==", "is equal to"), ("|", "pipe"),
                                 ("|", "or"))
    # a language file's shapes may use the general and the code parts
    assert py.shape_patterns == (("(?<o>(?:[=<>]+))(?<i>(?:[a-z]+))", "$o $i"),)
    assert py.syntax == (("function_definition", "function $name"),)
    rs = cfg.for_file("lib.rs")
    assert (rs.speed, rs.format, rs.syntax) == (1.2, "code", ())
    assert rs.pronunciations[-1] == ("|", "pipe")
    assert cfg.for_file("notes.md").pronunciations == (("id", "I D"),)


def test_a_directory_without_the_main_file_and_odd_files(tmp_path):
    d = folder(tmp_path, python_conf="[config]\nspeed = 2\n[files]\n.py = python\n")
    (d / "Rust Lang.conf").write_text("[config]\nspeed = 3\n", encoding="utf-8")
    (d / "notes.txt").write_text("not a config\n", encoding="utf-8")
    cfg, warnings = config.load(d)
    assert cfg.speed == 1.0 and cfg.for_file("a.py").speed == 2.0
    assert sorted(warnings) == sorted([
        f"{d / 'Rust Lang.conf'}: ignored; a layer's file is named after its "
        f"layer, as python.conf",
        f"{d / 'python.conf'}: [files] belongs in readaloud.conf, not in "
        f"python.conf; ignored"])


def test_a_problem_in_a_language_file_names_that_file(tmp_path):
    d = folder(tmp_path, readaloud_conf="[config]\nspeed = 1.2\n",
               python_conf="[config]\nspeed = fast\n[shapes]\n/x*/ = ex\n")
    cfg, warnings = config.load(d)
    assert cfg.for_file("a.py").speed == 1.2
    assert warnings == [
        f"{d / 'python.conf'}: speed: 'fast' is not a number; using 1.0",
        f"{d / 'python.conf'}: [shapes] line 4: the shape /x*/ cannot be used: "
        f"it can match empty text"]


LEGACY = """\
[config]
# settings
speed = 1.3

[parts]
ext = py|rs

# source files: read as written
[config.code]
markdown = no

[syntax.py]  # the def rules
function_definition = function $name $body

[pronunciations.code]
== = is equal to
[1] = one
[pronunciations.PY]
str = string
[pronunciations.xyz]
odd = words
[pronunciations]
id = I D
"""


def test_split_legacy_sends_a_suffixs_sections_to_its_language_file():
    files = config.split_legacy(LEGACY)
    assert list(files) == ["readaloud.conf", "python.conf"]
    # a layer's sections ([x.code]) and an unknown suffix stay in the main
    # file; the comment above a header moves with it; markdown = no is
    # format = code; "[1] = one" is an entry, not a header
    assert files["readaloud.conf"] == (
        "[config]\n# settings\nspeed = 1.3\n\n[parts]\next = py|rs\n\n"
        "# source files: read as written\n[config.code]\nformat = code\n\n"
        "[pronunciations.code]\n== = is equal to\n[1] = one\n"
        "[pronunciations.xyz]\nodd = words\n[pronunciations]\nid = I D\n")
    assert files["python.conf"] == (
        "[syntax]  # the def rules\nfunction_definition = function $name $body\n"
        "[pronunciations]\nstr = string\n")
    assert config.split_legacy("") == {}
    assert config.split_legacy("speed = 2\n") == {"readaloud.conf": "speed = 2\n"}


def test_format_takes_one_of_three_words(tmp_path):
    for raw, want in [("code", "code"), ("Plain", "plain"), (" markdown ", "markdown")]:
        cfg, warnings = config.load(write(tmp_path / "c.conf", f"[config]\nformat = {raw}\n"))
        assert (cfg.format, warnings) == (want, [])
    path = write(tmp_path / "c.conf", "[config]\nformat = html\nmarkdown = no\n")
    cfg, warnings = config.load(path)
    assert cfg.format == "markdown"
    assert warnings == [
        f"{path}: format: 'html' is not one of markdown, code, plain; using markdown",
        f"{path}: unknown key 'markdown' (ignored; it is format = markdown | code | plain now)"]


def test_renderer_is_none_or_mdcat(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf", "[config]\nrenderer = MDCAT\n"))
    assert (cfg.renderer, warnings) == ("mdcat", [])
    path = write(tmp_path / "c.conf", "[config]\nrenderer = pandoc\n")
    cfg, warnings = config.load(path)
    assert cfg.renderer == "none"
    assert warnings == [f"{path}: renderer: 'pandoc' is not one of none, mdcat; using none"]


def test_split_legacy_rewrites_the_markdown_setting():
    files = config.split_legacy("[config]\nmarkdown = yes\nspeed = 2\n[config.py]\n"
                                "markdown = no  # as written\n")
    assert files["readaloud.conf"] == "[config]\nspeed = 2\n"
    assert files["python.conf"] == "[config]\nformat = code  # as written\n"


def test_migrate_splits_the_old_file_into_the_directory_and_deletes_it(tmp_path):
    old = write(tmp_path / ".readaloud.conf", LEGACY)
    folder = tmp_path / ".readaloud"
    notes = config.migrate(old, folder)
    assert notes == [f"moved {old} into {folder}/: readaloud.conf, python.conf (the "
                     f"settings section and your pronunciations are in readaloud.conf; "
                     f"the sections for a file type are in that language's file)"]
    assert not old.exists()
    assert sorted(p.name for p in folder.iterdir()) == ["python.conf", "readaloud.conf"]
    cfg, warnings = config.load(folder)
    assert warnings == []
    assert (cfg.speed, cfg.parts) == (1.3, (("ext", "py|rs"),))
    py = cfg.for_file("a.py")
    assert py.format == "code" and ("str", "string") in py.pronunciations
    assert py.syntax == (("function_definition", "function $name $body"),)
    # nothing to do a second time, or when the directory is already there
    assert config.migrate(old, folder) == []
    write(old, "[config]\nspeed = 2\n")
    assert config.migrate(old, folder) == [] and old.exists()


def test_migrate_never_raises(tmp_path):
    old = write(tmp_path / "old.conf", "[config]\nspeed = 2\n")
    blocked = tmp_path / "ro"
    blocked.mkdir()
    blocked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        notes = config.migrate(old, blocked / ".readaloud")
    finally:
        blocked.chmod(stat.S_IRWXU)
    assert len(notes) == 1 and notes[0].startswith(f"could not move {old}")
    assert old.exists()


def test_layer_headers_ignore_case_and_take_one_name(tmp_path):
    path = write(tmp_path / "c.conf",
                 "[Shapes.Python]\n/x+/ = ex\n[config.]\n[.py]\n[shapes.a.b]\n[files.py]\n")
    cfg, warnings = config.load(path)
    assert cfg.types[0].name == "python" and cfg.types[0].shapes == (("/x+/", "ex"),)
    assert [w.split(": ", 1)[1].split(" (")[0] for w in warnings] == [
        "ignoring unknown section [config.]",
        "ignoring unknown section [.py]",
        "ignoring unknown section [shapes.a.b]",
        "ignoring unknown section [files.py]"]


def test_a_misspelled_layer_header_is_hinted(tmp_path):
    path = write(tmp_path / "c.conf", "[shapess.python]\n/x+/ = ex\n")
    cfg, warnings = config.load(path)
    assert cfg.types == ()
    assert warnings == [f"{path}: ignoring unknown section [shapess.python] "
                        f"(did you mean [shapes.python]?)"]


def test_layer_problems_carry_the_layer_label(tmp_path):
    path = write(tmp_path / "c.conf",
                 "[config.python]\nspeed = fast\nspead = 2\n"
                 "[parts.python]\nbad = (x\n"
                 "[shapes.python]\n/x*/ = ex\n"
                 "[pronunciations.python]\nno equals here\n")
    cfg, warnings = config.load(path)
    assert [w.removeprefix(f"{path}: ") for w in warnings] == [
        "[config.python] speed: 'fast' is not a number; using 1.0",
        "[config.python] unknown key 'spead' (ignored)",
        "[pronunciations.python] line 9: 'no equals here' has no \"=\"; write it "
        "as: text = how to say it",
        "[parts.python] line 5: 'bad' cannot be used: unclosed group",
        "[shapes.python] line 7: the shape /x*/ cannot be used: it can match "
        "empty text",
    ]


def test_a_layer_part_may_share_a_general_name_without_a_warning(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf",
                                      "[parts]\nx = a\n[parts.python]\nx = b\n"
                                      "[shapes.python]\n/%{x}/ = it\n"))
    assert warnings == []
    assert cfg.for_suffix(".py").shape_patterns == (("(?:b)", "it"),)


def test_a_layer_shape_may_use_a_general_named_shape(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf",
                                      "[shapes]\nn: /\\d/ = digit\n"
                                      "[shapes.python]\n/<(?<l>[\\d,]+)>/ = ${l|n}\n"))
    assert warnings == []
    assert cfg.shapes == ()
    assert cfg.for_suffix(".py").shapes == (("/<(?<l>[\\d,]+)>/", "${l|n}"),)


def test_a_repeated_layer_header_is_read_as_one(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf",
                                      "[config.python]\nspeed = 2\n"
                                      "[pronunciations.python]\na = b\n"
                                      "[config.python]\nformat = code\n"
                                      "[pronunciations.python]\nc = d\n"))
    assert warnings == []
    (py,) = cfg.types
    assert py.settings == (("speed", 2.0), ("format", "code"))
    assert py.pronunciations == (("a", "b"), ("c", "d"))


# --------------------------------------------------------------------------- #
# [syntax]: saying a source file's tree
# --------------------------------------------------------------------------- #


SYNTAX = """\
[syntax]
function_definition = function $name $parameters $return_type
function_definition.return_type = and returns type $0
parameters = takes ${*|,=and}

[syntax.python]
typed_default_parameter = $name type $type with a default of $value
type/binary_operator = $left or $right
parameters = takes ${*|,=and|*=star}
"""


def test_syntax_rules_are_read_in_general_and_for_a_type(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf", SYNTAX))
    assert warnings == []
    assert cfg.syntax == (
        ("function_definition", "function $name $parameters $return_type"),
        ("function_definition.return_type", "and returns type $0"),
        ("parameters", "takes ${*|,=and}"))
    (py,) = cfg.types
    assert py.syntax == (
        ("typed_default_parameter", "$name type $type with a default of $value"),
        ("type/binary_operator", "$left or $right"),
        ("parameters", "takes ${*|,=and|*=star}"))
    # the type's rules come after the general ones, so the type's win
    assert cfg.for_suffix(".py").syntax == cfg.syntax + py.syntax
    assert config.Config().syntax == ()


def test_each_syntax_problem_says_what_is_wrong(tmp_path):
    for line, problem in [
        ("nothing", "'nothing' has no \"=\"; write it as: kind = wording"),
        ("a/b/c = x", "'a/b/c' is not a node kind: a rule is kind = wording, "
                      "ancestor/kind = wording, or kind.field = wording"),
        ("def = $1", "def: '$1': a rule names a node's fields, $0 for the node "
                     "as written, or $* for its parts"),
        ("def = ${name|slash}", "def: '${name|slash}': write a replacement as "
                                "|text=word, such as ${*|,=and}"),
        ("def = --", "def: '--' has nothing to say; leave it empty to say the "
                     "node as written"),
    ]:
        cfg, warnings = config.load(write(tmp_path / "c.conf", f"[syntax]\n{line}\n"))
        assert cfg.syntax == (), line
        assert warnings == [f"{tmp_path / 'c.conf'}: [syntax] line 2: {problem}"]


def test_an_empty_syntax_wording_and_a_repeat_are_read(tmp_path):
    path = write(tmp_path / "c.conf", "[syntax.python]\nstring =\ndef = a\ndef = b\n")
    cfg, warnings = config.load(path)
    assert cfg.types[0].syntax == (("string", ""), ("def", "b"))
    assert warnings == [f"{path}: [syntax.python] line 4: 'def' is also on line 3; "
                        f"line 4 wins"]


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #

SHIPPED_DIR = Path(config.__file__).with_name("configs")
SHIPPED_FILE = SHIPPED_DIR / "readaloud.conf"


def shipped() -> Config:
    """What a fresh file says, as `load` reads it."""
    with tempfile.TemporaryDirectory() as folder:
        cfg, warnings = config.load(write(Path(folder) / "c.conf",
                                          config.template()))
    assert warnings == []
    return cfg


#: What a fresh file says out of the box.
SHIPPED = shipped()


def test_template_is_the_shipped_file_as_it_is():
    assert config.template() == SHIPPED_FILE.read_text(encoding="utf-8")


def test_template_loads_without_a_warning(tmp_path):
    cfg, warnings = config.load(write(tmp_path / "c.conf", config.template()))
    assert warnings == []
    assert cfg.pronunciations == config.TEMPLATE_PRONUNCIATIONS
    assert cfg.parts == config.TEMPLATE_PARTS
    assert cfg.shapes == config.TEMPLATE_SHAPES
    assert cfg.pronunciations and cfg.parts and cfg.shapes


def test_template_ends_with_a_live_pronunciations_section():
    """A line appended to a fresh file lands under [pronunciations]."""
    text = config.template()
    head, _, block = text.partition(f"\n[{config.PRONUNCIATIONS}]\n")
    assert block, "no live [pronunciations] header"
    assert f"[{config.SECTION}]" in head
    assert not re.search(r"(?m)^\[", block)


def test_every_shipped_pronunciation_is_usable(tmp_path):
    """Each line on its own: parsed, no warning, and not a settings name (the
    parser refuses those), so a new entry cannot be quietly dropped."""
    block = config.template().partition(f"\n[{config.PRONUNCIATIONS}]\n")[2]
    lines = [line for line in block.splitlines()
             if line.strip() and not line.lstrip().startswith(("#", ";"))]
    assert len(lines) == len(config.TEMPLATE_PRONUNCIATIONS)
    for line, pair in zip(lines, config.TEMPLATE_PRONUNCIATIONS):
        cfg, warnings = config.load(
            write(tmp_path / "c.conf", f"[pronunciations]\n{line}\n"))
        assert (cfg.pronunciations, warnings) == ((pair,), []), line


def test_the_shipped_pronunciations_do_not_repeat_themselves():
    texts = [text for text, _say in config.TEMPLATE_PRONUNCIATIONS]
    assert len(texts) == len(set(texts))


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
    assert config.ensure(path) == [path]
    assert path.read_text(encoding="utf-8") == config.template()
    assert config.load(path) == (SHIPPED, [])


def test_ensure_is_idempotent_and_does_not_clobber(tmp_path):
    path = write(tmp_path / "c.conf", "[config]\nspeed = 2.5\n")
    assert config.ensure(path) == []
    assert config.ensure(path) == []
    assert path.read_text(encoding="utf-8") == "[config]\nspeed = 2.5\n"
    assert config.load(path)[0].speed == 2.5


def test_ensure_on_a_second_call_after_creating(tmp_path):
    path = tmp_path / "c.conf"
    assert config.ensure(path) == [path]
    assert config.ensure(path) == []


def test_ensure_with_no_argument_uses_default_path(tmp_path):
    assert config.ensure() == [tmp_path / "sandbox.conf"]
    assert (tmp_path / "sandbox.conf").exists()
    assert config.ensure() == []


def test_ensure_never_raises_on_an_unwritable_home(tmp_path):
    home = tmp_path / "ro"
    home.mkdir()
    home.chmod(stat.S_IRUSR | stat.S_IXUSR)  # read + execute, no write
    try:
        assert config.ensure(home / ".readaloud.conf") == []
        assert config.ensure(home / ".readaloud") == []
    finally:
        home.chmod(stat.S_IRWXU)


def test_ensure_never_raises_on_a_missing_parent(tmp_path):
    assert config.ensure(tmp_path / "no" / "such" / "dir" / "c.conf") == []


def test_ensure_fills_a_directory_with_the_shipped_files(tmp_path):
    d = tmp_path / ".readaloud"
    written = config.ensure(d)
    assert [p.name for p in written] == list(config.SHIPPED_FILES)
    assert sorted(p.name for p in d.iterdir()) == sorted(config.SHIPPED_FILES)
    for name in config.SHIPPED_FILES:
        assert (d / name).read_text(encoding="utf-8") == config.template(name)
    # a second run writes only what is missing, and clobbers nothing
    (d / "python.conf").unlink()
    (d / "readaloud.conf").write_text("[config]\nspeed = 2\n", encoding="utf-8")
    assert [p.name for p in config.ensure(d)] == ["python.conf"]
    assert config.load(d)[0].speed == 2.0
    assert config.ensure(d) == []


def test_the_shipped_directory_loads_clean_and_lays_python_over_code(tmp_path):
    d = tmp_path / "prefs"
    config.write_template(d)
    cfg, warnings = config.load(d)
    assert warnings == []
    assert [t.name for t in cfg.types] == ["code", "plain", "markdown", "python"]
    py = cfg.for_file("app.py")
    assert py.format == "code" and ("==", "is equal to") in py.pronunciations
    assert ("str", "string") in py.pronunciations and py.syntax
    assert cfg.for_file("lib.rs").format == "code" and not cfg.for_file("lib.rs").syntax
    md = cfg.for_file("notes.md")
    assert (md.format, md.renderer) == ("markdown", "mdcat")
    assert (cfg.for_file("a.txt").format, cfg.renderer) == ("plain", "none")
    assert config.write_template(tmp_path / "one.conf") == tmp_path / "one.conf"
    assert (tmp_path / "one.conf").read_text(encoding="utf-8") == config.template()


def test_ensure_fills_an_existing_directory_whatever_its_name(tmp_path):
    d = tmp_path / "c.conf"
    d.mkdir()
    assert [p.name for p in config.ensure(d)] == list(config.SHIPPED_FILES)
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
