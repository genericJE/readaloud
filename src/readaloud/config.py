"""``~/.readaloud.conf`` -- user defaults for the settings that are otherwise
only command-line flags.

Precedence is strictly::

    explicit CLI flag  >  ~/.readaloud.conf  >  built-in default

`cli` implements that by giving argparse ``default=None`` sentinels and filling
the holes from `load` afterwards.  This module deliberately knows nothing about
argparse: baking config values in as argparse defaults would make "did the user
pass this flag?" unanswerable.

The file is INI (stdlib `configparser`) with a single ``[readaloud]`` section,
and it is meant to be hand-edited: `template` emits a comment above every key
explaining it and showing its default, with every key commented out, so an
untouched file means "all defaults".

Nothing here raises.  A missing file means plain defaults and no warning; an
unreadable, binary, duplicated or malformed file means the defaults for
whatever could not be understood, plus warnings the caller may print.  One bad
key never discards the rest of the file.  A reader that refuses to start
because its preferences file is odd would be worse than one that ignores it.
"""

from __future__ import annotations

import configparser
import io
import re
from dataclasses import dataclass, fields as _dataclass_fields
from pathlib import Path

__all__ = [
    "Config",
    "DEFAULT_PATH",
    "SECTION",
    "ensure",
    "load",
    "template",
    "write_template",
]

#: Where the preferences live.  Resolved at call time (``path or DEFAULT_PATH``)
#: so tests can point the module elsewhere.
DEFAULT_PATH = Path.home() / ".readaloud.conf"

SECTION = "readaloud"


@dataclass
class Config:
    """Every setting the config file can carry, at its built-in default."""

    voice: str = "af_heart"
    speed: float = 1.0
    lang: str = ""                 # "" = derive from the voice prefix
    repo: str = "mlx-community/Kokoro-82M-4bit"
    sentences: int = 4
    chars: int = 380
    prefetch: int = 2
    device: str = ""               # "" = system default output device
    color: bool = True
    follow_lead: int = 20          # extra rows to scroll ahead in follow mode
    follow_margin: int = 2


# --------------------------------------------------------------------------- #
# field metadata -- drives both parsing and the generated template
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Field:
    name: str
    kind: str                      # "str" | "int" | "float" | "bool"
    help: tuple[str, ...]          # comment lines, without the leading "# "
    lo: float | None = None
    hi: float | None = None


_FIELDS: tuple[_Field, ...] = (
    _Field("voice", "str", (
        "Kokoro voice name.  `readaloud --list-voices` prints the ones your",
        "model repo has; the first letter picks the language (a=American",
        "English, b=British, e=Spanish, f=French, i=Italian, j=Japanese, ...).",
    )),
    _Field("speed", "float", (
        "Speech rate multiplier.  Clamped to 0.5 - 3.0.",
    ), lo=0.5, hi=3.0),
    _Field("lang", "str", (
        "Kokoro language code.  Leave empty to derive it from the voice name,",
        "which is almost always what you want.",
    )),
    _Field("repo", "str", (
        "HuggingFace model repo to load the voice from.",
    )),
    _Field("sentences", "int", (
        "Maximum sentences per spoken chunk.  Smaller chunks start sooner and",
        "seek more finely; larger ones give the model more context.  Minimum 1.",
    ), lo=1),
    _Field("chars", "int", (
        "Maximum characters per spoken chunk, whichever limit is hit first.",
        "Minimum 1.",
    ), lo=1),
    _Field("prefetch", "int", (
        "Chunks to synthesize ahead of the one being spoken.  0 disables",
        "prefetching (less memory, audible gaps between chunks).",
    ), lo=0),
    _Field("device", "str", (
        "Audio output device: an index, a substring of the device name, or",
        "'default'.  Leave empty for the system default output device.",
        "`readaloud --list-devices` prints the choices.",
    )),
    _Field("color", "bool", (
        "Render the input's colours.  false is the equivalent of --no-color.",
        "Accepts true/false, yes/no, on/off, 1/0.",
    )),
    _Field("follow_lead", "int", (
        "Extra rows to scroll ahead in follow mode.  When the spoken word",
        "reaches the bottom margin the view jumps this many rows further than",
        "strictly needed, so the text about to be read sits around the middle",
        "of the screen instead of pinned to the last row.  0 restores the old",
        "creep-by-one-row behaviour.",
    ), lo=0),
    _Field("follow_margin", "int", (
        "Rows kept between the spoken word and the top/bottom edge of the",
        "viewport before follow mode scrolls.",
    ), lo=0),
)

