"""``~/.readaloud.conf`` -- user defaults for the settings that are otherwise
only command-line flags.

Precedence is strictly::

    explicit CLI flag  >  ~/.readaloud.conf  >  built-in default

`cli` implements that by giving argparse ``default=None`` sentinels and filling
the holes from `load` afterwards.  This module deliberately knows nothing about
argparse: baking config values in as argparse defaults would make "did the user
pass this flag?" unanswerable.

The file is INI (stdlib `configparser`), and it is meant to be hand-edited.
The settings live in a ``[config]`` section.  A first run, and
``--write-config``, write `template`: the file shipped in the package as
``readaloud/readaloud.conf``, copied as it is.

A ``[pronunciations]`` section holds ``text = how to say it`` lines, which
the shipped file fills with the words Kokoro is measurably wrong about, as
ordinary lines the reader can edit or delete.  Those lines are what
configparser cannot read: it lowercases keys, splits "std::vector" at the
colon, takes "[1] = x" for a section header, and one line without a delimiter
makes it reject the whole file.  So `load` lifts that section out before
configparser sees the rest, and parses its lines itself.  This module only
parses them; where a pronunciation applies is `readaloud.pronounce`'s business.

``[shapes]`` and ``[parts]``, lifted out the same way, describe text to say
a set way.  A shape is ``/pattern/ = wording``, its named groups the parts
the wording names (``/(?<file>%{file}):(?<line>\\d+)/ = $file line $line``);
a part is ``name = pattern``, a piece any pattern below it or any shape can
use as ``%{name}``.  readaloud has no shape and no part of its own: a file
without the sections has none.  `TEMPLATE_PARTS`, `TEMPLATE_SHAPES` and
`TEMPLATE_PRONUNCIATIONS` are what the shipped file holds.

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
from importlib import resources
from pathlib import Path

from .pronounce import (Literal, check_pattern, check_regex, check_shape,
                        is_pattern)

__all__ = [
    "Config",
    "DEFAULT_PATH",
    "PRONUNCIATIONS",
    "PARTS",
    "SHAPES",
    "TEMPLATE_PARTS",
    "TEMPLATE_PRONUNCIATIONS",
    "TEMPLATE_SHAPES",
    "expand_parts",
    "SECTION",
    "ensure",
    "load",
    "template",
    "write_template",
]

#: Where the preferences live.  Resolved at call time (``path or DEFAULT_PATH``)
#: so tests can point the module elsewhere.
DEFAULT_PATH = Path.home() / ".readaloud.conf"

SECTION = "config"

#: The section of words and phrases readaloud should say differently.
PRONUNCIATIONS = "pronunciations"

#: The section of shapes: text described by a pattern (a file and its line, a
#: host and its port) and what it says.
SHAPES = "shapes"

#: The section of named pieces of pattern the shapes are built from.
PARTS = "parts"

#: The sections this module reads itself, rather than configparser.
_LIFTED = (PRONUNCIATIONS, SHAPES, PARTS)

#: A part used in a pattern: %{name}.
_PART_RE = re.compile(r"%\{(\w+)\}")
#: A name a part may have.
_PART_NAME_RE = re.compile(r"[A-Za-z_]\w*")


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
    follow_margin: int = 5
    media_keys: bool = True        # claim the system play/pause button
    # (written, spoken) pairs from [pronunciations], in file order
    pronunciations: tuple[tuple[str, str], ...] = ()
    # (pattern as written, wording) of each [shapes] line, in file order
    shapes: tuple[tuple[str, str], ...] = ()
    # (name, pattern as written) of each [parts] line, in file order
    parts: tuple[tuple[str, str], ...] = ()

    @property
    def shape_patterns(self) -> tuple[tuple[str, str], ...]:
        """The shapes as `readaloud.pronounce.Lexicon` takes them: each
        pattern without its slashes and with its parts filled in."""
        parts = expand_parts(self.parts)
        return tuple((_fill(pattern[1:-1], parts), say)
                     for pattern, say in self.shapes)


# --------------------------------------------------------------------------- #
# field metadata -- drives the parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Field:
    name: str
    kind: str                      # "str" | "int" | "float" | "bool"
    lo: float | None = None
    hi: float | None = None


_FIELDS: tuple[_Field, ...] = (
    _Field("voice", "str"),
    _Field("speed", "float", lo=0.5, hi=3.0),
    _Field("lang", "str"),
    _Field("repo", "str"),
    _Field("sentences", "int", lo=1),
    _Field("chars", "int", lo=1),
    _Field("prefetch", "int", lo=0),
    _Field("device", "str"),
    _Field("color", "bool"),
    _Field("follow_lead", "int", lo=0),
    _Field("follow_margin", "int", lo=0),
    _Field("media_keys", "bool"),
)

_BY_NAME = {f.name: f for f in _FIELDS}

# The dataclass and the metadata table are edited by hand; keep them honest.
# pronunciations, shapes and parts are the fields that are not [config]
# keys.
assert _BY_NAME.keys() == ({f.name for f in _dataclass_fields(Config)}
                           - {"pronunciations", "shapes", "parts"})


def _default_of(name: str) -> object:
    return getattr(Config(), name)


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

        lifted: dict[str, list[tuple[int, str]]] = {}
        try:
            ini, lifted, notes = _split_sections(text)
            pairs, problems = _parse_pronunciations(lifted[PRONUNCIATIONS])
        except Exception as exc:  # noqa: BLE001 - lose these, keep the settings
            ini, pairs, problems = text, (), []
            notes = [f"[{PRONUNCIATIONS}] could not be used ({exc!r})"]
        try:
            parts, part_problems = _parse_parts(lifted.get(PARTS, []))
            shapes, shape_problems = _parse_shapes(lifted.get(SHAPES, []),
                                                   parts)
            problems += part_problems + shape_problems
        except Exception as exc:  # noqa: BLE001 - lose these, keep the rest
            parts, shapes = (), ()
            notes.append(f"[{SHAPES}] and [{PARTS}] could not be used "
                         f"({exc!r})")
        # Set before configparser gets a say: a [config] section it rejects
        # must not cost the pronunciations as well.
        cfg.pronunciations = pairs
        cfg.shapes = shapes
        cfg.parts = parts
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
    """Set the ``[config]`` keys in `parser` on `cfg`, warning as it goes."""
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
#: What a pattern captured, named in its pronunciation: $1, ${1}, ${name}.
_GROUP_REF_RE = re.compile(r"\$(?:\d+|\{[^}]+\}|[A-Za-z_]\w*)")

#: Problems past this many are counted rather than listed: prose pasted under
#: [pronunciations] is a problem on every line, and a screenful of those would
#: bury every other warning.
_MAX_PROBLEMS = 5


def _split(text: str) -> tuple[str, list[tuple[int, str]], list[str]]:
    """`_split_sections` with the ``[pronunciations]`` lines alone."""
    ini, lifted, warnings = _split_sections(text)
    return ini, lifted[PRONUNCIATIONS], warnings


def _split_sections(text: str) -> tuple[
        str, dict[str, list[tuple[int, str]]], list[str]]:
    """Lift the ``[pronunciations]``, ``[shapes]`` and ``[parts]`` lines out
    of `text`.

    Returns ``(ini, lifted, warnings)``: the text configparser gets, each
    lifted section's lines as ``(line number, raw line)`` by its name, and
    warnings without the path in front.  Every line taken out of `ini` is
    left there empty, so configparser's line numbers stay right.

    Unknown sections are taken out too, with a warning.  configparser would
    choke on the entries of a misspelled "[pronunciation]" and throw away the
    settings with them; this way only the misspelled section is lost, and the
    warning can say which name was probably meant.  Several
    ``[pronunciations]`` sections are read as one, in file order.
    """
    kept: list[str] = []
    sections: dict[str, list[tuple[int, str]]] = {name: [] for name in _LIFTED}
    warnings: list[str] = []
    warned: set[str] = set()
    # "ini" is [config] and whatever precedes the first header, where bare
    # keys are allowed; "other" is an unknown section.
    state = "ini"                  # "ini" | one of _LIFTED | "other"
    # configparser's continuation rule, followed on every line it reads or
    # would have read: a line indented deeper than the option line above it
    # is more of that value, even when it looks like a header.  Only a
    # [pronunciations] header, which no file from before it could mean as a
    # value, is a header however it is indented.
    indent, option = 0, False
    for n, line in enumerate(text.split("\n"), 1):
        header = None
        if state in _LIFTED:
            header = _SECTION_END_RE.match(line.strip())
        elif value := line[:_comment_start(line)].strip():
            depth = len(line) - len(line.lstrip())
            header = _SECTION_RE.match(value)
            if (option and depth > indent and not (
                    header and _lifted(header))):
                header = None
            else:
                indent = depth
                if not header and (named := _OPTION_RE.match(value)):
                    option = bool(named.group("option").rstrip())
        if header:
            lifted, option = state != "ini", False
            name = header.group("name")
            if _lifted(header):
                state = _lifted(header)
            elif name in (SECTION, configparser.DEFAULTSECT):
                # [DEFAULT] stays configparser's: its keys fill [config]
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
        elif state in _LIFTED:
            sections[state].append((n, line))
        kept.append(line if state == "ini" else "")
    return "\n".join(kept), sections, warnings


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


def _lifted(header: re.Match[str]) -> str | None:
    """The section `header` names when this module reads it itself."""
    name = header.group("name").strip().lower()
    return name if name in _LIFTED else None


def _unknown_section(name: str) -> str:
    if name.strip().lower() == "readaloud":
        return (f"ignoring unknown section [{name}] "
                f"(the settings section is now [{SECTION}])")
    hint = difflib.get_close_matches(name.strip().lower(),
                                     [SECTION, *_LIFTED],
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

    Unquoted text between slashes is a pattern (``/:(\\d+)/ = line $1``, see
    :mod:`readaloud.pronounce`): its spaces are kept as written, it must
    compile, and its pronunciation may say nothing but a group.  Quoted, the
    same text is literal, and comes back as a :class:`~readaloud.pronounce.
    Literal` so the Lexicon keeps it that way.
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

    pattern = not key_quoted and is_pattern(key.strip())
    if pattern:
        key = unicodedata.normalize("NFC", key.strip())
    else:
        key = _normalise(key)
    value = _normalise(value)
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
    if pattern:
        problem = check_pattern(key[1:-1])
        if problem:
            # as written: repr would double every backslash in it
            shown = key if len(key) <= 40 else key[:40] + "..."
            return f"{at}the pattern {shown} cannot be used: {problem}"
    if not any(ch.isalnum() for ch in value) and not (
            pattern and _GROUP_REF_RE.search(value)):
        return (f"{at}{_shown(key)}: {_shown(value)} has nothing to say "
                f"(no letters or digits)")
    if not key_quoted and key.lower() in _BY_NAME:
        # The template ends in [pronunciations], so a setting appended at the
        # bottom of the file lands here.
        return (f"{at}{_shown(key)} is a setting: move it under [{SECTION}], "
                f"or put quotes round the word to pronounce it")
    if key_quoted and is_pattern(key):
        key = Literal(key)
    return key, value




def _value(rest: str) -> str:
    """The right of a ``[shapes]`` or ``[parts]`` line, as the pronunciation
    lines read theirs: quoted, or up to a # or ; after a space."""
    quoted = _quoted(rest.lstrip())
    after = quoted[1].lstrip() if quoted is not None else ""
    if quoted is not None and (not after or after[0] in "#;"):
        return quoted[0].strip()
    comment = _VALUE_COMMENT_RE.search(rest)
    return (rest[:comment.start()] if comment else rest).strip()


