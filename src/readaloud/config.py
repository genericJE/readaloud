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
use as ``%{name}``.  A named shape, ``param: /pattern/ = wording``, is never
found on its own: the shapes below it use it as ``%{param}`` in a pattern and
as ``${group|param}`` in a wording, which says each of its matches in that
group.  ``[syntax]`` lines, ``kind = wording``, say a node of a source
file's syntax tree (`readaloud.syntax`) by its kind, the wording naming the
node's fields.  Each section may also have one for a file type,
``[shapes.py]`` and the like, which `Config.for_file` lays over the general
one for a file with that extension.  readaloud has no shape and no part of its own: a file
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
from collections.abc import Sequence
from dataclasses import dataclass, field, fields as _dataclass_fields, replace
from importlib import resources
from pathlib import Path

from .pronounce import (Literal, check_pattern, check_regex, check_shape,
                        is_pattern)

__all__ = [
    "Config",
    "FileType",
    "DEFAULT_PATH",
    "FORMATS",
    "LEGACY_PATH",
    "MAIN_FILE",
    "SHIPPED_FILES",
    "PRONUNCIATIONS",
    "PARTS",
    "SHAPES",
    "SYNTAX",
    "FILES",
    "TEMPLATE_PARTS",
    "TEMPLATE_PRONUNCIATIONS",
    "TEMPLATE_SHAPES",
    "TEMPLATE_FILES",
    "migrate",
    "split_legacy",
    "templates",
    "expand_parts",
    "SECTION",
    "ensure",
    "load",
    "template",
    "write_template",
]

#: The general file inside a preferences directory, beside ``code.conf``
#: and a ``<language>.conf`` per language.
MAIN_FILE = "readaloud.conf"

#: Where the preferences live.  Resolved at call time (``path or DEFAULT_PATH``)
#: so tests can point the module elsewhere.
DEFAULT_PATH = Path.home() / ".readaloud"

#: Where the preferences lived up to 2.0: one file, which `migrate` splits
#: into the directory on the first run of a newer readaloud.
LEGACY_PATH = Path.home() / ".readaloud.conf"

#: The files a first run writes into the directory, in `templates`' order:
#: the general one first, then the code one, then a language's.
SHIPPED_FILES = (MAIN_FILE, "markdown.conf", "python.conf")

SECTION = "config"

#: The section of words and phrases readaloud should say differently.
PRONUNCIATIONS = "pronunciations"

#: The section of shapes: text described by a pattern (a file and its line, a
#: host and its port) and what it says.
SHAPES = "shapes"

#: The section of named pieces of pattern the shapes are built from.
PARTS = "parts"

#: The section of rules saying a source file's syntax tree, node kind by kind.
SYNTAX = "syntax"

#: The section mapping a file's extension to the configs that read it.
FILES = "files"

#: The sections this module reads itself, rather than configparser.
_LIFTED = (PRONUNCIATIONS, SHAPES, PARTS, SYNTAX, FILES)

#: A part used in a pattern: %{name}.
_PART_RE = re.compile(r"%\{(\w+)\}")
#: A name a part may have.
_PART_NAME_RE = re.compile(r"[A-Za-z_]\w*")


