"""``~/.readaloud.conf`` -- user defaults for the settings that are otherwise
only command-line flags.

Precedence is strictly::

    explicit CLI flag  >  ~/.readaloud.conf  >  built-in default

`cli` implements that by giving argparse ``default=None`` sentinels and filling
the holes from `load` afterwards.  This module deliberately knows nothing about
argparse: baking config values in as argparse defaults would make "did the user
pass this flag?" unanswerable.

The file is INI (stdlib `configparser`), and it is meant to be hand-edited.
The settings live in a ``[readaloud]`` section: `template` emits a comment
above every key explaining it and showing its default, with every key
commented out, so an untouched file leaves every setting at its default.

A ``[pronunciations]`` section holds ``text = how to say it`` lines.  Unlike
the settings, it is not empty to begin with: `template` writes the entries in
`TEMPLATE_PRONUNCIATIONS`, the words Kokoro is measurably wrong about, as
ordinary lines the reader can edit or delete.  Those lines are what
configparser cannot read: it lowercases keys, splits "std::vector" at the
colon, takes "[1] = x" for a section header, and one line without a delimiter
makes it reject the whole file.  So `load` lifts that section out before
configparser sees the rest, and parses its lines itself.  This module only
parses them; where a pronunciation applies is `readaloud.pronounce`'s business.

Nothing here raises.  A missing file means plain defaults and no warning; an
unreadable, binary, duplicated or malformed file means the defaults for
whatever could not be understood, plus warnings the caller may print.  One bad
key or pronunciation line never discards the rest of the file.  A reader that
refuses to start because its preferences file is odd would be worse than one
that ignores it.
"""

from __future__ import annotations

import configparser
import difflib
import io
import re
import unicodedata
from dataclasses import dataclass, fields as _dataclass_fields
from pathlib import Path

__all__ = [
    "Config",
    "DEFAULT_PATH",
    "PRONUNCIATIONS",
    "TEMPLATE_PRONUNCIATIONS",
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

#: The section of words and phrases readaloud should say differently.
PRONUNCIATIONS = "pronunciations"


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
    media_keys: bool = True        # claim the system play/pause button
    # (written, spoken) pairs from [pronunciations], in file order
    pronunciations: tuple[tuple[str, str], ...] = ()


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
    _Field("media_keys", "bool", (
        "Take over the system play/pause button -- the one on a Bluetooth",
        "headset, on the keyboard, and in Control Center -- so pressing it",
        "pauses readaloud instead of launching Apple Music.  Needs the",
        "optional PyObjC extra (install with `uv tool install --editable",
        "'.[mediakeys]'`); without it this setting does nothing at all.",
        "false is the equivalent of --no-media-keys.",
    )),
)

_BY_NAME = {f.name: f for f in _FIELDS}

# The dataclass and the metadata table are edited by hand; keep them honest.
# pronunciations is the one field that is not a [readaloud] key.
assert _BY_NAME.keys() == ({f.name for f in _dataclass_fields(Config)}
                           - {"pronunciations"})


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
    never discards the rest of the file, and this never raises.  The same goes
    for ``[pronunciations]``: a line that makes no sense is a warning and is
    skipped, and the settings never depend on that section being right.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    cfg = Config()
    try:
        text, warnings = _read_text(target)
        if text is None:
            return cfg, warnings

        try:
            ini, lines, notes = _split(text)
            pairs, problems = _parse_pronunciations(lines)
        except Exception as exc:  # noqa: BLE001 - lose these, keep the settings
            ini, pairs, problems = text, (), []
            notes = [f"[{PRONUNCIATIONS}] could not be used ({exc!r})"]
        # Set before configparser gets a say: a [readaloud] section it rejects
        # must not cost the pronunciations as well.
        cfg.pronunciations = pairs
        warnings += [f"{target}: {note}" for note in notes]

        parser, parse_warnings = _parse(ini, target)
        warnings += parse_warnings
        if parser is not None:
            _read_settings(parser, cfg, target, warnings)
        warnings += [f"{target}: {problem}" for problem in problems]
        return cfg, warnings
    except Exception as exc:  # noqa: BLE001 - the whole point: never raise
        return Config(), [f"{target}: could not be used ({exc!r}); "
                          f"using defaults"]


