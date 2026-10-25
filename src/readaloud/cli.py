"""Command-line entry point for readaloud.

Responsibilities, in order:

1. parse arguments, lay the input's config layers (its suffix's, or
   ``--conf NAME``'s) over the general config, then fill every flag the user
   did *not* pass from it (see `readaloud.config`);
2. answer the "just tell me something" flags (``--list-voices``,
   ``--list-devices``) and exit;
3. obtain the input text -- ``-f FILE`` or a lone TEXT naming a file,
   positional ``TEXT``, or piped stdin -- draining stdin *completely* before
   anything else touches fd 0;
4. build the `Document`, which says the config file's pronunciations (see
   `readaloud.pronounce`); with ``renderer = mdcat`` (``markdown.conf``) the
   input is Markdown source, drawn by mdcat and read one table cell at a time
   (see `readaloud.markdown`);
5. either render the whole document to a WAV file (``--save``) or hand over to
   `readaloud.app.run`, which owns the curses session.

Precedence is strictly::

    explicit CLI flag  >  ~/.readaloud/  >  built-in default

which is why every flag with a config counterpart parses with ``default=None``:
an argparse default would make "did the user actually pass this?" unanswerable.
`_apply_config` is the only place the holes get filled.

Every failure the user can plausibly cause turns into a one-line message on
stderr and a non-zero exit status, never a traceback.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from . import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPT = 130

#: Kokoro voice names encode their language in the first letter, so the
#: pipeline's ``lang_code`` can be derived from ``--voice`` unless overridden.
_LANG_OF_VOICE = {
    "a": "a",  # American English
    "b": "b",  # British English
    "e": "e",  # Spanish
    "f": "f",  # French
    "h": "h",  # Hindi
    "i": "i",  # Italian
    "j": "j",  # Japanese
    "p": "p",  # Brazilian Portuguese
    "z": "z",  # Mandarin
}


#: shown in --help; the real path is resolved at call time, not import time
_CONFIG_HINT = "~/.readaloud/"


def _err(msg: str) -> None:
    print(f"readaloud: {msg}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    from .speech import DEFAULT_REPO_ID, DEFAULT_VOICE

    p = argparse.ArgumentParser(
        prog="readaloud",
        description=(
            "Read text aloud in the terminal with Kokoro TTS: word-level "
            "highlighting, less-style navigation, click a word to jump."
        ),
        epilog=(
            "examples:\n"
            "  readaloud notes.md\n"
            "  readaloud src/app.py\n"
            "  cat notes.md | readaloud --conf md\n"
            "  readaloud 'the quick brown fox'\n"
            "  readaloud notes.md --save notes.wav\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("text", nargs="*", metavar="TEXT",
                   help="text to read (joined with spaces)")
    p.add_argument("-f", "--file", metavar="FILE",
                   help="read from FILE instead of stdin/TEXT ('-' means stdin); "
                        "a lone TEXT that names a file is read as that file too")
    p.add_argument("--conf", default=None, metavar="NAME",
                   help="read the input with the configs for a .NAME file "
                        "(--conf md), or with the config NAME (--conf python), "
                        "whatever the input is called; see [files] in "
                        f"{_CONFIG_HINT}readaloud.conf")
    # Everything with a `~/.readaloud.conf` counterpart defaults to None and is
    # filled in by `_apply_config`; the help text shows the built-in default.
    p.add_argument("-v", "--voice", default=None, metavar="NAME",
                   help=f"Kokoro voice (default: {DEFAULT_VOICE})")
    p.add_argument("-s", "--speed", type=float, default=None, metavar="X",
                   help="speech rate multiplier (default: 1.0)")
    p.add_argument("--sentences", type=int, default=None, metavar="N",
                   help="max sentences per chunk (default: 4)")
    p.add_argument("--chars", type=int, default=None, metavar="N",
                   help="max characters per chunk (default: 380)")
    p.add_argument("--prefetch", type=int, default=None, metavar="N",
                   help="chunks to synthesize ahead of the current one (default: 2)")
    p.add_argument("--device", default=None, metavar="DEV",
                   help="audio output device: index, name substring, or 'default'")
    p.add_argument("--start", type=int, default=1, metavar="N",
                   help="start playback at chunk N (1-based, default: 1)")
    p.add_argument("--save", metavar="FILE.wav",
                   help="render the whole document to a WAV file and exit (no TUI)")
    p.add_argument("--lang", default=None, metavar="CODE",
                   help="Kokoro language code (default: derived from the voice name)")
    p.add_argument("--repo", default=None, metavar="ID",
                   help=f"HuggingFace model repo (default: {DEFAULT_REPO_ID})")
    # One destination, two spellings: `--color` exists so a flag can override
    # `color = false` in the config file, which `--no-color` alone cannot do.
    p.add_argument("--color", dest="color", action="store_true", default=None,
                   help="render the input's colours (overrides color=false in the "
                        "config file)")
    p.add_argument("--no-color", dest="color", action="store_false",
                   help="ignore colours in the input and render monochrome")
    # Same shared-dest trick as --color: two spellings, one setting, so the
    # config file can be overridden in either direction.
    p.add_argument("--media-keys", dest="media_keys", action="store_true",
                   default=None,
                   help="take over the system play/pause button (needs the "
                        "optional [mediakeys] extra; overrides "
                        "media_keys=false in the config file)")
    p.add_argument("--no-media-keys", dest="media_keys", action="store_false",
                   help="leave the system play/pause button alone")
    p.add_argument("--config", default=None, metavar="PATH",
                   help=f"read defaults from PATH, a file or a directory, instead of {_CONFIG_HINT}")
    p.add_argument("--no-config", action="store_true",
                   help="ignore the config file entirely (built-in defaults only)")
    p.add_argument("--write-config", action="store_true",
                   help="write a commented config template (overwriting it) and exit")
    p.add_argument("--list-voices", action="store_true",
                   help="print the available voices and exit")
    p.add_argument("--list-devices", action="store_true",
                   help="print the available audio output devices and exit")
    p.add_argument("--tree", action="store_true",
                   help="print the syntax tree of FILE (the node kinds and fields a "
                        "[syntax] rule can name) and exit")
    p.add_argument("--version", action="version", version=f"readaloud {__version__}")
    return p


# --------------------------------------------------------------------------- #
# ~/.readaloud.conf
# --------------------------------------------------------------------------- #


def config_path(args: argparse.Namespace) -> Path:
    """Which file `args` points the preferences at."""
    from . import config as config_mod

    if getattr(args, "config", None):
        return Path(args.config).expanduser()
    return config_mod.DEFAULT_PATH


def load_config(args: argparse.Namespace):
    """Return ``(Config, warnings)`` for `args`, honouring ``--no-config``."""
    from . import config as config_mod

    if getattr(args, "no_config", False):
        return config_mod.Config(), []
    return config_mod.load(config_path(args))


def short_notices(warnings: Sequence[str], target: Path) -> list[str]:
    """Config warnings, re-pointed for the one-line status bar.

    `config` prefixes every warning with the file's path so a stderr line says
    *which* file is wrong.  In the status bar that path is most of an 80-column
    terminal and pushes the actual complaint off the end, so swap it for the
    word "config": the reader only has one config file open.
    """
    prefix = f"{target}: "
    inside = f"{target}/"            # a file of the preferences directory
    return [f"config: {w[len(prefix):]}" if w.startswith(prefix)
            else f"config: {w[len(inside):]}" if w.startswith(inside) else w
            for w in warnings]


def _apply_config(args: argparse.Namespace, cfg) -> None:
    """Fill the flags the user did not pass.  Explicit flags are never touched.

    `lang` and `device` use "" as their "unset" value in the config but None on
    the namespace, so an empty string there has to stay None: `lang_for` and
    `Player` both read None as "work it out yourself".
    """
    if args.voice is None:
        args.voice = cfg.voice
    if args.speed is None:
        args.speed = cfg.speed
    if args.sentences is None:
        args.sentences = cfg.sentences
    if args.chars is None:
        args.chars = cfg.chars
    if args.prefetch is None:
        args.prefetch = cfg.prefetch
    if args.repo is None:
        args.repo = cfg.repo
    if args.lang is None:
        args.lang = cfg.lang or None
    if args.device is None:
        args.device = cfg.device or None
    if args.color is None:
        args.color = bool(cfg.color)
    # Whether the user *asked* for media keys, as opposed to inheriting the
    # default.  Only an explicit request earns an explanation when PyObjC is
    # missing; saying it unprompted would nag every user who never wanted it.
    args.media_keys_explicit = args.media_keys is not None
    if args.media_keys is None:
        args.media_keys = bool(cfg.media_keys)


def input_file(args: argparse.Namespace) -> str | None:
    """The file named for the input, ``-f FILE`` ('-' is stdin), or None."""
    return getattr(args, "file", None)


def name_the_file(args: argparse.Namespace) -> None:
    """Take a lone TEXT that names an existing file for ``-f`` of that file:
    ``readaloud notes.md`` reads the file.  Nobody wants a file's name read
    out; two names, or one that is not a file, stay TEXT to be spoken."""
    if (args.file is None and len(args.text) == 1
            and os.path.isfile(args.text[0])):
        args.file, args.text = args.text[0], []


def layers_for_input(args: argparse.Namespace, cfg) -> tuple[str, ...]:
    """The config layers that read the input: ``--conf NAME``'s, as the
    ``[files]`` line for ``.NAME`` has them or else NAME itself, or the
    input file's by its suffix; none for a pipe or TEXT."""
    conf = getattr(args, "conf", None)
    if conf:
        name = conf.strip().lower()
        return cfg.layers_for("." + name.lstrip(".")) or (name,)
    path = input_file(args)
    if path and path != "-":
        return cfg.layers_for(Path(path).suffix)
    return ()


