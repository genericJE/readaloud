# Development

Installing from a checkout, how the modules fit together, running the tests, and cutting
a release.

## How it fits together

| Module | Job |
| --- | --- |
| `cli.py` | arguments, config precedence, reading the input, `--save`, and handing `-md` input to `markdown.py` |
| `ansi.py` | escape-sequence parser to styled `Run`s; Markdown cleanup for non-ANSI input |
| `document.py` | words with character offsets, the sentence/paragraph chunker, and table cells |
| `pronounce.py` | `[pronunciations]`: finding the text in a chunk and respelling it, so every word keeps its highlight |
| `markdown.py` | `-md`: runs mdcat and maps each rendered table's cells back to the source |
| `width.py` | how many terminal cells a character takes (wide CJK and emoji take two) |
| `speech.py` | Kokoro pipeline, the token to word-slot timestamp alignment, and trimming a cell's silence |
| `player.py` | one persistent PortAudio stream with a lock-free callback |
| `ui.py` | curses view: wrapping, lazy 256-colour pairs, hit-testing, status bar |
| `keys.py` | terminal input decoding (SGR mouse, CSI keys) and the keymap |
| `app.py` | the loop, the prefetch worker, and the playback state machine |
| `config.py` | `~/.readaloud.conf`: parsing the settings and the `[pronunciations]` lines, clamping, and the generated template |
| `mediakeys.py` | the macOS "Now Playing" role, so the headphone button reaches us |

## From a checkout

`uv tool install --editable '.[mediakeys]'` needs [`uv`][uv] and Apple silicon. `uv`
supplies everything including the espeak fallback, so there is no `brew install espeak-ng`
step.

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

## Cutting a release

Bump `version` in `pyproject.toml` and `__version__` in `src/readaloud/__init__.py`, run
`uv lock`, commit, then push a `vX.Y.Z` tag. The `release` workflow builds the bundle on a
`macos-14` runner and attaches it. Then update three lines in the tap's
`Formula/readaloud.rb`: `url`, `version`, `sha256`.

The runner's macOS version is load-bearing. mlx publishes one wheel per macOS generation
and `uv` picks the newest the build host can run, so the build machine's macOS becomes the
minimum every user needs. Building on a newer machine silently excludes older ones.

[uv]: https://docs.astral.sh/uv/

[Back to the README](../README.md)