@dataclass(frozen=True)
class FileType:
    """One config layer: the sections headed with its name in a file
    (``[shapes.python]``) or the plain sections of ``<name>.conf``.  The
    ``[files]`` section says which layers read a file, by its extension."""

    name: str                      # "code", "python": in lower case
    settings: tuple[tuple[str, object], ...] = ()
    pronunciations: tuple[tuple[str, str], ...] = ()
    shapes: tuple[tuple[str, str], ...] = ()
    named_shapes: tuple[tuple[str, str, str], ...] = ()
    parts: tuple[tuple[str, str], ...] = ()
    syntax: tuple[tuple[str, str], ...] = ()


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
    format: str = "markdown"       # how plain input is read: markdown, code or plain
    highlight: bool = True         # colour a source file by its grammar
    language: str = ""             # the grammar's name; "" = the last layer's
    renderer: str = "none"         # draw plain input with mdcat first, or not
    parsers: str = ""              # a directory of parser/<language>.so files
    # (written, spoken) pairs from [pronunciations], in file order
    pronunciations: tuple[tuple[str, str], ...] = ()
    # (pattern as written, wording) of each [shapes] line, in file order
    shapes: tuple[tuple[str, str], ...] = ()
    # (name, pattern as written, wording) of each named [shapes] line
    named_shapes: tuple[tuple[str, str, str], ...] = ()
    # (name, pattern as written) of each [parts] line, in file order
    parts: tuple[tuple[str, str], ...] = ()
    # (node kind, wording) of each [syntax] line, in file order
    syntax: tuple[tuple[str, str], ...] = ()
    # (extension, layer names) of each [files] line, in file order; the
    # shipped file's unless a file gives its own
    files: tuple[tuple[str, tuple[str, ...]], ...] = field(
        default_factory=lambda: TEMPLATE_FILES)
    # the layers: the sections headed [config.code], [pronunciations.code]
    # and so on, and each <name>.conf of the directory, in load order
    types: tuple[FileType, ...] = ()

    def layers_for(self, suffix: str) -> tuple[str, ...]:
        """The layer names the ``[files]`` section gives for `suffix`
        (``.py``), in order; none for a suffix it does not name."""
        suffix = suffix.lower()
        for extension, layers in self.files:
            if extension == suffix:
                return layers
        return ()

    def grammar_name(self, suffix: str) -> str | None:
        """The grammar a file with `suffix` is read with: the ``language``
        setting, else the last of its layers, else None."""
        if self.language.strip():
            return self.language.strip().lower().replace("-", "_")
        layers = self.layers_for(suffix)
        return layers[-1] if layers else None

    def for_file(self, path: str | Path | None) -> Config:
        """This config with the sections for `path`'s file type laid over
        it; itself for no file, stdin (``-``), or a type with no sections."""
        if not path or str(path) == "-":
            return self
        return self.for_suffix(Path(path).suffix)

    def for_suffix(self, suffix: str) -> Config:
        """This config with the layers ``[files]`` gives for `suffix`
        (``.py``) laid over it, see `for_layers`."""
        return self.for_layers(self.layers_for(suffix))

    def for_layers(self, names: Sequence[str]) -> Config:
        """This config with the layers called `names` laid over it, in that
        order, each winning over the one before: a layer's settings replace
        the earlier ones, its shapes are tried first, and its pronunciations,
        parts, named shapes and syntax rules come after the earlier ones."""
        out = self
        for name in names:
            for typed in self.types:
                if typed.name != name.lower():
                    continue
                out = replace(
                    out, **dict(typed.settings),
                    pronunciations=out.pronunciations + typed.pronunciations,
                    shapes=typed.shapes + out.shapes,
                    named_shapes=out.named_shapes + typed.named_shapes,
                    parts=out.parts + typed.parts,
                    syntax=out.syntax + typed.syntax)
        return replace(out, types=()) if out is not self else self

    @property
    def shape_patterns(self) -> tuple[tuple[str, str], ...]:
        """The shapes as `readaloud.pronounce.Lexicon` takes them: each
        pattern without its slashes and with its parts and named shapes
        filled in."""
        fills, _subs = _shape_fills(self.parts, self.named_shapes)
        return tuple((_fill(pattern[1:-1], fills), say)
                     for pattern, say in self.shapes)

    @property
    def sub_patterns(self) -> tuple[tuple[str, str, str], ...]:
        """The named shapes as `readaloud.pronounce.Lexicon` takes them:
        ``(name, pattern, wording)``, the pattern filled in like a shape's."""
        return _shape_fills(self.parts, self.named_shapes)[1]


# --------------------------------------------------------------------------- #
# field metadata -- drives the parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Field:
    name: str
    kind: str                      # "str" | "int" | "float" | "bool"
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()  # a "str" that must be one of these


#: How plain input is read, the `format` setting: Markdown syntax stripped and
#: a code block read whole; code, as written, its indented lines cut like a
#: paragraph and its operators lit; or plain prose, as written.
FORMATS = ("markdown", "code", "plain")


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
    _Field("format", "str", choices=FORMATS),
    _Field("highlight", "bool"),
    _Field("language", "str"),
    _Field("renderer", "str", choices=("none", "mdcat")),
    _Field("parsers", "str"),
)

_BY_NAME = {f.name: f for f in _FIELDS}