def print_tree(args: argparse.Namespace, cfg) -> int:
    """``--tree``: print the syntax tree of the named file through its
    grammar, one node per line, for writing ``[syntax]`` rules."""
    from . import syntax

    name_the_file(args)
    path = input_file(args)
    if not path or path == "-":
        _err("--tree needs a file: readaloud --tree app.py")
        return EXIT_USAGE
    suffix = Path(path).suffix
    layers = layers_for_input(args, cfg)
    language = cfg.language.strip().lower() or (layers[-1] if layers else None)
    if language is None:
        _err(f"no grammar is known for {suffix or 'a file without a suffix'}; "
             f"name one in [files], set language, or pass --conf")
        return EXIT_ERROR
    try:
        grammar = syntax.grammar_for(language, cfg.parsers)
        text = read_input(args)
    except (syntax.GrammarError, OSError) as exc:
        _err(str(exc))
        return EXIT_ERROR
    print(syntax.outline(text, grammar), end="")
    return EXIT_OK


def grammar_for_input(args: argparse.Namespace, cfg, layers: Sequence[str]):
    """The grammar the input is read with, for the config's ``[syntax]``
    rules and for colouring it, and the notices on the way: ``(grammar or
    None, language or None, notices)``.

    The grammar is the ``language`` setting, else the last of the input's
    `layers` (``python`` of ``.py = code python``), and is looked for only
    when the config has rules or wants colour; without one, or with rules
    the grammar does not have, the notices say so and the input is read
    without.
    """
    from . import config as config_mod
    from . import syntax

    if not (cfg.syntax or cfg.highlight):
        return None, None, []
    language = cfg.language.strip().lower() or (layers[-1] if layers else None)
    at = f"{config_path(args)}: "
    if language is None:
        path = input_file(args)
        if not cfg.syntax or not path or path == "-":
            return None, None, []          # nothing to say it with: fine
        suffix = Path(path).suffix or "no"
        return None, None, [
            f"{at}[{config_mod.SYNTAX}]: no grammar is known for {suffix} "
            f"files; name one in [{config_mod.FILES}], or set language"]
    try:
        grammar = syntax.grammar_for(language, cfg.parsers)
    except syntax.GrammarError as exc:
        return None, language, [f"{at}{exc}"] if cfg.syntax else []
    problems = syntax.check_rules(cfg.syntax, grammar, config_mod.SYNTAX)
    return grammar, language, [f"{at}{p}" for p in problems]


