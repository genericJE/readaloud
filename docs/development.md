# Development

Installing from a checkout, how the modules fit together, running the tests, and cutting
a release.

## How it fits together

| Module | Job |
| --- | --- |
| `cli.py` | arguments, the input's config layers, config precedence, reading the input, `--save`, and handing Markdown to `markdown.py` when `renderer = mdcat` |
| `ansi.py` | escape-sequence parser to styled `Run`s; Markdown cleanup for non-ANSI input |
| `document.py` | words with character offsets, the sentence/paragraph chunker, and table cells |
| `pronounce.py` | `[pronunciations]`: finding the text in a chunk and respelling it, so every word keeps its highlight |
| `markdown.py` | `renderer = mdcat`: runs mdcat and maps each rendered table's cells back to the source |
| `width.py` | how many terminal cells a character takes (wide CJK and emoji take two) |
| `syntax.py` | source code as a syntax tree: the tree-sitter runtime with a language's grammar loaded at run time, from the `tree-sitter-<language>` wheel or a Neovim `parser/<language>.so` |
| `_core` | the compiled half, built from `rust/`: the computation behind `width.py`, `pronounce.py`, `ansi.py`, `syntax.py`, and the words, sentences and chunks of `document.py` |
| `speech.py` | Kokoro pipeline, the token to word-slot timestamp alignment, and trimming a cell's silence |
| `player.py` | one persistent PortAudio stream with a lock-free callback |
| `ui.py` | curses view: wrapping, lazy 256-colour pairs, hit-testing, status bar |
| `keys.py` | terminal input decoding (SGR mouse, CSI keys) and the keymap |
| `app.py` | the loop, the prefetch worker, and the playback state machine |
| `config.py` | `~/.readaloud/`: parsing the settings and the `[pronunciations]` lines, clamping, laying the layers `[files]` names over `readaloud.conf`, splitting an old `~/.readaloud.conf` into the directory; the files a first run writes are under `src/readaloud/configs/` |
| `mediakeys.py` | the macOS "Now Playing" role, so the headphone button reaches us |

## From a checkout

`uv tool install --editable '.[mediakeys,grammars]'` needs [`uv`][uv], [`rustup`][rustup] and
Apple silicon. `uv` supplies everything Python, including the espeak fallback, so there
is no `brew install espeak-ng` step. `rustup` supplies the Rust compiler that
`rust-toolchain.toml` names, which builds `readaloud._core` as part of the install.

The first run downloads `mlx-community/Kokoro-82M-4bit` (~300 MB) into the HuggingFace
cache; later runs load it in a few seconds. Only the safetensors and voice packs are
fetched. The repo also ships a PyTorch checkpoint of the same weights that an MLX build
never opens, and skipping it halves the download.

## Working on it

```bash
uv sync --extra mediakeys
uv run pytest -q
uv run readaloud -f README.md
```

The work that is pure computation lives in Rust, in `rust/src/`, and is compiled into
`readaloud._core` by [maturin][maturin]. Each Python module keeps its name and its
functions and calls into `_core`, so nothing else imports it directly. `uv run`
rebuilds the extension whenever a file under `rust/` changed (`tool.uv.cache-keys` in
`pyproject.toml`), so the tests always run against the current Rust; a bare `pytest`
does not rebuild. The extension lands in `src/readaloud/` and is not committed. The
Python each module had before its port stays in `tests/reference/`, and the
`tests/test_core_*.py` tests check the port against it over random input.

The Rust side reads its character classes (letters and digits, spaces, capitals,
widths) from `rust/src/tables.rs`, generated from Python's own Unicode database so both
halves agree on every character. Regenerate it whenever the pinned Python changes:

```bash
uv run python scripts/gen-unicode-tables.py
PYO3_PYTHON="$(uv python find 3.12)" cargo test --manifest-path rust/Cargo.toml
```

`tests/test_integration.py` drives the real binary under a pseudo-terminal with a pipe on
stdin, which is the only way to exercise the curses path. `curses` cannot initialise
without a tty, and skipping the `/dev/tty` reopen fails *silently*: every `getch()` just
returns -1 forever. Tests marked `slow` need the model weights and an audio device; skip
them with `-m "not slow"`. The tests that render with mdcat are skipped when it is not
installed.

While the TUI is up, `stderr` is parked on a temp file. The HuggingFace fetch bar and
phonemizer's "words count mismatch" both arrive after curses owns the screen and would
otherwise shred the display. It is replayed only if the app crashes. Set
`READALOUD_DEBUG=1` to also report a synthesis worker still busy at exit (harmless: it is
a daemon thread).

## Recording the demo

`docs/demo/build.sh` records `docs/demo.mp4`: a Markdown file drawn by mdcat and read
cell by cell, then a Python file read through its grammar, with the speech muxed in. It
needs [vhs][vhs], ffmpeg, mdcat and the model weights, and it really speaks out loud. The
script's header carries what the recording taught: vhs captures no audio, so `tap.py`
writes what the player plays and `align.py` lines it up with the first word that lights.

## Cutting a release

Add the release to `CHANGELOG.md`, bump `version` in `pyproject.toml` and `__version__`
in `src/readaloud/__init__.py`, run `uv lock`, commit, then push a `vX.Y.Z` tag. The
`release` workflow builds the bundle on a `macos-14` runner, compiling `readaloud._core`
with the pinned Rust (the tree-sitter runtime's C is compiled there too) and installing
the `mediakeys` and `grammars` extras, and attaches it. Then update three lines in the
tap's `Formula/readaloud.rb`: `url`, `version`, `sha256`, and put the changelog entry on
the GitHub release.

The runner's macOS version is load-bearing. mlx publishes one wheel per macOS generation
and `uv` picks the newest the build host can run, so the build machine's macOS becomes the
minimum every user needs. Building on a newer machine silently excludes older ones.

[uv]: https://docs.astral.sh/uv/
[rustup]: https://rustup.rs/
[maturin]: https://www.maturin.rs/
[vhs]: https://github.com/charmbracelet/vhs

[Back to the README](../README.md)