# The dataclass and the metadata table are edited by hand; keep them honest.
# pronunciations, shapes, named shapes and parts are the fields that are not
# [config] keys.
assert _BY_NAME.keys() == ({f.name for f in _dataclass_fields(Config)}
                           - {"pronunciations", "shapes", "named_shapes",
                              "parts", "syntax", "files", "types"})


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
            warnings: list[str], where: str = "") -> object | None:
    """Return the value for `field`, or ``None`` to keep the default."""
    default = _default_of(field.name)
    value = _unquote(raw)

    if field.kind == "str":
        if field.choices and value.strip().lower() not in field.choices:
            warnings.append(
                f"{path}: {where}{field.name}: {raw.strip()!r} is not one of "
                f"{', '.join(field.choices)}; using {default}")
            return None
        return value.strip().lower() if field.choices else value

    if field.kind == "bool":
        try:
            return _BOOLEANS[value.strip().lower()]
        except KeyError:
            warnings.append(
                f"{path}: {where}{field.name}: {raw.strip()!r} is not a boolean "
                f"(try true/false); using {'true' if default else 'false'}")
            return None

    try:
        number: float | int = (int(value, 10) if field.kind == "int"
                               else float(value))
    except ValueError:
        kind = "a whole number" if field.kind == "int" else "a number"
        warnings.append(f"{path}: {where}{field.name}: {raw.strip()!r} is not {kind}; "
                        f"using {default}")
        return None

    if field.lo is not None and number < field.lo:
        clamped = int(field.lo) if field.kind == "int" else field.lo
        warnings.append(f"{path}: {where}{field.name}: {number} is below the minimum "
                        f"{clamped}; clamped to {clamped}")
        return clamped
    if field.hi is not None and number > field.hi:
        clamped = int(field.hi) if field.kind == "int" else field.hi
        warnings.append(f"{path}: {where}{field.name}: {number} is above the maximum "
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
    """Parse the preferences.  Returns ``(config, warnings)``.

    `path` is a file, or a directory holding `MAIN_FILE` for what applies to
    everything, ``code.conf`` for every source file and a ``<language>.conf``
    (``python.conf``) for each language, whose plain sections apply to that
    language's files alone; see `Config.for_file`.

    A missing file is NOT a warning -- it yields plain defaults.  A malformed
    value IS a warning and falls back to the default for that key; one bad key
    never discards the rest of the file, and this never raises.  The same goes
    for ``[pronunciations]``: a line that makes no sense is a warning and is
    skipped, and the settings never depend on that section being right.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    try:
        if target.is_dir():
            return _load_dir(target)
    except OSError:
        pass
    cfg, _settings, warnings = _load_file(target)
    return cfg, warnings


def _load_dir(folder: Path) -> tuple[Config, list[str]]:
    """`load` for a directory: `MAIN_FILE`, then ``code.conf``, then each
    other ``<language>.conf``, whose parts and named shapes may use those of
    the files before them."""
    cfg, _settings, warnings = _load_file(folder / MAIN_FILE)
    types = list(cfg.types)
    parts, named = cfg.parts, cfg.named_shapes
    try:
        others = sorted(f for f in folder.iterdir()
                        if f.suffix == ".conf" and f.name != MAIN_FILE)
    except OSError as exc:
        return cfg, warnings + [f"{folder}: could not be listed ({exc!r})"]
    # the layers a [files] line names first come first, so a later one's
    # parts and shapes may use theirs ("code python": python after code)
    rank = {name: i for ext, layers in cfg.files for i, name in enumerate(layers)}
    others.sort(key=lambda f: (rank.get(f.stem.lower(), len(rank)), f.name))
    for file in others:
        name = file.stem.lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            warnings.append(f"{file}: ignored; a layer's file is named after "
                            f"its layer, as python.conf")
            continue
        own, settings, own_warnings = _load_file(file, parts, named, layer=name)
        warnings += own_warnings
        types.append(FileType(name, tuple(settings.items()), own.pronunciations,
                              own.shapes, own.named_shapes, own.parts,
                              own.syntax))
        types.extend(own.types)
        parts, named = parts + own.parts, named + own.named_shapes
    cfg.types = tuple(types)
    return cfg, warnings


def _load_file(target: Path,
               known_parts: tuple[tuple[str, str], ...] = (),
               known_named: tuple[tuple[str, str, str], ...] = (),
               layer: str | None = None,
               ) -> tuple[Config, dict[str, object], list[str]]:
    """`load` for one file: ``(config, the settings it names, warnings)``.
    Its parts and shapes may use the `known_parts` and `known_named` shapes
    of the files before it.  A `layer`'s file (``python.conf``) has no
    ``[files]`` of its own: that is the main file's to say."""
    cfg = Config()
    try:
        text, warnings = _read_text(target)
        if text is None:
            return cfg, {}, warnings

        lifted: dict[str, list[tuple[int, str]]] = {}
        try:
            ini, lifted, notes = _split_sections(text)
            pairs, problems = _parse_pronunciations(lifted[PRONUNCIATIONS])
        except Exception as exc:  # noqa: BLE001 - lose these, keep the settings
            ini, pairs, problems = text, (), []
            notes = [f"[{PRONUNCIATIONS}] could not be used ({exc!r})"]
        try:
            parts, part_problems = _parse_parts(lifted.get(PARTS, []),
                                                known=known_parts)
            shapes, named, shape_problems = _parse_shapes(
                lifted.get(SHAPES, []), known_parts + parts, known_named)
            problems += part_problems + shape_problems
        except Exception as exc:  # noqa: BLE001 - lose these, keep the rest
            parts, shapes, named = (), (), ()
            notes.append(f"[{SHAPES}] and [{PARTS}] could not be used "
                         f"({exc!r})")
        try:
            rules, rule_problems = _parse_syntax(lifted.get(SYNTAX, []))
            problems += rule_problems
        except Exception as exc:  # noqa: BLE001 - lose these, keep the rest
            rules = ()
            notes.append(f"[{SYNTAX}] could not be used ({exc!r})")
        files: tuple[tuple[str, tuple[str, ...]], ...] = ()
        try:
            if layer is not None and lifted.get(FILES):
                problems.append(f"[{FILES}] belongs in {MAIN_FILE}, not in "
                                f"{layer}.conf; ignored")
            else:
                files, file_problems = _parse_files(lifted.get(FILES, []))
                problems += file_problems
        except Exception as exc:  # noqa: BLE001 - lose these, keep the rest
            notes.append(f"[{FILES}] could not be used ({exc!r})")
        try:
            typed = _parse_types(lifted, known_parts + parts,
                                 known_named + named, problems)
        except Exception as exc:  # noqa: BLE001 - lose these, keep the rest
            typed = {}
            notes.append(f"the sections for file types could not be used "
                         f"({exc!r})")
        # Set before configparser gets a say: a [config] section it rejects
        # must not cost the pronunciations as well.
        cfg.pronunciations = pairs
        cfg.shapes = shapes
        cfg.named_shapes = named
        cfg.parts = parts
        cfg.syntax = rules
        if files:
            cfg.files = files
        warnings += [f"{target}: {note}" for note in notes]

        parser, parse_warnings = _parse(ini, target)
        warnings += parse_warnings
        general: dict[str, object] = {}
        settings: dict[str, dict[str, object]] = {}
        if parser is not None:
            general, settings = _read_settings(parser, cfg, target, warnings)
        cfg.types = tuple(
            FileType(name, tuple(settings.get(name, {}).items()),
                     **typed.get(name, {}))
            for name in dict.fromkeys([*typed, *settings]))
        warnings += [f"{target}: {problem}" for problem in problems]
        return cfg, general, warnings
    except Exception as exc:  # noqa: BLE001 - the whole point: never raise
        return Config(), {}, [f"{target}: could not be used ({exc!r}); "
                              f"using defaults"]


def _read_settings(parser: configparser.ConfigParser, cfg: Config,
                   path: Path, warnings: list[str]
                   ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    """Set the ``[config]`` keys in `parser` on `cfg`, warning as it goes,
    and return them by name, with each ``[config.<ext>]`` section's settings
    by suffix."""
    typed: list[tuple[str, str]] = []
    for name in parser.sections():
        kind = _typed(name)
        if kind is not None and kind[0] == SECTION:
            typed.append((name, kind[1]))
        elif name != SECTION:
            warnings.append(f"{path}: ignoring unknown section [{name}]")

    general: dict[str, object] = {}
    if parser.has_section(SECTION):
        general = _read_section(parser.items(SECTION), path, warnings)
        for field, value in general.items():
            setattr(cfg, field, value)
    settings: dict[str, dict[str, object]] = {}
    for name, suffix in typed:
        # a type's own keys only: [DEFAULT]'s are already the general ones
        own = parser._sections[name].items()  # noqa: SLF001
        settings.setdefault(suffix, {}).update(
            _read_section(own, path, warnings, f"[{name.lower()}] "))
    return general, settings


def _read_section(items, path: Path, warnings: list[str], where: str = ""
                  ) -> dict[str, object]:
    """The settings among `items` (``(key, raw value)``) by field name,
    warning as it goes; `where` names a file type's section in a warning."""
    out: dict[str, object] = {}
    for key, raw in items:
        field = _BY_NAME.get(key.strip().lower())
        if field is None:
            # A key nothing like a setting is most likely a pronunciation in
            # a file written before [pronunciations] existed.
            like = difflib.get_close_matches(key.strip().lower(), _BY_NAME,
                                             n=1, cutoff=0.6)
            hint = ("" if like else
                    f"; a pronunciation goes under [{PRONUNCIATIONS}]")
            if key.strip().lower() == "markdown":
                # the setting up to 2.0; `split_legacy` rewrites it
                hint = f"; it is format = {' | '.join(FORMATS)} now"
            warnings.append(f"{path}: {where}unknown key {key!r} "
                            f"(ignored{hint})")
            continue
        value = _coerce(field, raw if raw is not None else "",
                        path, warnings, where)
        if value is not None:
            out[field.name] = value
    return out


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
    state = "ini"                  # "ini" | a lifted section | "other"
    # configparser's continuation rule, followed on every line it reads or
    # would have read: a line indented deeper than the option line above it
    # is more of that value, even when it looks like a header.  Only a
    # [pronunciations] header, which no file from before it could mean as a
    # value, is a header however it is indented.
    indent, option = 0, False
    for n, line in enumerate(text.split("\n"), 1):
        header = None
        if state in sections:
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
                sections.setdefault(state, [])
            elif (name in (SECTION, configparser.DEFAULTSECT)
                  or (_typed(name) or ("",))[0] == SECTION):
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
        elif state in sections:
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
    """The section `header` names when this module reads it itself: one of
    `_LIFTED`, or one for a file type such as ``shapes.py``."""
    name = header.group("name").strip().lower()
    if name in _LIFTED or (_typed(name) or ("",))[0] in _LIFTED:
        return name
    return None


#: A section for one file type: ``shapes.py``, the section and an extension.
_TYPE_RE = re.compile(r"(?P<base>[a-z]+)\.(?P<name>[a-z][a-z0-9_]*)")


def _typed(name: str) -> tuple[str, str] | None:
    """``(section, layer)`` when `name` is a section of one layer, such as
    ``("shapes", "python")`` for ``[Shapes.Python]``."""
    match = _TYPE_RE.fullmatch(name.strip().lower())
    if match is None or match["base"] not in (SECTION, *_LIFTED) or match["base"] == FILES:
        return None
    return match["base"], match["name"]


def _unknown_section(name: str) -> str:
    if name.strip().lower() == "readaloud":
        return (f"ignoring unknown section [{name}] "
                f"(the settings section is now [{SECTION}])")
    base, dot, ext = name.strip().lower().partition(".")
    hint = difflib.get_close_matches(base if dot else name.strip().lower(),
                                     [SECTION, *_LIFTED],
                                     n=1, cutoff=0.6)
    guess = (f" (did you mean [{hint[0]}{dot}{ext}]?)" if hint and (
        not dot or re.fullmatch(_TYPE_RE.pattern, f"{hint[0]}.{ext}"))
        else "")
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


#: A named group's opening, ``(?<name>`` or ``(?P<name>``.
_GROUP_OPEN_RE = re.compile(r"\(\?P?<[A-Za-z_]\w*>")


def _unnamed(pattern: str) -> str:
    """`pattern` with its named groups made non-capturing, so it may stand
    in a pattern more than once."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            out.append(pattern[i:i + 2])
            i += 2
            continue
        opening = _GROUP_OPEN_RE.match(pattern, i)
        if opening:
            out.append("(?:")
            i = opening.end()
        else:
            out.append(pattern[i])
            i += 1
    return "".join(out)


def _shape_fills(parts: tuple[tuple[str, str], ...],
                 named: tuple[tuple[str, str, str], ...]
                 ) -> tuple[dict[str, str], tuple[tuple[str, str, str], ...]]:
    """What each ``%{name}`` stands for in a shape, parts and named shapes
    alike, and each named shape ``(name, filled pattern, wording)``.  A named
    shape uses the parts and the named shapes above it, and stands in a
    pattern with its groups unnamed."""
    fills = expand_parts(parts)
    subs = []
    for name, pattern, wording in named:
        filled = _fill(pattern[1:-1], fills)
        subs.append((name, filled, wording))
        fills[name] = _unnamed(filled)
    return fills, tuple(subs)


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


def _parse_parts(lines: list[tuple[int, str]],
                 known: tuple[tuple[str, str], ...] = ()
                 ) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """Return ``(parts, problems)`` for the ``[parts]`` lines.

    `parts` is ``(name, pattern as written)`` per usable line, in file order.
    A part may use the parts on the lines above it as ``%{name}``; one used
    before it is defined, an unknown one, or a pattern that does not compile
    costs only its own line.  The same name twice is a problem, and the later
    line wins.  The `known` parts, a file type's general ones, are above
    every line.
    """
    usable: dict[str, tuple[int, str]] = {}
    filled: dict[str, str] = expand_parts(known)
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
#: ${name|from=to|...}, the group with each `from` said as the word `to`;
#: a piece without "=", ${name|shape}, says each match of a named shape.
_REFERENCE_RE = re.compile(r"\$\$|\$\{([^}|]*)((?:\|[^}|]*)*)\}|\$(\w+)")
#: A named shape's name, before its pattern: ``param: /pattern/``.
_NAMED_SHAPE_RE = re.compile(r"([A-Za-z_]\w*)\s*:\s*(?=/)")


def _parse_shapes(lines: list[tuple[int, str]],
                  parts: tuple[tuple[str, str], ...] = (),
                  named_above: tuple[tuple[str, str, str], ...] = ()
                  ) -> tuple[tuple[tuple[str, str], ...],
                             tuple[tuple[str, str, str], ...], list[str]]:
    """Return ``(shapes, named, problems)`` for the ``[shapes]`` lines.

    `shapes` is ``(/pattern/ as written, wording)`` per usable line, in file
    order, which is the order they are tried in.  `named` is ``(name,
    /pattern/, wording)`` per usable named line: a named shape is never
    found on its own, but the lines below it may use it, as ``%{name}`` in a
    pattern or ``${group|name}`` in a wording.  The same pattern, or the same
    name, twice is a problem, and the later line wins, taking its own place
    in the order.  The `named_above` shapes, a file type's general ones, are
    above every line.
    """
    fills, _subs = _shape_fills(parts, named_above)
    above = {name: None for name, _pattern, _wording in named_above}
    usable: dict[str, tuple[int, str]] = {}
    named: dict[str, tuple[int, str, str]] = {}
    problems: list[str] = []
    for n, raw in lines:
        parsed = _parse_shape(n, raw, fills, {**above, **named})
        if isinstance(parsed, str):
            problems.append(parsed)
        elif parsed is not None:
            name, pattern, wording = parsed
            at = f"[{SHAPES}] line {n}: "
            if name is None:
                if pattern in usable:
                    problems.append(f"{at}{_shown_pattern(pattern)} is also on "
                                    f"line {usable[pattern][0]}; line {n} wins")
                    del usable[pattern]
                usable[pattern] = (n, wording)
                continue
            if any(name == part for part, _ in parts):
                problems.append(f"{at}{name!r} is also a part; give the shape "
                                f"another name")
                continue
            if name in named:
                problems.append(f"{at}{name!r} is also on line "
                                f"{named[name][0]}; line {n} wins")
                del named[name]
            named[name] = (n, pattern, wording)
            fills[name] = _unnamed(_fill(pattern[1:-1], fills))
    return (tuple((pattern, wording)
                  for pattern, (_n, wording) in usable.items()),
            tuple((name, pattern, wording)
                  for name, (_n, pattern, wording) in named.items()),
            _capped(problems, SHAPES))


def _parse_types(lifted: dict[str, list[tuple[int, str]]],
                 parts: tuple[tuple[str, str], ...],
                 named: tuple[tuple[str, str, str], ...],
                 problems: list[str]) -> dict[str, dict[str, tuple]]:
    """The ``[pronunciations.code]``, ``[parts.python]`` and so on lines by
    layer name, as `FileType` fields; their problems go on `problems`.  A
    layer's parts and shapes may use the general parts and named shapes,
    and those of the layers headed before it."""
    out: dict[str, dict[str, tuple]] = {}
    for label in lifted:
        kind = _typed(label)
        if kind is None or kind[1] in out:
            continue
        suffix = "." + kind[1]
        if out:
            parts = parts + out[list(out)[-1]]["parts"]
            named = named + out[list(out)[-1]]["named_shapes"]

        def lines(base: str) -> list[tuple[int, str]]:
            return lifted.get(base + suffix, [])

        def relabelled(found: list[str], base: str) -> list[str]:
            return [f"[{base}{suffix}]" + p[len(base) + 2:]
                    if p.startswith(f"[{base}]") else p for p in found]

        pairs, found = _parse_pronunciations(lines(PRONUNCIATIONS))
        problems += relabelled(found, PRONUNCIATIONS)
        own_parts, found = _parse_parts(lines(PARTS), known=parts)
        problems += relabelled(found, PARTS)
        shapes, own_named, found = _parse_shapes(
            lines(SHAPES), parts + own_parts, named)
        problems += relabelled(found, SHAPES)
        rules, found = _parse_syntax(lines(SYNTAX))
        problems += relabelled(found, SYNTAX)
        out[kind[1]] = {"pronunciations": pairs, "parts": own_parts,
                        "shapes": shapes, "named_shapes": own_named,
                        "syntax": rules}
    return out


#: An extension in a [files] line: a dot and the suffix, as Path.suffix has it.
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9_+-]+")
#: A layer's name: a config of the directory (python.conf) or a header's tail.
_LAYER_RE = re.compile(r"[a-z][a-z0-9_]*")


def _parse_files(lines: list[tuple[int, str]]
                 ) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], list[str]]:
    """Return ``(files, problems)`` for the ``[files]`` lines.

    A line is ``.ext .ext2 = layer layer``: the files with those extensions
    are read with those layers, each laid over the one before, the last
    naming the grammar.  `files` is ``(extension, layers)`` per extension,
    in file order; the same extension twice is a problem, and the later line
    wins.
    """
    usable: dict[str, tuple[int, tuple[str, ...]]] = {}
    problems: list[str] = []
    for n, raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        at = f"[{FILES}] line {n}: "
        sides = _sides(line)
        if sides is None:
            problems.append(f'{at}{_shown(line)} has no "="; write it as: '
                            f".ext = layer layer")
            continue
        exts, layers = sides
        extensions = exts.split()
        names = tuple(layers.lower().split())
        bad = next((e for e in extensions if not _EXTENSION_RE.fullmatch(e)), None)
        if not extensions or bad is not None:
            problems.append(f"{at}{_shown(bad or line)} is not an extension: "
                            f"write it with its dot, as .py")
            continue
        bad = next((l for l in names if not _LAYER_RE.fullmatch(l)), None)
        if not names or bad is not None:
            problems.append(f"{at}{_shown(bad or line)} is not a layer's name: "
                            f"letters, digits and _, as code or python")
            continue
        for ext in extensions:
            ext = ext.lower()
            if ext in usable:
                problems.append(f"{at}{ext} is also on line {usable[ext][0]}; "
                                f"line {n} wins")
                del usable[ext]
            usable[ext] = (n, names)
    return (tuple((ext, names) for ext, (_n, names) in usable.items()),
            _capped(problems, FILES))


#: A syntax rule's key: a node kind, an ancestor's kind and a kind, or a
#: kind and the field of it the node is.
_RULE_KEY_RE = re.compile(r"[A-Za-z_]\w*(?:[/.][A-Za-z_]\w*)?")
#: A reference in a rule's wording: $$, $field, ${field|from=to}, $0 (the
#: node as written) or $* (the node's parts in order).
_RULE_REFERENCE_RE = re.compile(r"\$\$|\$\{([^}|]*)((?:\|[^}|]*)*)\}|\$(\w+|\*)")


def _parse_syntax(lines: list[tuple[int, str]]
                  ) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    """Return ``(rules, problems)`` for the ``[syntax]`` lines.

    A rule is ``kind = wording``; ``ancestor/kind = wording`` for such a
    node only under a node of the first kind; or ``kind.field = wording``
    for whatever node is that field of such a node.  `rules` is ``(key,
    wording)`` per usable line, in file order.  The wording names the node's fields as
    ``$name``, says the node as written as ``$0`` and its parts in order as
    ``$*``, with ``|from=to`` replacements as a shape's wording has them;
    an empty wording says the node as written.  Whether a kind or a field
    exists is the grammar's to say, when it is loaded.  The same key twice
    is a problem, and the later line wins.
    """
    usable: dict[str, tuple[int, str]] = {}
    problems: list[str] = []
    for n, raw in lines:
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        at = f"[{SYNTAX}] line {n}: "
        sides = _sides(line)
        if sides is None:
            problems.append(f'{at}{_shown(line)} has no "="; write it as: '
                            f"kind = wording")
            continue
        key, wording = sides
        wording = _normalise(wording)
        if not _RULE_KEY_RE.fullmatch(key):
            problems.append(f"{at}{_shown(key)} is not a node kind: a rule "
                            f"is kind = wording, ancestor/kind = wording, "
                            f"or kind.field = wording")
            continue
        problem = _rule_problem(wording)
        if problem:
            problems.append(f"{at}{key}: {problem}")
            continue
        if key in usable:
            problems.append(f"{at}{key!r} is also on line {usable[key][0]}; "
                            f"line {n} wins")
            del usable[key]
        usable[key] = (n, wording)
    return (tuple((key, wording) for key, (_n, wording) in usable.items()),
            _capped(problems, SYNTAX))


def _rule_problem(wording: str) -> str | None:
    """Why a syntax rule's `wording` cannot be used, or None."""
    for ch in wording:
        if unicodedata.category(ch) == "Cc" or ch == "\ufffd":
            return f"{_shown(wording)} has a character it cannot say"
    found = [m for m in _RULE_REFERENCE_RE.finditer(wording)
             if m.group(0) != "$$"]
    for m in found:
        name = m.group(1) if m.group(1) is not None else m.group(3)
        if name.isdigit() and name != "0":
            return (f"{_shown(m.group(0))}: a rule names a node's fields, "
                    f"$0 for the node as written, or $* for its parts")
        if not (name == "*" or _PART_NAME_RE.fullmatch(name) or name == "0"):
            return f"{_shown(m.group(0))} names no field"
        for swap in filter(None, (m.group(2) or "").split("|")):
            if "=" not in swap or swap.startswith("="):
                return (f"{_shown(m.group(0))}: write a replacement as "
                        f"|text=word, such as ${{*|,=and}}")
    if _LINK_RE.search(wording):
        return "[text](...) is not supported; write it the way it sounds"
    if wording and not found and not any(ch.isalnum() for ch in wording):
        return (f"{_shown(wording)} has nothing to say; leave it empty to "
                f"say the node as written")
    return None


def _capped(problems: list[str], section: str) -> list[str]:
    """`problems`, past `_MAX_PROBLEMS` counted rather than listed."""
    if len(problems) > _MAX_PROBLEMS:
        hidden = len(problems) - _MAX_PROBLEMS
        more = "1 more problem" if hidden == 1 else f"{hidden} more problems"
        problems[_MAX_PROBLEMS:] = [f"[{section}]: {more} not shown"]
    return problems


def _parse_shape(n: int, raw: str, parts: dict[str, str],
                 named: dict[str, object] | None = None
                 ) -> tuple[str | None, str, str] | str | None:
    """One ``[name:] /pattern/ = wording`` line: ``(name or None, pattern,
    wording)``, a problem, or None.

    The separator, the quotes and the comments follow the pronunciation
    lines.  The pattern may use the parts, and the named shapes in `parts`,
    as ``%{name}`` and must not match empty text; the wording may be empty,
    which says the text as written, may name the pattern's groups as
    ``$name``, ``${name}`` or by number, and may say a group through one of
    the `named` shapes as ``${group|name}``.
    """
    named = named or {}
    at = f"[{SHAPES}] line {n}: "
    line = raw.strip()
    if not line or line[0] in "#;":
        return None
    sides = _sides(line)
    if sides is None:
        return f'{at}{_shown(line)} has no "="; write it as: /pattern/ = wording'
    pattern, wording = sides
    wording = _normalise(wording)
    name = _NAMED_SHAPE_RE.match(pattern)
    if name:
        pattern = pattern[name.end():]
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
        shapes_named = []
        for swap in filter(None, (m.group(2) or "").split("|")):
            if "=" not in swap:
                shapes_named.append(swap)
            elif swap.startswith("="):
                return (f"{at}{_shown(m.group(0))}: write a replacement as "
                        f"|text=word, such as ${{path|/=slash}}")
        for called in shapes_named:
            if called not in named:
                return (f"{at}{_shown(m.group(0))} names no shape above it; "
                        f"write a replacement as |text=word, and a named "
                        f"shape as name: /pattern/ = wording")
        if len(shapes_named) > 1:
            return f"{at}{_shown(m.group(0))} names two shapes; use one"

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
    return (name.group(1) if name else None), pattern, wording


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


def template(name: str = MAIN_FILE) -> str:
    """One of the files a first run and ``--write-config`` write, shipped in
    the package under ``readaloud/configs/``, as it is: `MAIN_FILE` by
    default."""
    return (resources.files(__package__) / "configs" / name).read_text(
        encoding="utf-8")


def templates() -> dict[str, str]:
    """Every file a first run writes, by name, in the order written."""
    return {name: template(name) for name in SHIPPED_FILES}


def _is_directory(target: Path) -> bool:
    """Whether `target` is, or is meant to be, a preferences directory: one
    that exists, or a path without a ``.conf`` suffix."""
    try:
        if target.is_dir():
            return True
        if target.exists():
            return False
    except OSError:
        return False
    return target.suffix.lower() != ".conf"


def _shipped() -> tuple[tuple, ...]:
    """The pronunciations, parts, shapes and [files] the shipped file holds."""
    _ini, sections, _warnings = _split_sections(template())
    pairs, _ = _parse_pronunciations(sections[PRONUNCIATIONS])
    parts, _ = _parse_parts(sections[PARTS])
    shapes, _named, _ = _parse_shapes(sections[SHAPES], parts)
    files, _ = _parse_files(sections[FILES])
    return pairs, parts, shapes, files


#: What the shipped file holds, as `load` reads it.
TEMPLATE_PRONUNCIATIONS, TEMPLATE_PARTS, TEMPLATE_SHAPES, TEMPLATE_FILES = _shipped()


#: A line that is nothing but a section header, its inline comment aside.
_HEADER_LINE_RE = re.compile(r"\[(?P<name>[^\]]+)\]")
#: The ``markdown = yes|no`` setting of 2.0, which `split_legacy` rewrites.
_OLD_MARKDOWN_RE = re.compile(r"(?P<indent>\s*)markdown\s*=\s*(?P<value>\S+)(?P<rest>.*)$")


def split_legacy(text: str) -> dict[str, str]:
    """The one-file preferences `text` as the files of a directory, by name.

    The sections for a file type go to that type's file with the suffix
    taken off their headers: ``[pronunciations.code]`` to ``code.conf`` as
    ``[pronunciations]``, ``[syntax.py]`` to ``python.conf`` as
    ``[syntax]``.  The comment lines right above a header move with it.
    Everything else stays in `MAIN_FILE` as it was: the sections headed
    with a layer's name (``[config.code]``), which the directory's main file
    holds too, and an extension the shipped ``[files]`` does not know.  The
    ``markdown`` setting of 2.0 becomes ``format``: ``markdown = no`` is
    ``format = code``, and a ``markdown = yes`` goes, being the default.
    """
    def language_for(suffix: str) -> str | None:
        layers = dict(TEMPLATE_FILES).get(suffix.lower(), ())
        return layers[-1] if layers else None

    files: dict[str, list[str]] = {MAIN_FILE: []}
    current = MAIN_FILE
    pending: list[str] = []            # comments and blanks awaiting a header
    for line in text.split("\n"):
        bare = line[:_comment_start(line)].strip()
        header = _HEADER_LINE_RE.fullmatch(bare)
        if (old := _OLD_MARKDOWN_RE.match(line)) and not header:
            if old.group("value").lower() in _BOOLEANS and not _BOOLEANS[old.group("value").lower()]:
                line = f"{old.group('indent')}format = code{old.group('rest')}"
            else:
                continue
        if header:
            name = header.group("name")
            kind = _typed(name)
            current = MAIN_FILE
            if kind is not None:
                base, tail = kind
                language = language_for("." + tail)
                if language is not None:
                    current = f"{language}.conf"
                    line = line.replace(f"[{name}]", f"[{base}]", 1)
            files.setdefault(current, []).extend(pending)
            pending = []
            files[current].append(line)
        elif not bare and (not line.strip() or line.lstrip()[0] in "#;"):
            pending.append(line)        # a comment or blank: may belong below
        else:
            files[current].extend(pending)
            pending = []
            files[current].append(line)
    files[current].extend(pending)
    out = {}
    for name, lines in files.items():
        body = "\n".join(lines).strip("\n")
        if body.strip():
            out[name] = body + "\n"
    return out


def migrate(old: Path | None = None, folder: Path | None = None) -> list[str]:
    """Split the one-file preferences at `old` (`LEGACY_PATH`) into the
    directory `folder` (`DEFAULT_PATH`), and delete the file.  Returns the
    notices to show; nothing happens unless `old` is a file and `folder`
    does not exist.  Never raises."""
    old = Path(old) if old is not None else LEGACY_PATH
    folder = Path(folder) if folder is not None else DEFAULT_PATH
    try:
        if not old.is_file() or folder.exists():
            return []
        text, notes = _read_text(old)
        if text is None:
            return notes
        files = split_legacy(text)
        folder.mkdir(parents=True)
        for name, body in files.items():
            (folder / name).write_text(body, encoding="utf-8")
        old.unlink()
        return [f"moved {old} into {folder}/: "
                f"{', '.join(files)} (the settings section and your "
                f"pronunciations are in {MAIN_FILE}; the sections for a "
                f"file type are in that language's file)"]
    except Exception as exc:  # noqa: BLE001 - never stop the reader
        return [f"could not move {old} into {folder}: {exc!r}"]


def write_template(path: Path | None = None) -> Path:
    """Write the shipped files to `path` (overwriting them) and return the
    path: every file of `templates` into a directory, the general one alone
    into a ``.conf`` file."""
    target = Path(path) if path is not None else DEFAULT_PATH
    if _is_directory(target):
        target.mkdir(parents=True, exist_ok=True)
        for name, text in templates().items():
            (target / name).write_text(text, encoding="utf-8")
    else:
        target.write_text(template(), encoding="utf-8")
    return target


def ensure(path: Path | None = None) -> list[Path]:
    """Create the shipped files that are absent: the directory and each of
    `templates` for a directory, the general file for a ``.conf`` path.
    Returns the paths written, so an empty list when nothing was.

    Never raises -- an unwritable HOME must not stop the reader from starting.
    """
    target = Path(path) if path is not None else DEFAULT_PATH
    written: list[Path] = []
    try:
        if _is_directory(target):
            target.mkdir(parents=True, exist_ok=True)
            wanted = [(target / name, text) for name, text in templates().items()]
        else:
            wanted = [(target, template())]
        for file, text in wanted:
            try:
                # "x" rather than write_template's truncate: two readaloud
                # processes starting at once must not leave a half-written
                # file, and an existing file is never clobbered even if it
                # appears between the two calls.
                with open(file, "x", encoding="utf-8") as fh:
                    fh.write(text)
                written.append(file)
            except FileExistsError:
                continue
    except Exception:  # noqa: BLE001 - unwritable HOME, a file in the way, ...
        pass
    return written