_BY_NAME = {f.name: f for f in _FIELDS}

# The dataclass and the metadata table are edited by hand; keep them honest.
assert _BY_NAME.keys() == {f.name for f in _dataclass_fields(Config)}


def _default_of(name: str) -> object:
    return getattr(Config(), name)


def _format_default(field: _Field) -> str:
    value = _default_of(field.name)
    if field.kind == "bool":
        return "true" if value else "false"
    return str(value)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


_HEADER_RE = re.compile(r"^\s*\[", re.MULTILINE)
_QUOTES = ("'", '"')


def _unquote(raw: str) -> str:
    """Strip whitespace and one layer of matching quotes: ``voice = "af_heart"``."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        return value[1:-1]
    return value


def _read_text(path: Path) -> tuple[str | None, list[str]]:
    """Return (text, warnings).  ``None`` text means "use plain defaults"."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, []                       # not a problem, not a warning
    except OSError as exc:
        # a directory in the way, no read permission, a dead symlink, ...
        return None, [f"{path}: could not be read ({exc.strerror or exc}); "
                      f"using defaults"]
    # Binary garbage must not raise: decode leniently and let configparser
    # complain about the shape rather than the bytes.
    return data.decode("utf-8", errors="replace"), []


def _parse(text: str, path: Path) -> tuple[configparser.ConfigParser | None,
                                           list[str]]:
    parser = configparser.ConfigParser(
        # People hand-write this file: tolerate duplicates (last one wins),
        # trailing "# ..." notes, and stray % signs in values.
        strict=False,
        inline_comment_prefixes=("#", ";"),
        interpolation=None,
    )
    attempts = [text]
    if not _HEADER_RE.search(text):
        # No section header anywhere -- someone wrote bare "speed = 1.2" lines.
        attempts = [f"[{SECTION}]\n{text}"]
    else:
        # There *is* a header, but keys may precede it; retry with one bolted on.
        attempts.append(f"[{SECTION}]\n{text}")
    last: Exception | None = None
    for attempt in attempts:
        try:
            parser.read_file(io.StringIO(attempt), source=str(path))
        except (configparser.Error, ValueError, UnicodeError) as exc:
            last = exc
            parser = configparser.ConfigParser(
                strict=False,
                inline_comment_prefixes=("#", ";"),
                interpolation=None,
            )
            continue
        return parser, []
    detail = _one_line(str(last)) if last else "unparseable"
    return None, [f"{path}: could not be parsed ({detail}); using defaults"]


def _one_line(text: str, limit: int = 120) -> str:
    """Squash a multi-line parser complaint into one printable, short line.

    A binary file's error carries the offending bytes; unabridged, that is a
    screenful of escapes on the terminal the user is about to read in.
    """
    flat = " ".join(text.split())
    printable = "".join(c if c.isprintable() else "?" for c in flat)
    return printable if len(printable) <= limit else printable[:limit - 3] + "..."