def _read_settings(parser: configparser.ConfigParser, cfg: Config,
                   path: Path, warnings: list[str]) -> None:
    """Set the ``[readaloud]`` keys in `parser` on `cfg`, warning as it goes."""
    for name in parser.sections():
        if name != SECTION:
            warnings.append(f"{path}: ignoring unknown section [{name}]")

    if not parser.has_section(SECTION):
        return

    for key, raw in parser.items(SECTION):
        field = _BY_NAME.get(key.strip().lower())
        if field is None:
            # A key nothing like a setting is most likely a pronunciation in
            # a file written before [pronunciations] existed.
            like = difflib.get_close_matches(key.strip().lower(), _BY_NAME,
                                             n=1, cutoff=0.6)
            hint = ("" if like else
                    f"; a pronunciation goes under [{PRONUNCIATIONS}]")
            warnings.append(f"{path}: unknown key {key!r} (ignored{hint})")
            continue
        value = _coerce(field, raw if raw is not None else "",
                        path, warnings)
        if value is not None:
            setattr(cfg, field.name, value)


# --------------------------------------------------------------------------- #
# [pronunciations] -- lifted out of the file before configparser reads it
# --------------------------------------------------------------------------- #


#: configparser's own idea of a section header (its SECTCRE): "[", anything,
#: and the last "]", matched at the start of the line once the inline comment
#: and the whitespace round it are gone.  Whatever follows that "]" is ignored.
_SECTION_RE = re.compile(r"\[(?P<name>.+)\]")
#: configparser's own idea of an option line, with its default delimiters.
_OPTION_RE = configparser.ConfigParser.OPTCRE
#: Inside [pronunciations] only a line that is nothing but a header ends the
#: section, so "[x] = checkbox" and "[1] = footnote one" stay entries.
_SECTION_END_RE = re.compile(r"^\[(?P<name>[^\[\]]+)\]\s*(?:[#;].*)?$")

#: A pronunciation's separator: the first "=" with whitespace on both sides,
#: so "a == b = x" says "a == b" as "x".  Without one, the first "=" at all.
_SEPARATOR_RE = re.compile(r"(?<=\s)=(?=\s)")
#: An inline comment after the "=": a # or ; after whitespace, so "C#" is not.
_VALUE_COMMENT_RE = re.compile(r"(?<=\s)[#;]")
#: A setting written with configparser's other delimiter, "speed: 1.5".
_SETTING_COLON_RE = re.compile(r"([\w-]+)\s*:")
#: Markdown link syntax, which misaki reads as "say this text with these
#: phonemes" and so would swallow.
_LINK_RE = re.compile(r"\[[^\]]+\]\([^)]*\)")

#: Problems past this many are counted rather than listed: prose pasted under
#: [pronunciations] is a problem on every line, and a screenful of those would
#: bury every other warning.
_MAX_PROBLEMS = 5