def _sides(line: str) -> tuple[str, str] | None:
    """A line split at its separator, as the pronunciation lines are."""
    separator = _SEPARATOR_RE.search(line)
    eq = separator.start() if separator else line.find("=")
    if eq < 0:
        return None
    return line[:eq].strip(), _value(line[eq + 1:])


def _shown_pattern(pattern: str, limit: int = 50) -> str:
    """A pattern as written in a warning: repr would double its backslashes."""
    return pattern if len(pattern) <= limit else pattern[:limit] + "..."


def _fill(pattern: str, parts: dict[str, str]) -> str:
    """`pattern` with each %{name} replaced by that part, in a group."""
    return _PART_RE.sub(lambda m: "(?:" + parts[m.group(1)] + ")", pattern)


def _unknown_parts(pattern: str, parts: dict[str, str]) -> list[str]:
    return [m.group(1) for m in _PART_RE.finditer(pattern)
            if m.group(1) not in parts]


def expand_parts(parts: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Each part by name with the parts it uses filled in.  A part only uses
    those above it, so one pass in file order fills them all."""
    out: dict[str, str] = {}
    for name, pattern in parts:
        out[name] = _fill(pattern, out)
    return out


def _parse_parts(lines: list[tuple[int, str]]
                 ) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """Return ``(parts, problems)`` for the ``[parts]`` lines.

    `parts` is ``(name, pattern as written)`` per usable line, in file order.
    A part may use the parts on the lines above it as ``%{name}``; one used
    before it is defined, an unknown one, or a pattern that does not compile
    costs only its own line.  The same name twice is a problem, and the later
    line wins.
    """
    usable: dict[str, tuple[int, str]] = {}
    filled: dict[str, str] = {}
    problems: list[str] = []
    for n, raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        at = f"[{PARTS}] line {n}: "
        sides = _sides(line)
        if sides is None:
            problems.append(f'{at}{_shown(line)} has no "="; write it as: '
                            f"name = pattern")
            continue
        name, pattern = sides
        if not _PART_NAME_RE.fullmatch(name):
            problems.append(f"{at}{_shown(name)} is not a name: use letters, "
                            f"digits and _, starting with a letter")
            continue
        if not pattern:
            problems.append(f"{at}{name!r} has no pattern")
            continue
        unknown = _unknown_parts(pattern, filled)
        if unknown:
            problems.append(f"{at}{name!r} uses %{{{unknown[0]}}}, which is "
                            f"not a part on a line above it")
            continue
        problem = check_regex(_fill(pattern, filled))
        if problem:
            problems.append(f"{at}{name!r} cannot be used: {problem}")
            continue
        if name in usable:
            problems.append(f"{at}{name!r} is also on line {usable[name][0]}; "
                            f"line {n} wins")
            del usable[name]
        usable[name] = (n, pattern)
        filled[name] = _fill(pattern, filled)
    return (tuple((name, pattern) for name, (_n, pattern) in usable.items()),
            _capped(problems, PARTS))


#: A reference in a wording: $$ (a dollar sign), $name, ${name}, or
#: ${name|from=to|...}, the group with each `from` said as the word `to`.
_REFERENCE_RE = re.compile(r"\$\$|\$\{([^}|]*)((?:\|[^}|]*)*)\}|\$(\w+)")


def _parse_shapes(lines: list[tuple[int, str]],
                  parts: tuple[tuple[str, str], ...] = ()
                  ) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """Return ``(shapes, problems)`` for the ``[shapes]`` lines.

    `shapes` is ``(/pattern/ as written, wording)`` per usable line, in file
    order, which is the order they are tried in.  The same pattern twice is a
    problem, and the later line wins, taking its own place in the order.
    """
    filled = expand_parts(parts)
    usable: dict[str, tuple[int, str]] = {}
    problems: list[str] = []
    for n, raw in lines:
        parsed = _parse_shape(n, raw, filled)
        if isinstance(parsed, str):
            problems.append(parsed)
        elif parsed is not None:
            pattern, wording = parsed
            if pattern in usable:
                problems.append(f"[{SHAPES}] line {n}: "
                                f"{_shown_pattern(pattern)} is also on line "
                                f"{usable[pattern][0]}; line {n} wins")
                del usable[pattern]
            usable[pattern] = (n, wording)
    return (tuple((pattern, wording)
                  for pattern, (_n, wording) in usable.items()),
            _capped(problems, SHAPES))


def _capped(problems: list[str], section: str) -> list[str]:
    """`problems`, past `_MAX_PROBLEMS` counted rather than listed."""
    if len(problems) > _MAX_PROBLEMS:
        hidden = len(problems) - _MAX_PROBLEMS
        more = "1 more problem" if hidden == 1 else f"{hidden} more problems"
        problems[_MAX_PROBLEMS:] = [f"[{section}]: {more} not shown"]
    return problems


def _parse_shape(n: int, raw: str, parts: dict[str, str]
                 ) -> tuple[str, str] | str | None:
    """One ``/pattern/ = wording`` line: the pair, a problem, or None.

    The separator, the quotes and the comments follow the pronunciation
    lines.  The pattern may use the parts as ``%{name}`` and must not match
    empty text; the wording may be empty, which says the text as written,
    and may name the pattern's groups as ``$name``, ``${name}`` or by number.
    """
    at = f"[{SHAPES}] line {n}: "
    line = raw.strip()
    if not line or line[0] in "#;":
        return None
    sides = _sides(line)
    if sides is None:
        return f'{at}{_shown(line)} has no "="; write it as: /pattern/ = wording'
    pattern, wording = sides
    wording = _normalise(wording)
    shown = _shown_pattern(pattern)
    if not is_pattern(pattern):
        return f"{at}{_shown(pattern)} is not a pattern between slashes"
    unknown = _unknown_parts(pattern, parts)
    if unknown:
        return f"{at}{shown} uses %{{{unknown[0]}}}, which is not a part"
    problem, groups = check_shape(_fill(pattern[1:-1], parts))
    if problem:
        return f"{at}the shape {shown} cannot be used: {problem}"
    names, count = groups
    for ch in wording:
        if unicodedata.category(ch) == "Cc" or ch == "\ufffd":
            return f"{at}{_shown(wording)} has a character it cannot say"
    found = [m for m in _REFERENCE_RE.finditer(wording) if m.group(0) != "$$"]
    references = [m.group(1) if m.group(1) is not None else m.group(3)
                  for m in found]
    for m in found:
        for swap in filter(None, (m.group(2) or "").split("|")):
            if "=" not in swap or swap.startswith("="):
                return (f"{at}{_shown(m.group(0))}: write a replacement as "
                        f"|text=word, such as ${{path|/=slash}}")
    for ref in references:
        if not (ref in names or (ref.isdigit() and int(ref) <= count)):
            known = ", ".join("$" + name for name in names) or "none"
            return (f"{at}{shown} has no group ${ref}; its named groups are "
                    f"{known}, and $0 is the whole match")
    if _LINK_RE.search(wording):
        return f"{at}[text](...) is not supported; write it the way it sounds"
    if wording and not references and not any(ch.isalnum() for ch in wording):
        return (f"{at}{_shown(wording)} has nothing to say; leave it empty to "
                f"say the text as written")
    return pattern, wording


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


def template() -> str:
    """The file a first run and ``--write-config`` write: the one shipped in
    the package as ``readaloud/readaloud.conf``, as it is."""
    return (resources.files(__package__) / "readaloud.conf").read_text(
        encoding="utf-8")


def _shipped() -> tuple[tuple[tuple[str, str], ...], ...]:
    """The pronunciations, parts and shapes the shipped file holds."""
    _ini, sections, _warnings = _split_sections(template())
    pairs, _ = _parse_pronunciations(sections[PRONUNCIATIONS])
    parts, _ = _parse_parts(sections[PARTS])
    shapes, _ = _parse_shapes(sections[SHAPES], parts)
    return pairs, parts, shapes


#: What the shipped file holds, as `load` reads it.
TEMPLATE_PRONUNCIATIONS, TEMPLATE_PARTS, TEMPLATE_SHAPES = _shipped()


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
