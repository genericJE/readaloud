# readaloud

A terminal "Read Aloud" for anything you can pipe into it.

`readaloud` chunks text into sentences, speaks it with [Kokoro-82M][kokoro] running
locally on the Apple Neural Engine via [mlx-audio][mlx-audio], highlights the word you
are hearing, and lets you move around the document with `less` keys while it keeps
talking. Click any word to jump playback there.

Nothing leaves the machine — the model runs locally.

```
mdcat --ansi notes.md | readaloud
```

```
PLAY     af_heart  1.15x  chunk 7/26  follow on                            42%
```

## What it does

- **Reads styled terminal output.** Piped ANSI (`mdcat --ansi`, `bat`, `glow`) keeps its
  colours, bold, italics and OSC-8 hyperlinks on screen; raw Markdown gets its syntax
  characters stripped so `##` and `**` are not spoken out loud.
- **Word-level highlighting.** misaki's per-token timestamps are aligned back onto the
  original characters, so the reverse-video highlight tracks the audio to within a few
  milliseconds rather than being interpolated.
- **Never blocks.** A background thread loads the model and keeps the current chunk plus
  a couple more synthesized in a bounded cache. The document is on screen and scrollable
  before the voice has finished loading.
- **`less` navigation that is independent of playback.** Scroll wherever you like;
  playback carries on. `F` or `c` snaps the view back to the spoken word.
- **Survives the awkward cases**: terminal resize, empty input, input with nothing
  speakable, a chunk whose synthesis fails, and `q` during synthesis.

## Requirements

- macOS on Apple silicon (arm64), Python 3.12.
- An audio output device.
- [`uv`][uv]. Everything — including the espeak fallback — installs through `uv`;
  there is no Homebrew step and no `brew install espeak-ng`.

## Install

```bash
git clone <this repo> readaloud
cd readaloud
uv tool install --editable .
```

That puts `readaloud` on your `PATH`. To run it from a checkout without installing:

```bash
uv run readaloud -f notes.md
```

The first run downloads `mlx-community/Kokoro-82M-4bit` (~90 MB) into the HuggingFace
cache; later runs load it in a few seconds.

## Usage

```bash
# the primary path: rendered markdown, colours and all
mdcat --ansi notes.md | readaloud

# a file directly (raw markdown is cleaned up before it is spoken)
readaloud -f README.md

# a string
readaloud "the quick brown fox jumps over the lazy dog"

# any other producer
git log --oneline -20 | readaloud
pbpaste | readaloud

# a different voice, a bit faster, starting part-way in
readaloud -f notes.md --voice bf_emma --speed 1.2 --start 5

# render the whole document to a file and exit -- no TUI, no terminal needed
readaloud -f notes.md --save notes.wav
```

Piping works even when stdout is redirected: `readaloud` reopens `/dev/tty` for both the
keyboard and the screen, so `mdcat --ansi notes.md | readaloud > log` draws the reader on
the terminal rather than into `log`.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `TEXT...` | | text to read, joined with spaces |
| `-f`, `--file FILE` | | read `FILE` instead of stdin (`-` means stdin) |
| `-v`, `--voice NAME` | `af_heart` | Kokoro voice (see below) |
| `-s`, `--speed X` | `1.0` | speech rate multiplier, 0.5–3.0 in the TUI |
| `--sentences N` | `4` | max sentences per chunk |
| `--chars N` | `380` | max characters per chunk |
| `--prefetch N` | `2` | chunks synthesized ahead of the current one |
| `--device DEV` | system default | output device: index, name substring, or `default` |
| `--start N` | `1` | begin playback at the Nth speakable chunk |
| `--save FILE.wav` | | render everything to a WAV and exit |
| `--lang CODE` | from the voice | Kokoro language code (`a`, `b`, `j`, …) |
| `--repo ID` | `mlx-community/Kokoro-82M-4bit` | model repo |
| `--no-color` | | render monochrome, ignoring the input's colours |
| `--list-voices` | | print the voices for the chosen language |
| `--list-devices` | | print the audio output devices |

## Keys

The viewport moves independently of playback. Most movement keys take a numeric prefix
(`10j`).