def _split(text: str) -> tuple[str, list[tuple[int, str]], list[str]]:
    """Lift the ``[pronunciations]`` lines out of `text`.

    Returns ``(ini, lines, warnings)``: the text configparser gets, the
    pronunciation lines as ``(line number, raw line)``, and warnings without
    the path in front.  Every line taken out of `ini` is left there empty, so
    configparser's line numbers stay right.

    Unknown sections are taken out too, with a warning.  configparser would
    choke on the entries of a misspelled "[pronunciation]" and throw away the
    settings with them; this way only the misspelled section is lost, and the
    warning can say which name was probably meant.  Several
    ``[pronunciations]`` sections are read as one, in file order.
    """
    kept: list[str] = []
    lines: list[tuple[int, str]] = []
    warnings: list[str] = []
    warned: set[str] = set()
    # "ini" is [readaloud] and whatever precedes the first header, where bare
    # keys are allowed; "other" is an unknown section.
    state = "ini"                  # "ini" | PRONUNCIATIONS | "other"
    # configparser's continuation rule, followed on every line it reads or
    # would have read: a line indented deeper than the option line above it
    # is more of that value, even when it looks like a header.  Only a
    # [pronunciations] header, which no file from before it could mean as a
    # value, is a header however it is indented.
    indent, option = 0, False
    for n, line in enumerate(text.split("\n"), 1):
        header = None
        if state == PRONUNCIATIONS:
            header = _SECTION_END_RE.match(line.strip())
        elif value := line[:_comment_start(line)].strip():
            depth = len(line) - len(line.lstrip())
            header = _SECTION_RE.match(value)
            if (option and depth > indent and not (
                    header and _names_pronunciations(header))):
                header = None
            else:
                indent = depth
                if not header and (named := _OPTION_RE.match(value)):
                    option = bool(named.group("option").rstrip())
        if header:
            lifted, option = state != "ini", False
            name = header.group("name")
            if _names_pronunciations(header):
                state = PRONUNCIATIONS
            elif name in (SECTION, configparser.DEFAULTSECT):
                # [DEFAULT] stays configparser's: its keys fill [readaloud]
                state = "ini"
                if lifted:
                    # configparser carries a value across the empty lines
                    # left where the section was, and would take an indented
                    # header after them for more of it
                    line = line.lstrip()
            else:
                state = "other"
                if name not in warned:      # configparser merged repeats
                    warned.add(name)
                    warnings.append(_unknown_section(name))
        elif state == PRONUNCIATIONS:
            lines.append((n, line))
        kept.append(line if state == "ini" else "")
    return "\n".join(kept), lines, warnings


def _comment_start(line: str) -> int:
    """Where configparser's comment starts in `line`, or ``len(line)``.

    A line whose text starts with # or ; is all comment.  Otherwise it looks
    at the first # and the first ;, then the second of each, and so on, and
    stops at the first round where one of them starts the line or follows
    whitespace.
    """
    if line.lstrip().startswith(("#", ";")):
        return 0
    seen = {"#": -1, ";": -1}
    while seen:
        starts, found = [], {}
        for prefix, index in seen.items():
            index = line.find(prefix, index + 1)
            if index < 0:
                continue
            found[prefix] = index
            if index == 0 or line[index - 1].isspace():
                starts.append(index)
        if starts:
            return min(starts)
        seen = found
    return len(line)


def _names_pronunciations(header: re.Match[str]) -> bool:
    return header.group("name").strip().lower() == PRONUNCIATIONS


def _unknown_section(name: str) -> str:
    hint = difflib.get_close_matches(name.strip().lower(),
                                     [SECTION, PRONUNCIATIONS],
                                     n=1, cutoff=0.6)
    guess = f" (did you mean [{hint[0]}]?)" if hint else ""
    return f"ignoring unknown section [{name}]{guess}"


def _parse_pronunciations(
    lines: list[tuple[int, str]],
) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """Return ``(pairs, problems)`` for the ``(line number, raw line)`` list.

    The same text twice (compared with its case, so "id", "Id" and "ID" are
    three entries) is a problem, and the later line wins: it takes its own
    place in the file order, as if the earlier line had never been written.
    """
    entries: dict[str, tuple[int, str]] = {}
    problems: list[str] = []
    for n, raw in lines:
        parsed = _parse_pronunciation(n, raw)
        if isinstance(parsed, str):
            problems.append(parsed)
        elif parsed is not None:
            key, value = parsed
            if key in entries:
                problems.append(
                    f"[{PRONUNCIATIONS}] line {n}: {_shown(key)} is also on "
                    f"line {entries[key][0]}; line {n} wins")
                del entries[key]
            entries[key] = (n, value)
    if len(problems) > _MAX_PROBLEMS:
        hidden = len(problems) - _MAX_PROBLEMS
        more = "1 more problem" if hidden == 1 else f"{hidden} more problems"
        problems[_MAX_PROBLEMS:] = [f"[{PRONUNCIATIONS}]: {more} not shown"]
    pairs = tuple((key, value) for key, (_, value) in entries.items())
    return pairs, problems


