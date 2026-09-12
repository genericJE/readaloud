# readaloud

A terminal "Read Aloud" for Markdown files and anything you can pipe into it.

`readaloud` chunks text into sentences, speaks it with [Kokoro-82M][kokoro] running
locally on the Apple silicon GPU via [mlx-audio][mlx-audio], and highlights the word
you are hearing. Move around with `less` keys while it keeps talking, or click any word
to jump playback there. Nothing leaves the machine.

```
readaloud -md notes.md
```


https://github.com/user-attachments/assets/dc81e313-fac4-4525-bb81-b09d70b7059e


## What it does

- **Reads Markdown as mdcat draws it.** `readaloud -md notes.md` shows the file the way
  [mdcat][mdcat] draws it and reads its tables one cell at a time, row by row, with the
  highlight on the cell even where it wraps beside its neighbours. See
  [Markdown with `-md`](docs/markdown.md).
- **Reads styled terminal output.** Piped ANSI (`mdcat --ansi`, `bat`, `glow`) keeps its
  colours, bold, italics and OSC-8 hyperlinks on screen. Raw Markdown gets its syntax
  characters stripped so `##` and `**` are not spoken out loud.
- **Word-level highlighting.** misaki's per-token timestamps are aligned back onto the
  original characters, so the highlight tracks the audio to within a few milliseconds
  rather than being interpolated.
- **Says words the way you do.** A `[pronunciations]` section in the config file sets how
  a word or phrase is said, and comes filled in with the words Kokoro gets wrong:
  `main.py` is "main dot pie", and `id` is "eye dee" rather than Freud's id. See
  [Pronunciations](docs/pronunciations.md).
- **Never blocks.** A background thread loads the model and keeps the current chunk plus
  a couple more synthesized in a bounded cache. The document is on screen and scrollable
  before the voice has finished loading, and playback carries on wherever you scroll.
- **The play/pause button on your headphones works.** readaloud claims the system "Now
  Playing" role, so the button pauses the reader instead of launching Apple Music.
- **Survives the awkward cases:** terminal resize, empty input, input with nothing
  speakable, a chunk whose synthesis fails, and `q` during synthesis.

## Install

```bash
brew install genericJE/tools/readaloud
```

Apple silicon, macOS 14 or later. The formula ships a self-contained bundle carrying its
own Python and every library it needs, so nothing is compiled and nothing is fetched from
PyPI. The Kokoro model weights (~300 MB) download on first use.

From a checkout instead, which needs [`uv`][uv]:

```bash
git clone https://github.com/genericJE/readaloud
cd readaloud
uv tool install --editable '.[mediakeys]'
```

The `[mediakeys]` extra is what makes the system play/pause button work; drop it for a
smaller install. To run without installing: `uv run readaloud -f notes.md`. See
[Development](docs/development.md) for what that pulls in.

[mdcat][mdcat] is optional, and neither install brings it. `-md` runs it, and the
`mdcat --ansi notes.md | readaloud` pipe needs it too; everything else works without it.

```bash
brew install mdcat
```

## Usage

```bash
# the primary path for Markdown: drawn by mdcat, tables read one cell at a time
readaloud -md notes.md

# anything already rendered, colours and all
mdcat --ansi notes.md | readaloud

# a file, no mdcat needed (most Markdown syntax is stripped before it is spoken)
readaloud -f README.md

# a string
readaloud "the quick brown fox jumps over the lazy dog"

# any other producer
git log --oneline -20 | readaloud
pbpaste | readaloud

# a different voice, a bit faster, starting part-way in
readaloud -f notes.md --voice bf_emma --speed 1.2 --start 5

# render to a file and exit: no TUI, no terminal needed
readaloud -f notes.md --save notes.wav
```

Piping works even when stdout is redirected. `readaloud` reopens `/dev/tty` for both the
keyboard and the screen, so `mdcat --ansi notes.md | readaloud > log` draws the reader on
the terminal rather than into `log`.

`readaloud --help` lists every flag, and [Options](docs/options.md) explains them.

## Keys

| Key | Action |
| --- | --- |
| `Space` | play / pause |
| `.` &middot; `→` | next chunk (in a table read with `-md`, the next cell) |
| `,` &middot; `←` | previous chunk (in a table read with `-md`, the previous cell) |
| `]` &middot; `[` | speed up / slow down (0.1x steps, 0.5x to 3.0x) |
| `F` &middot; `c` | follow the spoken word again after scrolling away |
| click a word | jump playback to that word |
| `q` | quit |

Moving around uses `less` keys (`j` `k` `d` `u` `f` `b` `g` `G`, and `/` to search) and
never disturbs playback. See [Reading](docs/reading.md) for those, the status bar, follow
mode and the headphone button.

## Configuration

The first run writes `~/.readaloud.conf`: every setting commented out at its default, and
a `[pronunciations]` section already filled in with the words Kokoro says wrongly.

```ini
[readaloud]
voice = af_heart
speed = 1.0

[pronunciations]
id = ID
.py = dot pie
C# = C sharp
```

Precedence is `explicit flag > ~/.readaloud.conf > built-in default`, key by key. The file
is read forgivingly: a value that makes no sense keeps its default, one that is out of
range is clamped, a pronunciation line that makes no sense costs only itself, and anything
odd is reported without stopping the reader.

## Docs

| Page | What is in it |
| --- | --- |
| [Reading](docs/reading.md) | the keys, the status bar, follow mode, the headphone button |
| [Markdown with `-md`](docs/markdown.md) | how a rendered table is read one cell at a time |
| [Pronunciations](docs/pronunciations.md) | saying a word or phrase your way |
| [Configuration](docs/configuration.md) | `~/.readaloud.conf`, setting by setting |
| [Options](docs/options.md) | every flag, and the voices |
| [Development](docs/development.md) | how the modules fit together, the tests, releases |

## License

MIT.

If anything here ends up being useful to you and you feel like saying thanks, my PayPal is https://paypal.me/genericJE. Truly no expectation either way, just leaving the option here in case.

[kokoro]: https://huggingface.co/hexgrad/Kokoro-82M
[mdcat]: https://github.com/BIRSAx2/mdcat
[mlx-audio]: https://github.com/Blaizzy/mlx-audio
[uv]: https://docs.astral.sh/uv/