def highlighter_for_input(args: argparse.Namespace, cfg, grammar, language):
    """What colours the input file by its `grammar`, or None, and the
    notices on the way: nothing without a grammar, with ``highlight = no``,
    with ``--no-color``, or for a grammar that ships no highlights query."""
    from . import syntax

    if grammar is None or not cfg.highlight or not args.color:
        return None, []
    query = syntax.highlights_for(language, cfg.parsers)
    if query is None:
        return None, []
    try:
        return syntax.highlighter(grammar, query), []
    except syntax.GrammarError as exc:
        return None, [f"{config_path(args)}: {language}: {exc}"]


def document_name(args: argparse.Namespace) -> str:
    """What to call this document -- Control Center shows it as the "artist".

    A file gets its basename; anything piped or typed has no name, so it gets
    the one thing that is true of it.
    """
    path = input_file(args)
    if path and path != "-":
        return Path(path).name
    return "readaloud"


def lang_for(voice: str, override: str | None) -> str:
    if override:
        return override[:1].lower()
    return _LANG_OF_VOICE.get((voice or "a")[:1].lower(), "a")


def check_voice(voice: str, repo: str, lang: str) -> str | None:
    """Return an error message when `voice` is not a voice this repo has.

    Returns `None` when the voice is fine, or when the voice list cannot be
    determined (a custom repo we know nothing about must not be blocked).
    Without this check an unknown voice loads the model happily and then fails
    at synthesis time, once per chunk: the TUI races to the end of the document
    in silence and exits 0.
    """
    import difflib

    from .speech import list_voices

    try:
        known = list_voices(repo)
    except Exception:  # noqa: BLE001 - a broken listing must not block startup
        return None
    if not known or voice in known:
        return None

    same_lang = [v for v in known if v[:1] == (lang or "")[:1]] or known
    # prefer suggestions the requested language can actually speak
    hints = (difflib.get_close_matches(voice, same_lang, n=3, cutoff=0.6)
             or difflib.get_close_matches(voice, known, n=3, cutoff=0.6)
             or same_lang[:3])
    return (f"unknown voice {voice!r}; did you mean "
            f"{', '.join(hints)}?  (see --list-voices)")


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #


