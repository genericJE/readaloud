"""Command-line entry point for readaloud.

Responsibilities, in order:

1. parse arguments, then fill every flag the user did *not* pass from
   ``~/.readaloud.conf`` (see `readaloud.config`);
2. answer the "just tell me something" flags (``--list-voices``,
   ``--list-devices``) and exit;
3. obtain the input text -- ``-f FILE`` (or ``-md FILE``), positional ``TEXT``,
   or piped stdin -- draining stdin *completely* before anything else touches
   fd 0;
4. build the `Document`, which says the config file's pronunciations (see
   `readaloud.pronounce`); with ``-md`` the input is Markdown source, rendered
   by mdcat and read one table cell at a time (see `readaloud.markdown`);
5. either render the whole document to a WAV file (``--save``) or hand over to
   `readaloud.app.run`, which owns the curses session.

Precedence is strictly::

    explicit CLI flag  >  ~/.readaloud.conf  >  built-in default

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
_CONFIG_HINT = "~/.readaloud.conf"


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
            "  readaloud -md notes.md\n"
            "  mdcat --ansi notes.md | readaloud\n"
            "  readaloud -f README.md\n"
            "  readaloud 'the quick brown fox'\n"
            "  readaloud -f notes.md --save notes.wav\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("text", nargs="*", metavar="TEXT",
                   help="text to read (joined with spaces)")
    p.add_argument("-f", "--file", metavar="FILE",
                   help="read from FILE instead of stdin/TEXT ('-' means stdin)")
    # Takes the file like -f, or modifies -f, TEXT and stdin when given alone,
    # so the value is True, a path, or None when the flag is absent.  Test it
    # with `is not None`: `-md ''` is a (falsy) path.
    p.add_argument("-md", "--markdown", nargs="?", const=True, default=None,
                   metavar="FILE",
                   help="read FILE (or the -f file, TEXT or stdin) as Markdown: "
                        "show it with mdcat --ansi and read tables one cell at "
                        "a time (needs mdcat)")
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
                   help=f"read defaults from PATH instead of {_CONFIG_HINT}")
    p.add_argument("--no-config", action="store_true",
                   help="ignore the config file entirely (built-in defaults only)")
    p.add_argument("--write-config", action="store_true",
                   help="write a commented config template (overwriting it) and exit")
    p.add_argument("--list-voices", action="store_true",
                   help="print the available voices and exit")
    p.add_argument("--list-devices", action="store_true",
                   help="print the available audio output devices and exit")
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
    return [f"config: {w[len(prefix):]}" if w.startswith(prefix) else w
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
    """The file named for the input: ``-md FILE``, else ``-f FILE`` ('-' is stdin).

    A bare ``-md`` names no file; it only says how to read the others.
    """
    markdown = getattr(args, "markdown", None)
    if isinstance(markdown, str):
        return markdown
    return getattr(args, "file", None)


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

    Raises `OSError` for an unreadable ``--file`` (or ``-md FILE``).
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
                   no_color: bool, pronunciations=None):
    """Parse `data` (ANSI or raw markdown) into a `Document`.

    `pronunciations` is the config file's `readaloud.pronounce.Lexicon`, which
    the Document respells its chunks with.
    """
    from . import ansi
    from .document import Document

    lines = ansi.parse(data)
    if not ansi.has_ansi(data):
        lines = ansi.strip_markdown(lines)
    if no_color:
        lines = _monochrome(lines)
    return Document(lines, max_sentences=max_sentences, max_chars=max_chars,
                    pronunciations=pronunciations)


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
    args = parser.parse_args(argv)

    # ---- ~/.readaloud.conf ----------------------------------------------
    target = config_path(args)
    cfg, warnings = load_config(args)
    _apply_config(args, cfg)

    # Anything that is not the TUI can print to stderr; the reader gets its
    # warnings through the status bar instead (see `notices=` below), because a
    # print() lands on top of the curses screen.
    headless = bool(args.save or args.write_config or args.list_voices
                    or args.list_devices)
    if headless:
        for warning in warnings:
            _err(warning)

    if args.write_config:
        try:
            written = config_mod.write_template(target)
        except OSError as exc:
            _err(f"could not write {target}: {exc.strerror or exc}")
            return EXIT_ERROR
        print(f"wrote {written}")
        return EXIT_OK

    # First run: leave a fully commented template behind.  `ensure` never
    # raises and never overwrites, so an unwritable HOME just means no file.
    if not args.no_config and config_mod.ensure(target):
        if not args.save:
            # --save's stdout is the machine-readable half; keep it clean and
            # say nothing there.  Before curses starts, stderr is still safe.
            _err(f"created {target} -- your defaults live there now")

    if args.list_devices:
        from .player import format_devices
        print(format_devices())
        return EXIT_OK

    lang = lang_for(args.voice, args.lang)

    if args.list_voices:
        from .speech import list_voices
        names = list_voices(args.repo, lang)
        print(f"voices for lang '{lang}' (repo {args.repo}):")
        for name in names:
            print(f"  {name}")
        return EXIT_OK

    if isinstance(args.markdown, str) and args.file is not None:
        _err("use either -md FILE or -f FILE -md")
        return EXIT_USAGE
    if (args.markdown is True and args.file is None and len(args.text) == 1
            and os.path.isfile(args.text[0])):
        # `readaloud notes.md -md` and `-md --save x.wav notes.md` leave the
        # file in TEXT, since a bare -md takes no value there.  Nobody wants a
        # file's name rendered as Markdown, so read the file it names.
        args.markdown, args.text = args.text[0], []
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

    # ---- -md needs mdcat ------------------------------------------------
    # Looked up before the input is read, so a pipe is not drained for nothing.
    mdcat = None
    if args.markdown is not None:
        from . import markdown as markdown_mod

        mdcat = markdown_mod.find_mdcat()
        if mdcat is None:
            _err("-md needs mdcat to render Markdown (brew install mdcat); "
                 "without it, readaloud -f FILE reads the file as plain Markdown")
            return EXIT_ERROR

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
            _err("no input: pipe something in, pass TEXT, or use -f FILE")
            _err("try 'readaloud --help', 'readaloud -md notes.md', or "
                 "'mdcat --ansi notes.md | readaloud'")
        return EXIT_USAGE

    # Built once whichever way the document is read; --no-config has none.
    from .pronounce import Lexicon

    pronunciations = Lexicon(cfg.pronunciations)
    no_color = not args.color
    md_notices: list[str] = []
    if mdcat is not None:
        from .ansi import has_ansi

        if has_ansi(data):
            # a pipe or a saved render alike: nothing here assumes a pipe
            _err("-md wants Markdown source, not mdcat's output: "
                 "readaloud -md notes.md")
            return EXIT_USAGE
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
        )
    if not doc.speakable_chunks:
        _err("the input contains nothing speakable (only rules, symbols or blanks)")
        return EXIT_ERROR

    # What the document has to say about itself, after the config warnings:
    # the tables -md reads line by line, and the chunks a pronunciation could
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