| Key | Action |
| --- | --- |
| `j` &middot; `↓` &middot; `Enter` | line down |
| `k` &middot; `↑` | line up |
| `d` &middot; `^D` | half page down |
| `u` &middot; `^U` | half page up |
| `f` &middot; `^F` &middot; `PgDn` | page down |
| `b` &middot; `^B` &middot; `PgUp` | page up |
| `g` &middot; `Home` | top of the document (`10g` → row 10) |
| `G` &middot; `End` | bottom of the document |
| mouse wheel | scroll three rows |
| `/` | search forward (regular expression, smart case) |
| `?` | search backward |
| `n` | repeat the search |
| `N` | repeat it the other way |
| `Esc` | close the search prompt / clear the highlights |
| `Space` | play / pause |
| `.` &middot; `→` | next chunk |
| `,` &middot; `←` | previous chunk |
| `]` | speed up (+0.1x) |
| `[` | slow down (−0.1x) |
| `F` | toggle follow-the-word auto-scroll |
| `c` | centre the view on the spoken word and turn follow back on |
| click a word | jump playback to that word |
| `^L` &middot; `^R` | force a full redraw |
| `q` | quit |

Any manual scroll — key or wheel — switches follow mode **off**, so you can read ahead
while it talks. `F` and `c` switch it back on.

Changing the speed re-synthesizes from the word you are on, so the cache is dropped and
there is a short pause before the audio resumes.

## Status bar

```
PLAY     af_heart  1.15x  chunk 7/26  follow on  |  synthesizing chunk 8...   42%
```

`LOADING` / `PLAY` / `PAUSE`, the voice, the speed, the current chunk out of the
speakable ones, whether follow mode is on, a transient message, and how far down the
document the viewport is (`ALL` / `END` / a percentage).

## Voices

`--list-voices` prints the ones your language code can use; the language is derived from
the first letter of the voice name unless you pass `--lang`.

| Language | Voices |
| --- | --- |
| American English (`a`) | `af_alloy` `af_aoede` `af_bella` `af_heart` `af_jessica` `af_kore` `af_nicole` `af_nova` `af_river` `af_sarah` `af_sky` `am_adam` `am_echo` `am_eric` `am_fenrir` `am_liam` `am_michael` `am_onyx` `am_puck` `am_santa` |
| British English (`b`) | `bf_alice` `bf_emma` `bf_isabella` `bf_lily` `bm_daniel` `bm_fable` `bm_george` `bm_lewis` |
| Spanish (`e`) | `ef_dora` `em_alex` `em_santa` |
| French (`f`) | `ff_siwis` |
| Hindi (`h`) | `hf_alpha` `hf_beta` `hm_omega` `hm_psi` |
| Italian (`i`) | `if_sara` `im_nicola` |
| Japanese (`j`) | `jf_alpha` `jf_gongitsune` `jf_nezumi` `jf_tebukuro` `jm_kumo` |
| Portuguese (`p`) | `pf_dora` `pm_alex` `pm_santa` |
| Mandarin (`z`) | `zf_xiaobei` `zf_xiaoni` `zf_xiaoxiao` `zf_xiaoyi` `zm_yunjian` `zm_yunxi` `zm_yunxia` `zm_yunyang` |

`af_heart` is the default and the best-behaved. Non-English languages need misaki's extra
language packs, which this project does not install by default.

## How it fits together

| Module | Job |
| --- | --- |
| `ansi.py` | escape-sequence parser → styled `Run`s; Markdown cleanup for non-ANSI input |
| `document.py` | words with character offsets, and the sentence/paragraph chunker |
| `speech.py` | Kokoro pipeline, and the token → word-slot timestamp alignment |
| `player.py` | one persistent PortAudio stream with a lock-free callback |
| `ui.py` | curses view: wrapping, lazy 256-colour pairs, hit-testing, status bar |
| `keys.py` | terminal input decoding (SGR mouse, CSI keys) and the keymap |
| `app.py` | the loop, the prefetch worker, and the playback state machine |

## Development

```bash
uv sync
uv run --with pytest python -m pytest -q      # pytest is not yet a project dependency
uv run readaloud -f README.md
```

`tests/test_integration.py` drives the real binary under a pseudo-terminal with a pipe on
stdin, which is the only way to exercise the curses path (`curses` cannot initialise
without a tty, and skipping the `/dev/tty` reopen fails *silently* — every `getch()` just
returns -1 forever). Tests marked `slow` need the model weights and an audio device; skip
them with `-m "not slow"`.

While the TUI is up, `stderr` is parked on a temp file — the HuggingFace fetch bar, a
`torch.jit.script` FutureWarning and phonemizer's "words count mismatch" all arrive after
curses owns the screen and would otherwise shred the display. It is replayed only if the
app crashes. Set `READALOUD_DEBUG=1` to also report a synthesis worker that was still busy
at exit (harmless: it is a daemon thread).

[kokoro]: https://huggingface.co/hexgrad/Kokoro-82M
[mlx-audio]: https://github.com/Blaizzy/mlx-audio
[uv]: https://docs.astral.sh/uv/