def read_input(args: argparse.Namespace) -> str:
    """Return the raw text to read, draining stdin when that is the source.

    Raises `OSError` for an unreadable ``--file``.
    """
    from .ui import read_stdin_text

    path = input_file(args)
    if path and path != "-":
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    if path == "-":
        return read_stdin_text()
    if args.text:
        return " ".join(args.text)
    # `read_stdin_text` returns "" when fd 0 is a terminal, i.e. nothing piped.
    return read_stdin_text()


def build_document(data: str, *, max_sentences: int, max_chars: int,
                   no_color: bool, pronunciations=None,
                   format: str = "markdown", highlighter=None):
    """Parse `data` (ANSI or raw markdown) into a `Document`.

    `format` is the config file's setting for plain input: ``markdown`` has
    its syntax stripped and a code block read whole, ``code`` is read as
    written with its indented lines cut like a paragraph and its operators
    lit, ``plain`` is read as written as prose.  Coloured input is none of
    these: it keeps what it came with.  `highlighter`, from
    `readaloud.syntax`, colours plain input by its grammar.

    `pronunciations` is the config file's `readaloud.pronounce.Lexicon`, which
    the Document respells its chunks with.
    """
    from . import ansi
    from .document import Document

    lines = ansi.parse(data)
    code = False
    if not ansi.has_ansi(data):
        if format == "markdown":
            lines = ansi.strip_markdown(lines)
        code = format == "code"
        if highlighter is not None:
            lines = highlighter(lines)
    if no_color:
        lines = _monochrome(lines)
    return Document(lines, max_sentences=max_sentences, max_chars=max_chars,
                    pronunciations=pronunciations, markdown=not code)


def build_markdown_document(data: str, *, mdcat: str, columns: int,
                            max_sentences: int, max_chars: int,
                            no_color: bool, pronunciations=None):
    """Render Markdown source with mdcat into a `Document` that reads tables by cell.

    Returns ``(Document, notices)``: one line per table that could not be
    mapped and is read line by line instead.  Raises
    `readaloud.markdown.MdcatError` when mdcat cannot render the document.

    mdcat's lines go in exactly as rendered: `ansi.strip_markdown` would take
    an unstyled render for raw Markdown and move characters of the lines the
    tables were measured on.  ``references=False`` keeps a footnote's ``[1]``
    on screen and reads the footnote.  mdcat still writes link references for
    an image inside a link (a badge's ``[1]`` and its ``[1]: URL`` line); the
    Document silences those itself, outside the tables.  `pronunciations`
    respells the chunks, cells included, as in `build_document`.
    """
    from . import markdown
    from .document import Document

    rendered = markdown.render_markdown(data, mdcat=mdcat, columns=columns)
    lines = _monochrome(rendered.lines) if no_color else rendered.lines
    doc = Document(lines, max_sentences=max_sentences, max_chars=max_chars,
                   tables=rendered.tables, references=False,
                   pronunciations=pronunciations)
    return doc, list(rendered.notices)