def _parse_pronunciation(n: int, raw: str) -> tuple[str, str] | str | None:
    """One line: a ``(written, spoken)`` pair, a problem, or None for nothing.

    ``text = how to say it``.  The separator is the first "=" with whitespace
    on both sides, so neither side needs quotes for an "=" of its own
    (``== = equals equals``); failing that, the first "=" (``id=ID``).  A side
    may still be quoted, with no escapes: the text when it starts with # or ;
    or a quote, or holds " = " itself; the pronunciation when it holds a # or ;
    after a space.  Whitespace runs collapse to one space and both sides are
    NFC; case is kept, since it decides what the text matches.
    """
    at = f"[{PRONUNCIATIONS}] line {n}: "
    line = raw.strip()
    if not line or line[0] in "#;":
        return None

    quoted = _quoted(line)
    key_quoted = quoted is not None and quoted[1].lstrip().startswith("=")
    if key_quoted:
        key, rest = quoted[0], quoted[1].lstrip()[1:]
    else:
        separator = _SEPARATOR_RE.search(line)
        eq = separator.start() if separator else line.find("=")
        if eq < 0:
            setting = _SETTING_COLON_RE.match(line)
            if setting and setting.group(1).lower() in _BY_NAME:
                # "speed: 1.5" appended below the template's last header
                return (f"{at}{_shown(setting.group(1))} is a setting: move "
                        f"it under [{SECTION}]")
            return (f'{at}{_shown(line)} has no "="; '
                    f"write it as: text = how to say it")
        key, rest = line[:eq], line[eq + 1:]

    quoted = _quoted(rest.lstrip())
    after = quoted[1].lstrip() if quoted is not None else ""
    value_quoted = quoted is not None and (not after or after[0] in "#;")
    if value_quoted:
        value = quoted[0]
    else:
        # Searched before stripping, so "id = # a note" has an empty value.
        comment = _VALUE_COMMENT_RE.search(rest)
        value = rest[:comment.start()] if comment else rest

    key, value = _normalise(key), _normalise(value)
    if not key:
        return (f'{at}nothing before the "="; put quotes round text '
                f'that has an "=" in it')
    for side, text in (("the text", key), ("the pronunciation", value)):
        for ch in text:
            if ch == "\ufffd":
                return f"{at}{side} {_shown(text)} is not valid UTF-8"
            if unicodedata.category(ch) == "Cc":
                return (f"{at}{side} {_shown(text)} has a control character "
                        f"(U+{ord(ch):04X})")
    if not value:
        return f'{at}{_shown(key)} has nothing after the "="'
    if not value_quoted and value.startswith("="):
        # "x == y" has no "=" with spaces round it, so it split at the first
        return (f'{at}{_shown(key)} = {_shown(value)}: put quotes round text '
                f'that has an "=" in it')
    if not value_quoted and len(value) >= 2 and value[0] == value[-1] == "/":
        return (f"{at}{_shown(key)}: phonemes (/.../) are not supported; "
                f"write it the way it sounds")
    if _LINK_RE.search(value):
        # Quoted or not: misaki would take it for phonemes either way.
        return (f"{at}{_shown(key)}: [text](...) is not supported; "
                f"write it the way it sounds")
    if not any(ch.isalnum() for ch in value):
        return (f"{at}{_shown(key)}: {_shown(value)} has nothing to say "
                f"(no letters or digits)")
    if not key_quoted and key.lower() in _BY_NAME:
        # The template ends in [pronunciations], so a setting appended at the
        # bottom of the file lands here.
        return (f"{at}{_shown(key)} is a setting: move it under [{SECTION}], "
                f"or put quotes round the word to pronounce it")
    return key, value