def _coerce(field: _Field, raw: str, path: Path,
            warnings: list[str]) -> object | None:
    """Return the value for `field`, or ``None`` to keep the default."""
    default = _default_of(field.name)
    value = _unquote(raw)

    if field.kind == "str":
        return value

    if field.kind == "bool":
        try:
            return _BOOLEANS[value.strip().lower()]
        except KeyError:
            warnings.append(
                f"{path}: {field.name}: {raw.strip()!r} is not a boolean "
                f"(try true/false); using {'true' if default else 'false'}")
            return None

    try:
        number: float | int = (int(value, 10) if field.kind == "int"
                               else float(value))
    except ValueError:
        kind = "a whole number" if field.kind == "int" else "a number"
        warnings.append(f"{path}: {field.name}: {raw.strip()!r} is not {kind}; "
                        f"using {default}")
        return None

    if field.lo is not None and number < field.lo:
        clamped = int(field.lo) if field.kind == "int" else field.lo
        warnings.append(f"{path}: {field.name}: {number} is below the minimum "
                        f"{clamped}; clamped to {clamped}")
        return clamped
    if field.hi is not None and number > field.hi:
        clamped = int(field.hi) if field.kind == "int" else field.hi
        warnings.append(f"{path}: {field.name}: {number} is above the maximum "
                        f"{clamped}; clamped to {clamped}")
        return clamped
    return number


_BOOLEANS = {
    # configparser.ConfigParser.BOOLEAN_STATES, spelled out so a bare
    # `getboolean` failure mode cannot surprise us.
    "1": True, "yes": True, "true": True, "on": True,
    "0": False, "no": False, "false": False, "off": False,
}


def load(path: Path | None = None) -> tuple[Config, list[str]]:
    """Parse the file.  Returns ``(config, warnings)``.

    A missing file is NOT a warning -- it yields plain defaults.  A malformed
    value IS a warning and falls back to the default for that key; one bad key
    never discards the rest of the file, and this never raises.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    cfg = Config()
    try:
        text, warnings = _read_text(target)
        if text is None:
            return cfg, warnings

        parser, parse_warnings = _parse(text, target)
        warnings += parse_warnings
        if parser is None:
            return cfg, warnings

        for name in parser.sections():
            if name != SECTION:
                warnings.append(f"{target}: ignoring unknown section [{name}]")

        if not parser.has_section(SECTION):
            return cfg, warnings

        for key, raw in parser.items(SECTION):
            field = _BY_NAME.get(key.strip().lower())
            if field is None:
                warnings.append(f"{target}: unknown key {key!r} (ignored)")
                continue
            value = _coerce(field, raw if raw is not None else "",
                            target, warnings)
            if value is not None:
                setattr(cfg, field.name, value)
        return cfg, warnings
    except Exception as exc:  # noqa: BLE001 - the whole point: never raise
        return Config(), [f"{target}: could not be used ({exc!r}); "
                          f"using defaults"]


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #


_PREAMBLE = f"""\
# ~/.readaloud.conf -- defaults for readaloud.
#
# Every key below is commented out and shows its built-in default, so this file
# as generated changes nothing.  Uncomment a line and edit it to make that
# setting stick.  A command-line flag always wins over this file, and this file
# always wins over the built-in default.
#
# Delete this file and readaloud will write a fresh copy on its next run.

[{SECTION}]
"""


def template() -> str:
    """The fully commented INI text: every key present, every key disabled."""
    out = [_PREAMBLE]
    for field in _FIELDS:
        block = [f"# {line}" for line in field.help]
        block.append(f"# default: {_format_default(field) or '(empty)'}")
        block.append(f"#{field.name} = {_format_default(field)}".rstrip())
        out.append("\n".join(block) + "\n")
    return "\n".join(out)


def write_template(path: Path | None = None) -> Path:
    """Write `template` to `path` (overwriting it) and return the path."""
    target = Path(path) if path is not None else DEFAULT_PATH
    target.write_text(template(), encoding="utf-8")
    return target


def ensure(path: Path | None = None) -> bool:
    """Create the template if absent.  True if it created one.

    Never raises -- an unwritable HOME must not stop the reader from starting.
    """
    try:
        target = Path(path) if path is not None else DEFAULT_PATH
        if target.exists():
            return False
        # "x" rather than write_template's truncate: two readaloud processes
        # starting at once must not leave a half-written file, and an existing
        # file is never clobbered even if it appears between the two calls.
        with open(target, "x", encoding="utf-8") as fh:
            fh.write(template())
        return True
    except Exception:  # noqa: BLE001 - unwritable HOME, directory in the way, ...
        return False
