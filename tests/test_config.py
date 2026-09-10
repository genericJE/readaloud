"""Tests for `readaloud.config` -- the ``~/.readaloud.conf`` preferences file.

Two properties matter more than any individual key:

1. `load` never raises.  It is called before the TUI starts, on a file the user
   hand-edits, so a stray line, a binary blob, a directory where the file
   should be or a file nobody may read must all degrade to "defaults plus a
   warning" rather than to a traceback in front of the reader.
2. The generated template round-trips.  Parsing `template` -- as written, and
   with every key uncommented -- must yield exactly `Config()`, otherwise the
   documented defaults and the real defaults have drifted apart.

Every test writes to `tmp_path`; the autouse fixture below also re-points
`config.DEFAULT_PATH` there, so even a test that forgets to pass a path cannot
touch the real ``~/.readaloud.conf``.
"""

from __future__ import annotations

import os
import re
import stat
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