def _quoted(text: str) -> tuple[str, str] | None:
    """``(inside, after)`` when `text` opens with a quote that closes again."""
    if text[:1] in _QUOTES:
        close = text.find(text[0], 1)
        if close > 0:
            return text[1:close], text[close + 1:]
    return None


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _shown(text: str, limit: int = 40) -> str:
    """`text` quoted for a warning, cut short so one line stays one line."""
    return repr(text if len(text) <= limit else text[:limit] + "...")


# --------------------------------------------------------------------------- #
# template
# --------------------------------------------------------------------------- #


_PREAMBLE = f"""\
# ~/.readaloud.conf -- defaults for readaloud.
#
# Every setting under [{SECTION}] is commented out and shows its built-in
# default, so this file as generated changes nothing.  Uncomment a line and
# edit it to make that setting stick.  A command-line flag always wins over
# this file, and this file always wins over the built-in default.
#
# [{PRONUNCIATIONS}] at the end tells readaloud how to say particular words.
# It comes with entries for the ones Kokoro gets wrong; delete any you do not
# want.
#
# Delete this file and readaloud will write a fresh copy on its next run.

[{SECTION}]
"""

# Live header, commented body.  No help line may hold " = " with spaces round
# it: only the examples do, so uncommenting them is all it takes to use them.
_PRONUNCIATIONS_HELP = f"""\
[{PRONUNCIATIONS}]
# How to say a word or phrase: the text as written on the left of the "=",
# and how to say it on the right, spelled the way it sounds.  Capitals are
# usually read as letters, so ID says "eye dee".
#
# A word matches on its own and as part of a name: id matches the id, foo.id,
# user_id and userId, but not idle or grid.  Lowercase text matches any case;
# text with a capital matches that case only.  A space in a phrase matches any
# spacing, one line break too.  Put quotes round text that starts with # or ;
# or a quote, or that has an "=" with spaces round it.
#
# Try one before you rely on it: `readaloud 'user.id'` reads just that with
# your pronunciations, and `readaloud --no-config 'user.id'` reads it without.
#
# These come with readaloud, and they are only lines in a file: delete one to
# stop readaloud saying it that way, or edit it to say it your way.
"""

