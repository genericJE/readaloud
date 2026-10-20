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
- **Reads styled terminal output.** Piped ANSI (`bat`, `git log --color`) keeps its
  colours, bold, italics and OSC-8 hyperlinks on screen. For Markdown, use `-md` above:
  piped from a renderer, a table is read line by line instead of cell by cell. Raw Markdown gets its syntax
  characters stripped so `##` and `**` are not spoken out loud.
- **Word-level highlighting.** misaki's per-token timestamps are aligned back onto the
  original characters, so the highlight tracks the audio to within a few milliseconds
  rather than being interpolated.
- **Says words the way you do.** A `[pronunciations]` section in the config file sets how
  a word or phrase is said, and comes filled in with the words Kokoro gets wrong:
  `main.py` is "main dot pie", and `id` is "eye dee" rather than Freud's id. A file
  reference is read as one, so `settings.py:281-283` is "settings dot pie lines 281 to
  283", and a port stays a port. See [Pronunciations](docs/pronunciations.md).
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

[mdcat][mdcat] is optional, and neither install brings it. `-md` runs it; everything else
works without it.

```bash
brew install mdcat
```

## Usage

```bash
# the primary path for Markdown: drawn by mdcat, tables read one cell at a time
readaloud -md notes.md

# a file, no mdcat needed (most Markdown syntax is stripped before it is spoken, unless `markdown = no`)
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

Read Markdown with `-md` rather than piping `mdcat --ansi notes.md | readaloud`. Piped
output arrives as finished lines, so readaloud cannot tell a table's cells apart: it reads
the table line by line, straight across its columns, and a cell that wraps gets mixed up
with its neighbours ("writes the joined parser and the in tests March"). `-md` runs mdcat
itself and reads every cell on its own, in order.

Piping works even when stdout is redirected. `readaloud` reopens `/dev/tty` for both the
keyboard and the screen, so `git log --oneline -20 | readaloud > log` draws the reader on
the terminal rather than into `log`.

`readaloud --help` lists every flag, and [Options](docs/options.md) explains them.

## Keys

| Key | Action |
| --- | --- |
| `Space` | play / pause |
| `.` &middot; `→` | next chunk (in a table read with `-md`, the next cell) |
| `,` &middot; `←` | previous chunk (in a table read with `-md`, the previous cell) |
| `]` &middot; `[` | speed up / slow down (0.1x steps, 0.5x to 3.0x) |
| `F` | jump to the spoken word and follow it from there |
| `c` | jump to the spoken word and stay put, following or not as before |
| click a word | jump playback to that word |
| `q` | quit |

Moving around uses `less` keys (`j` `k` `d` `u` `f` `b` `g` `G`, and `/` to search) and
never disturbs playback. See [Reading](docs/reading.md) for those, the status bar, follow
mode and the headphone button.

## Configuration

The first run writes `~/.readaloud.conf`, a copy of the file readaloud ships: every setting
at its default, the shapes that say a file with its line or a URL with its port, and the
words Kokoro says wrongly, as ordinary lines to edit or delete. A shortened example, with
one setting changed:

```ini
[config]
# settings; docs/configuration.md lists them all
speed = 1.2

[parts]
# named pieces of pattern, used as %{name} by the shapes and by later parts
ext = [A-Za-z]\w*
file = \S+\.%{ext}

[shapes]
# /pattern/ = how to say it, $name being a named group; first fit wins
/(?<file>%{file}):(?<from>\d+)-(?<to>\d+)/ = $file lines $from to $to
/(?<file>%{file}):(?<line>\d+)/ = $file line $line

[pronunciations]
# text = how to say it; lowercase matches any case, a capital only that case

# names, and quotes that stand for nothing
id = ID
"''" = empty string

# file names and extensions
README.md = readme dot M D
.py = dot pie

# top-level domains
.io = dot I O

# what the punctuation of code is called
!= = not equals
```

With these lines, `settings.py:281-283` is said "settings dot pie lines 281 to 283",
`user.id` ends in "eye dee" rather than Freud's id, and `a != b` is "a not equals b". The whole file, with every
part, shape and pronunciation, is in [Configuration](docs/configuration.md#the-file-a-first-run-writes).

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