def _monochrome(lines):
    """`lines` without colours; bold, italics and links stay (``--no-color``)."""
    return [
        [replace(r, style=replace(r.style, fg=None, bg=None)) for r in line]
        for line in lines
    ]


# --------------------------------------------------------------------------- #
# --save
# --------------------------------------------------------------------------- #


def check_writable(path: str) -> str | None:
    """Return an error message when `path` cannot be opened for writing.

    Used as a pre-flight so a mistyped ``--save`` destination is reported in
    the first second rather than after the whole document has been rendered.
    An existing file is opened without truncation and a file we create for the
    probe is removed again, so a failed run never destroys the old contents.
    """
    existed = os.path.exists(path)
    try:
        fh = open(path, "r+b" if existed else "wb")
    except OSError as exc:
        return f"could not write {path}: {exc}"
    fh.close()
    if not existed:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover - vanished under us; harmless
            pass
    return None


def save_wav(doc, path: str, *, voice: str, speed: float, lang: str,
             repo: str, quiet: bool = False) -> int:
    """Synthesize every speakable chunk and write one WAV file.  Returns 0/1."""
    import wave

    import numpy as np

    from .speech import SAMPLE_RATE, Engine

    speakable = doc.speakable_chunks
    if not speakable:
        _err("nothing speakable in the input")
        return EXIT_ERROR

    unwritable = check_writable(path)
    if unwritable:
        _err(unwritable)
        return EXIT_ERROR

    engine = Engine(voice=voice, speed=speed, lang_code=lang, repo_id=repo)
    try:
        try:
            engine.load()
        except BaseException as exc:  # noqa: BLE001 - becomes a message
            if isinstance(exc, KeyboardInterrupt):
                raise
            _err(f"could not load the voice model: {exc}")
            return EXIT_ERROR

        parts: list[np.ndarray] = []
        failures = 0
        for n, cidx in enumerate(speakable, 1):
            if not quiet:
                print(f"\rsynthesizing chunk {n}/{len(speakable)}...",
                      end="", file=sys.stderr, flush=True)
            spoken = engine.synth(doc.chunks[cidx])
            if spoken.audio.size == 0:
                failures += 1
                continue
            parts.append(spoken.audio)
            # a short breath between chunks -- except after a table cell, whose
            # audio already ends in the pause the TUI plays between cells
            if getattr(doc.chunks[cidx], "kind", "") != "cell":
                parts.append(np.zeros(int(SAMPLE_RATE * 0.20), dtype=np.float32))
        if not quiet:
            print("\r" + " " * 40 + "\r", end="", file=sys.stderr, flush=True)
    finally:
        engine.close()

    if not parts:
        _err("synthesis produced no audio" +
             (f": {engine.last_error}" if engine.last_error else ""))
        return EXIT_ERROR

    audio = np.concatenate(parts)
    pcm = np.clip(audio, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    try:
        # Open the file ourselves: `wave.open(path, ...)` opens it inside
        # `Wave_write.__init__` *before* `self._file` exists, so a failure
        # there leaves a half-built object whose `__del__` raises an
        # AttributeError that CPython dumps to stderr as a traceback.
        with open(path, "wb") as fh, wave.open(fh, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())
    except OSError as exc:
        _err(f"could not write {path}: {exc}")
        return EXIT_ERROR

    secs = len(audio) / float(SAMPLE_RATE)
    note = f" ({failures} chunk(s) failed)" if failures else ""
    print(f"wrote {path}: {secs:.1f}s, {len(speakable) - failures} chunks{note}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def _main(argv: Sequence[str] | None = None) -> int:
    from . import config as config_mod

    parser = build_parser()
    given = list(argv if argv is not None else sys.argv[1:])
    if "-md" in given or "--markdown" in given:
        # the flag up to 2.0: Markdown is a config's business now
        _err("-md is gone: readaloud notes.md reads a Markdown file through "
             "markdown.conf, and a pipe with readaloud --conf md")
        return EXIT_USAGE
    args = parser.parse_args(given)
    name_the_file(args)

    # ---- ~/.readaloud/ --------------------------------------------------
    # The input's layers, by its suffix or --conf, are laid over the general
    # config before any setting is applied.
    target = config_path(args)
    # Up to 2.0 the preferences were one file; the first run of a newer
    # readaloud splits it into the directory, once, and says so on stderr,
    # which is still safe before curses starts.
    if not args.no_config and target == config_mod.DEFAULT_PATH:
        for note in config_mod.migrate():
            _err(note)
    cfg, warnings = load_config(args)
    layers = layers_for_input(args, cfg)
    cfg = cfg.for_layers(layers)
    _apply_config(args, cfg)

    # Anything that is not the TUI can print to stderr; the reader gets its
    # warnings through the status bar instead (see `notices=` below), because a
    # print() lands on top of the curses screen.
    headless = bool(args.save or args.write_config or args.list_voices
                    or args.list_devices or args.tree)
    if headless:
        for warning in warnings:
            _err(warning)

    if args.write_config:
        try:
            written = config_mod.write_template(target)
        except OSError as exc:
            _err(f"could not write {target}: {exc.strerror or exc}")
            return EXIT_ERROR
        if written.is_dir():
            print(f"wrote {', '.join(str(written / n) for n in config_mod.SHIPPED_FILES)}")
        else:
            print(f"wrote {written}")
        return EXIT_OK

    # First run: leave a fully commented template behind.  `ensure` never
    # raises and never overwrites, so an unwritable HOME just means no file.
    if not args.no_config and (created := config_mod.ensure(target)):
        if not args.save:
            # --save's stdout is the machine-readable half; keep it clean and
            # say nothing there.  Before curses starts, stderr is still safe.
            where = f"{target}/" if target.is_dir() else str(target)
            _err(f"created {where} -- your defaults live there now"
                 + (f" ({', '.join(p.name for p in created)})" if target.is_dir() else ""))

    if args.list_devices:
        from .player import format_devices
        print(format_devices())
        return EXIT_OK

    if args.tree:
        return print_tree(args, cfg)

    lang = lang_for(args.voice, args.lang)

    if args.list_voices:
        from .speech import list_voices
        names = list_voices(args.repo, lang)
        print(f"voices for lang '{lang}' (repo {args.repo}):")
        for name in names:
            print(f"  {name}")
        return EXIT_OK

    if args.speed <= 0:
        _err("--speed must be greater than 0")
        return EXIT_USAGE
    if args.prefetch < 0:
        _err("--prefetch must be 0 or more")
        return EXIT_USAGE
    if args.sentences < 1 or args.chars < 1:
        _err("--sentences and --chars must be 1 or more")
        return EXIT_USAGE

    bad_voice = check_voice(args.voice, args.repo, lang)
    if bad_voice:
        _err(bad_voice)
        return EXIT_USAGE

    # ---- renderer = mdcat -----------------------------------------------
    # Looked up before the input is read; without mdcat the Markdown is read
    # as written, with a word in the status bar.
    mdcat = None
    if cfg.renderer == "mdcat":
        from . import markdown as markdown_mod

        mdcat = markdown_mod.find_mdcat()
        if mdcat is None:
            warnings.append(f"{target}: renderer = mdcat, but mdcat is not "
                            f"installed (brew install mdcat); the Markdown is "
                            f"read as written")

    # ---- input ----------------------------------------------------------
    try:
        data = read_input(args)
    except OSError as exc:
        _err(f"could not read {input_file(args)}: {exc}")
        return EXIT_ERROR

    if not data.strip():
        if input_file(args) or args.text:
            _err("the input is empty")
        else:
            _err("no input: pipe something in, pass TEXT, or name a FILE")
            _err("try 'readaloud --help', 'readaloud notes.md', or "
                 "'cat notes.md | readaloud --conf md'")
        return EXIT_USAGE

    # Built once whichever way the document is read; --no-config has none.
    from .pronounce import Lexicon

    grammar, language, grammar_notices = grammar_for_input(args, cfg, layers)
    warnings += grammar_notices
    pronunciations = Lexicon(cfg.pronunciations, cfg.shape_patterns,
                             cfg.sub_patterns, grammar, cfg.syntax)
    highlighter, highlight_notices = highlighter_for_input(
        args, cfg, grammar, language)
    warnings += highlight_notices
    no_color = not args.color
    md_notices: list[str] = []
    if mdcat is not None:
        from .ansi import has_ansi

        if has_ansi(data):
            mdcat = None            # drawn already: read it as it came
    if mdcat is not None:
        # A WAV has no screen to fit, so it renders at mdcat's own width
        # rather than at whatever terminal --save happened to run in.
        columns = 80 if args.save else markdown_mod.render_width()
        try:
            doc, md_notices = build_markdown_document(
                data,
                mdcat=mdcat,
                columns=columns,
                max_sentences=args.sentences,
                max_chars=args.chars,
                no_color=no_color,
                pronunciations=pronunciations,
            )
        except markdown_mod.MdcatError as exc:
            _err(str(exc))
            return EXIT_ERROR
    else:
        doc = build_document(
            data,
            max_sentences=args.sentences,
            max_chars=args.chars,
            no_color=no_color,
            pronunciations=pronunciations,
            format=cfg.format,
            highlighter=highlighter,
        )
    if not doc.speakable_chunks:
        _err("the input contains nothing speakable (only rules, symbols or blanks)")
        return EXIT_ERROR

    # What the document has to say about itself, after the config warnings:
    # the tables mdcat's render reads line by line, and the chunks a pronunciation could
    # not be applied to (only a bug does that, so it is worth a line).
    notices = list(md_notices)
    if doc.respell_failures:
        count = doc.respell_failures
        chunks = "1 chunk is" if count == 1 else f"{count} chunks are"
        notices.append(f"pronunciations: {chunks} read as written "
                       "(a pronunciation could not be applied)")

    # ---- --save: no terminal needed -------------------------------------
    if args.save:
        # warnings, not output: stdout stays the one machine-readable line
        for notice in notices:
            _err(notice)
        return save_wav(doc, args.save, voice=args.voice, speed=args.speed,
                        lang=lang, repo=args.repo)

    # ---- TUI ------------------------------------------------------------
    # stdin has been drained; point fd 0 (and fd 1, if redirected) at the real
    # terminal before curses touches anything.  Skipping this fails silently:
    # initscr() succeeds and every getch() returns -1 forever.
    from .ui import reopen_tty

    try:
        reopen_tty()
    except OSError as exc:
        _err(f"no controlling terminal to draw on ({exc.strerror or exc}); "
             "run readaloud from a terminal, or use --save FILE.wav")
        return EXIT_ERROR
    if not os.isatty(0):  # pragma: no cover - belt and braces
        _err("stdin is still not a terminal after reopening /dev/tty")
        return EXIT_ERROR

    from .app import run

    # `--start N` counts the *speakable* chunks, which is what the status bar
    # shows ("chunk N/M"); App itself works in document chunk indices.
    speakable = doc.speakable_chunks
    nth = min(max(0, int(args.start) - 1), len(speakable) - 1)
    start = speakable[nth]
    return run(
        doc,
        voice=args.voice,
        speed=args.speed,
        lang=lang,
        repo=args.repo,
        prefetch=args.prefetch,
        device=args.device,
        start_chunk=start,
        no_color=no_color,
        follow_lead=cfg.follow_lead,
        follow_margin=cfg.follow_margin,
        media_keys=bool(args.media_keys),
        media_keys_explicit=bool(getattr(args, "media_keys_explicit", False)),
        doc_name=document_name(args),
        notices=short_notices(warnings, target) + notices,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point.  Never raises; returns a process exit code."""
    try:
        return _main(argv)
    except KeyboardInterrupt:
        # The TUI catches its own Ctrl-C; this is the pre-curses / --save path.
        print("", file=sys.stderr)
        _err("interrupted")
        return EXIT_INTERRUPT
    except BrokenPipeError:  # pragma: no cover - `readaloud --list-voices | head`
        return EXIT_OK


entry = main  # backwards-compatible alias


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