#: What the generated file says out of the box, in the groups it keeps.  Each
#: one is text Kokoro reads wrongly without it, measured with misaki: it says
#: "id" as Freud's id, spells JSON out letter by letter, swallows the dot of a
#: file name, and runs "systemd" into a single cluster.
_TEMPLATE_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("names, and the quotes that stand for nothing", (
        ("id", "ID"),
        ("ids", "IDs"),
        ("''", "empty string"),
        ('""', "empty string"),
        ("'''", "triple quote"),
        ('"""', "triple quote"),
    )),
    ("file types.  The three names come first: with the bare .md entry alone,\n"
     "# README.md would be spelled out letter by letter", (
        ("README.md", "readme dot M D"),
        ("CHANGELOG.md", "changelog dot M D"),
        ("TODO.md", "todo dot M D"),
        (".md", "dot M D"),
        (".txt", "dot text"),
        (".rst", "dot R S T"),
        (".json", "dot jason"),
        (".yaml", "dot yaml"),
        (".yml", "dot yaml"),
        (".toml", "dot toml"),
        (".ini", "dot I N I"),
        (".cfg", "dot config"),
        (".conf", "dot conf"),
        (".env", "dot env"),
        (".csv", "dot C S V"),
        (".tsv", "dot T S V"),
        (".xml", "dot X M L"),
        (".html", "dot H T M L"),
        (".css", "dot C S S"),
        (".scss", "dot S C S S"),
        (".js", "dot J S"),
        (".jsx", "dot J S X"),
        (".ts", "dot T S"),
        (".tsx", "dot T S X"),
        (".py", "dot pie"),
        (".rb", "dot R B"),
        (".go", "dot go"),
        (".rs", "dot R S"),
        (".java", "dot java"),
        (".kt", "dot K T"),
        (".swift", "dot swift"),
        (".php", "dot P H P"),
        (".lua", "dot lua"),
        (".cpp", "dot C P P"),
        (".hpp", "dot H P P"),
        (".sh", "dot S H"),
        (".bash", "dot bash"),
        (".zsh", "dot Z S H"),
        (".sql", "dot S Q L"),
        (".sqlite", "dot sequel lite"),
        (".db", "dot D B"),
        (".log", "dot log"),
        (".lock", "dot lock"),
        (".pem", "dot pem"),
        (".crt", "dot C R T"),
        (".zip", "dot zip"),
        (".tar", "dot tar"),
        (".gz", "dot G Z"),
        (".png", "dot P N G"),
        (".jpg", "dot J peg"),
        (".jpeg", "dot J peg"),
        (".gif", "dot gif"),
        (".svg", "dot S V G"),
        (".webp", "dot web P"),
        (".pdf", "dot P D F"),
        (".mp3", "dot M P three"),
        (".mp4", "dot M P four"),
        (".wav", "dot wave"),
        (".exe", "dot E X E"),
        (".dll", "dot D L L"),
        (".dylib", "dot dylib"),
        (".app", "dot app"),
    )),
    ("languages, tools and formats", (
        ("C#", "C sharp"),
        ("F#", "F sharp"),
        (".NET", "dot net"),
        ("json", "jason"),
        ("YAML", "yammle"),
        ("TOML", "tommle"),
        ("cli", "C L I"),
        ("aws", "A. W. S."),
        ("redis", "red iss"),
        ("postgresql", "Postgres Q L"),
        ("sqlite", "sequel lite"),
        ("systemd", "system dee"),
        ("journalctl", "journal control"),
        ("kubectl", "cube control"),
        ("k8s", "kubernetes"),
        ("PyPI", "pie pea eye"),
        ("enum", "ee num"),
    )),
    ("what the punctuation of code is called", (
        ("!=", "not equals"),
        ("-->", "arrow"),
        ("->", "arrow"),
        ("=>", "fat arrow"),
        ("::", "colon colon"),
        ("&&", "and and"),
        ("||", "or or"),
    )),
)

#: Every shipped pair, flat and in the file's order.
TEMPLATE_PRONUNCIATIONS: tuple[tuple[str, str], ...] = tuple(
    pair for _title, pairs in _TEMPLATE_GROUPS for pair in pairs)

#: Left commented under the shipped ones, to show what else an entry can do.
_TEMPLATE_EXAMPLES: tuple[tuple[str, str], ...] = (
    ("GIF", "jif"),
    ("New York City", "NYC"),
    ("#include", "hash include"),
)


def _pronunciation_line(text: str, say: str) -> str:
    """One ``text = how to say it`` line, the text quoted where it must be."""
    if text[:1] in _QUOTES or text[:1] in "#;" or " = " in text:
        quote = "'" if '"' in text else '"'
        text = f"{quote}{text}{quote}"
    return f"{text} = {say}"


def template() -> str:
    """The fully commented INI text: every setting present and disabled, then
    the ``[pronunciations]`` section with its shipped entries and examples."""
    out = [_PREAMBLE]
    for field in _FIELDS:
        block = [f"# {line}" for line in field.help]
        block.append(f"# default: {_format_default(field) or '(empty)'}")
        block.append(f"#{field.name} = {_format_default(field)}".rstrip())
        out.append("\n".join(block) + "\n")
    out.append(_pronunciations_block())
    return "\n".join(out)


def _pronunciations_block() -> str:
    """The ``[pronunciations]`` section: help, the shipped lines, examples."""
    out = [_PRONUNCIATIONS_HELP]
    for title, pairs in _TEMPLATE_GROUPS:
        out.append(f"# {title}\n" + "\n".join(
            _pronunciation_line(text, say) for text, say in pairs) + "\n")
    out.append('# More to try.  To use one, delete the "# " in front of it.\n'
               + "\n".join("# " + _pronunciation_line(text, say)
                            for text, say in _TEMPLATE_EXAMPLES) + "\n")
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
