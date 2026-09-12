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
  highlight on the cell even where it wraps beside its neighbours. In a table, ticks and
  crosses are read as yes and no, and arrows and key symbols by name. See
  [below](#markdown-with--md).
- **Reads styled terminal output.** Piped ANSI (`mdcat --ansi`, `bat`, `glow`) keeps its
  colours, bold, italics and OSC-8 hyperlinks on screen. Raw Markdown gets its syntax
  characters stripped so `##` and `**` are not spoken out loud.
- **Word-level highlighting.** misaki's per-token timestamps are aligned back onto the
  original characters, so the highlight tracks the audio to within a few milliseconds
  rather than being interpolated.
- **Never blocks.** A background thread loads the model and keeps the current chunk plus
  a couple more synthesized in a bounded cache. The document is on screen and scrollable
  before the voice has finished loading.
- **Navigation independent of playback.** Scroll wherever you like; playback carries on.
  `F` or `c` snaps the view back to the spoken word.
- **Follow mode reads ahead.** When the spoken word reaches the bottom of the screen the
  view jumps `follow_lead` rows further than it strictly has to, so the text about to be
  read sits near the middle instead of the last row.
- **The play/pause button on your headphones works.** readaloud claims the system "Now
  Playing" role, so the button pauses the reader instead of launching Apple Music. See
  [below](#the-system-playpause-button).
- **Your defaults live in `~/.readaloud.conf`.** Voice, speed, chunking, colours and the
  follow lead, in a commented INI file that the first run writes for you.
- **Says words the way you do.** A `[pronunciations]` section in the config file sets how
  a word or phrase is said, and comes filled in with the words Kokoro gets wrong: file
  types (`main.py` is "main dot pie"), `id` as "eye dee" rather than Freud's id, `C#`,
  `systemd`, `kubectl`, and what the punctuation of code is called. The highlight stays on
  the word as written, even on text that is no word without it, like the `''` of
  `"''" = empty string`. Delete a line to be rid of it. See [below](#pronunciations).
- **Survives the awkward cases:** terminal resize, empty input, input with nothing
  speakable, a chunk whose synthesis fails, and `q` during synthesis.

## Install

### Homebrew

```bash
brew install genericJE/tools/readaloud
```

Apple silicon, macOS 14 or later. The formula ships a self-contained bundle carrying its
own Python and every library it needs, so nothing is compiled and nothing is fetched from
PyPI. The Kokoro model weights (~300 MB) download on first use.

### From a checkout

Needs [`uv`][uv] and Apple silicon. `uv` supplies everything including the espeak
fallback, so there is no `brew install espeak-ng` step.

```bash
git clone https://github.com/genericJE/readaloud
cd readaloud
uv tool install --editable '.[mediakeys]'
```

The `[mediakeys]` extra is what makes the system play/pause button work; drop it for a
smaller install. To run without installing: `uv run readaloud -f notes.md`.

The first run downloads `mlx-community/Kokoro-82M-4bit` (~300 MB) into the HuggingFace
cache; later runs load it in a few seconds. Only the safetensors and voice packs are
fetched. The repo also ships a PyTorch checkpoint of the same weights that an MLX build
never opens, and skipping it halves the download.

### mdcat

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

### Markdown with `-md`

`-md` reads a Markdown file from its source. readaloud runs mdcat itself, shows the file
as `mdcat --ansi` draws it, headings, colours and tables included, and reads each table
one cell at a time.

```bash
readaloud -md notes.md
readaloud -md notes.md --save notes.wav
cat notes.md | readaloud -md
```

`-md` also works after a file that exists (`readaloud notes.md -md`), after `-f FILE`, and
after TEXT. Put TEXT before the flag, because `-md` takes the word that follows it as its
file.

mdcat draws a table without pipes, padding its columns with spaces and wrapping a long
cell onto extra lines beside its neighbours. Once it is rendered nothing says where one
cell ends and the next begins, so a piped table is read line by line, the words of
neighbouring cells mixed together. `-md` works from the source, where the cells are known:

- Tables are read row by row, left to right, header row included, one cell at a time. The
  highlight stays on the cell being read even where it wraps onto lines shared with the
  next column, and follow mode keeps the whole row in view. A row too tall to fit between
  the follow margins is followed word by word instead.
- Each cell is a chunk of its own: the chunk keys (`.` and `,`) step one cell, a click
  anywhere in a cell plays it from the word nearest the click, and `chunk N/M` and
  `--start` count cells. An empty cell is skipped, and so is a click on one.
- The silence Kokoro puts around each cell is trimmed, so cells follow each other with a
  short pause and rows with a longer one, in the reader and in a `--save` WAV alike.
  Prose keeps its usual pauses.
- A tick or a cross (✓, ✅, ✗, ❌ and friends) is read as "yes" or "no" wherever it
  appears in a cell, where Kokoro would otherwise say nothing, or "white heavy check
  mark". Arrows and key symbols (← → ↑ ↓ ⏎ ⇥ ⇧ ⌃ ⌥ ⌘ ⌫ ⎋) are read by name too, and a
  cell holding nothing but symbols is read by their names, so the Keys table below says
  "dot, right arrow" for its `.` · `→` row, and `⇧⌘` is "shift, command". A symbol-only
  cell with a symbol readaloud has no name for is skipped whole. A
  [pronunciation](#pronunciations) can change what a named symbol says, but it cannot make
  a skipped cell speak.
- Footnote numbers stay on screen and are read where they stand, and the footnotes
  themselves are read; the pipe removes the numbers and skips the footnotes. A badge, or
  any image inside a link, is read by its alt text without the `[1]` mdcat puts after it,
  and the numbered URL line mdcat adds for it is shown but not read.
- A table readaloud cannot map is read line by line, as it would be when piped, and a
  notice in the status bar says so (in full on stderr for `--save`).
- The layout is fixed at startup: the terminal's width, capped at 80 columns as mdcat
  itself does (`--save` always uses 80). Narrow the terminal afterwards and the lines soft
  wrap instead of being redrawn.

Without mdcat on your `PATH`, `-md` stops with a one-line hint and the rest of readaloud
works as before. Input already rendered with `mdcat --ansi` is refused, since there is no
source left to find the cells in.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `TEXT...` | | text to read, joined with spaces |
| `-f`, `--file FILE` | | read `FILE` instead of stdin (`-` means stdin) |
| `-md`, `--markdown [FILE]` | | read `FILE` (or the `-f` file, `TEXT` or stdin) as Markdown: drawn by mdcat, tables read one cell at a time (needs mdcat) |
| `-v`, `--voice NAME` | `af_heart` | Kokoro voice (see below) |
| `-s`, `--speed X` | `1.0` | speech rate multiplier, 0.5 to 3.0 in the TUI |
| `--sentences N` | `4` | max sentences per chunk |
| `--chars N` | `380` | max characters per chunk |
| `--prefetch N` | `2` | chunks synthesized ahead of the current one |
| `--device DEV` | system default | output device: index, name substring, or `default` |
| `--start N` | `1` | begin playback at the Nth speakable chunk |
| `--save FILE.wav` | | render everything to a WAV and exit |
| `--lang CODE` | from the voice | Kokoro language code (`a`, `b`, `j`, …) |
| `--repo ID` | `mlx-community/Kokoro-82M-4bit` | model repo |
| `--color` / `--no-color` | colour on | render the input's colours, or force monochrome |
| `--media-keys` / `--no-media-keys` | on | take over the system play/pause button, or leave it alone |
| `--config PATH` | `~/.readaloud.conf` | read defaults from `PATH` instead |
| `--no-config` | | ignore the config file; built-in defaults only |
| `--write-config` | | write a fresh commented template (overwriting) and exit |
| `--list-voices` | | print the voices for the chosen language |
| `--list-devices` | | print the audio output devices |

The "Default" column is the *built-in* default. Anything set in `~/.readaloud.conf` wins
over it, and an explicit flag wins over both.

## Keys

### Reading

| Key | Action |
| --- | --- |
| `Space` | play / pause |
| `.` &middot; `→` | next chunk (in a table read with `-md`, the next cell) |
| `,` &middot; `←` | previous chunk (in a table read with `-md`, the previous cell) |
| `]` &middot; `[` | speed up / slow down (0.1x steps, 0.5x to 3.0x) |
| `F` | toggle follow mode; switching it on centres the spoken word |
| `c` | jump to the spoken word and switch follow mode on, placing the view exactly where follow mode would (see `follow_lead`) |
| click a word | jump playback to that word |
| `q` | quit |

`F` and `c` both end up following. They differ in where they leave the view: `F` centres
the spoken word, `c` places it as the next auto-scroll would, so the view does not jump
again a moment later.

### Moving around

`less` keys, and they never disturb playback. Most take a numeric prefix (`10j`).

| Key | Action |
| --- | --- |
| `j` &middot; `↓` &middot; `Enter` | line down |
| `k` &middot; `↑` | line up |
| `d` &middot; `^D` | half page down |
| `u` &middot; `^U` | half page up |
| `f` &middot; `^F` &middot; `PgDn` | page down |
| `b` &middot; `^B` &middot; `PgUp` | page up |
| `g` &middot; `Home` | top of the document (`10g` goes to row 10) |
| `G` &middot; `End` | bottom of the document |
| mouse wheel | scroll three rows |
| `/` &middot; `?` | search forward / backward (regular expression, smart case) |
| `n` &middot; `N` | repeat the search, forwards / backwards |
| `Esc` | close the search prompt, clear the highlights |
| `^L` &middot; `^R` | force a full redraw |

Any manual scroll, key or wheel, switches follow mode **off** so you can read ahead while
it talks. `F` and `c` switch it back on.

Changing the speed re-synthesizes from the word you are on, so the cache is dropped and
there is a short pause before the audio resumes.

## Configuration

Most flags can also be preferences in `~/.readaloud.conf`; the ones that pick the input or
the output (`-f`, `-md`, `--start`, `--save`) cannot. A `[pronunciations]` section in the
same file tells readaloud how to say particular words (see
[Pronunciations](#pronunciations)). The first run writes a template with every setting
present but commented out, so an untouched file leaves every setting at its default, and
with the pronunciations below already in it. It says so on stderr once:

```
readaloud: created /Users/you/.readaloud.conf -- your defaults live there now
```

Precedence is `explicit flag > ~/.readaloud.conf > built-in default`, key by key rather
than file by file: `readaloud --speed 2.0` with `speed = 1.4` and `voice = bm_george` in
the file runs at 2.0x in `bm_george`. A flag counts as explicit even when it matches the
built-in default, so `--speed 1.0` really does mean 1.0 whatever the file says.

`--config PATH` reads a different file, `--no-config` ignores the file entirely, and
`--write-config` writes a fresh template over whatever is there.

```ini
[readaloud]
# Kokoro voice name.  --list-voices prints the ones your model repo has; the
# first letter picks the language (a=American English, b=British, ...).
voice = af_heart

# Speech rate multiplier.  Clamped to 0.5 - 3.0.
speed = 1.0

# Kokoro language code.  Leave empty to derive it from the voice name.
lang =

# HuggingFace model repo to load the voice from.
repo = mlx-community/Kokoro-82M-4bit

# Maximum sentences per spoken chunk (minimum 1).
sentences = 4

# Maximum characters per spoken chunk, whichever limit is hit first (minimum 1).
chars = 380

# Chunks to synthesize ahead of the one being spoken.  0 disables prefetching.
prefetch = 2

# Audio output device: an index, a substring of the name, or 'default'.
# Empty means the system default output device.  --list-devices lists them.
device =

# Render the input's colours.  false is the equivalent of --no-color.
# Accepts true/false, yes/no, on/off, 1/0.
color = true

# Extra rows follow mode scrolls past the strict minimum (see below).
follow_lead = 20

# Rows kept between the spoken word and the top/bottom edge before follow
# mode scrolls at all.
follow_margin = 2

# Take over the system play/pause button.  Needs the optional [mediakeys]
# extra; without it this setting does nothing at all.
media_keys = true

[pronunciations]
# How to say a word or phrase: the text as written on the left of the "=",
# and how to say it on the right.  These come with readaloud; see below.
id = ID
.py = dot pie
C# = C sharp
```

The file is meant to be hand-edited and is read forgivingly. A missing file is not an
error, a value that makes no sense keeps its default, one that is out of range is
clamped, and the rest of the file still applies. Anything odd is reported: on stderr for
`--save`, `--write-config` and the `--list-*` flags, and in the status bar for the reader,
because a `print()` would land on top of the curses screen.

```
PLAY     af_heart  1.00x  chunk 1/12  follow on  |  config: speed: 'maybe' is not a number; using 1.0
```

### Pronunciations

Kokoro does not always say a word the way you would. It reads the `id` in `user.id` to
rhyme with "kid" and runs `ASP.NET` together into "aspnet". The `[pronunciations]` section
fixes that: each line holds the text as written, an `=`, and how to say it, spelled the
way it sounds.

```ini
[pronunciations]
# text as written = how to say it
id = ID                    # user.id, user_id, userId, the id; not idle or grid
ids = IDs                  # other forms of a word are entries of their own
kubectl = cube control     # the highlight stays on kubectl while it is said
GIF = jif                  # has a capital: matches GIF only, never gif
New York City = NYC        # a phrase: any spacing, one line break too
.NET = dot net             # ASP.NET says "ASP dot net"
C# = C sharp
std::vector = standard vector
!= = not equal             # no quotes needed: the separator has spaces round it
✓ = check                  # a tick in a -md table says "check", not "yes"
macOS = macOS              # said as written, which keeps "OS" below off it
OS = O S
"#include" = hash include  # quote text that starts with # or ; or a quote
```

A fresh `~/.readaloud.conf` already holds about ninety of these, each one a word Kokoro
was measured saying wrongly: the common file types (`main.py` is "main dot pie",
`config.yml` is "config dot yaml", `README.md` keeps its name instead of being spelled
out), `id` and `ids`, the quotes that stand for nothing (`''` is "empty string", `"""` is
"triple quote"), languages and tools (`C#`, `.NET`, `json`, `YAML`, `systemd`,
`journalctl`, `kubectl`, `k8s`, `redis`, `postgresql`, `PyPI`), and what the punctuation
of code is called (`!=`, `->`, `=>`, `::`, `&&`, `||`). They are ordinary lines in your
file: delete one to be rid of it, or edit it to say it your way. Anything readaloud
already says correctly is deliberately absent, so `SQL` stays "sequel", `nginx` stays
"engine x" and `C++` stays "C plus plus".

How the text on the left is found:

- **It is text, not a pattern.** `C#`, `std::vector` and `.NET` mean exactly those
  characters. The two sides are split at the first `=` with a space on each side, so `!=`
  and `==` need no quotes; a line without one splits at its first `=`, so `id=ID` works
  too.
- **A word matches on its own and as part of a name,** never inside a longer word: `id`
  matches `the id`, `foo.id`, `user_id`, `userId` and `idToken`, but not `idle`, `grid`,
  `ids` or `id2`. Spaces and punctuation end a word, and so does a lowercase letter
  followed by a capital. Text that starts or ends with a symbol needs no such break on
  that side: `.NET` matches in `ASP.NET`.
- **Smart case,** as in search. Lowercase text matches any case, so `id` covers `Id` and
  `ID` as well. Text with a capital matches that case only, so `GIF` leaves `gif` alone.
- **Phrases.** A space matches any run of spaces and tabs with at most one line break in
  it, so `New York City` still matches where a paragraph wraps between the words, but
  never across a blank line. Punctuation inside a phrase must be there as written.
- **The longest match wins.** Reading goes left to right, and where several entries match
  at the same place the longest one wins: with `New York = the big apple` as well, "New
  York City" still says "NYC". A tie goes to the text with a capital, then to the later
  line. Reading carries on after the match, so a respelling is never matched again and
  `id = id card` is safe.

Put quotes, single or double, round text that starts with `#`, `;` or a quote, or that has
an `=` with spaces round it: `"#include" = hash include`, `"a = b" = c`. Without the
quotes, `#include = hash include` is a comment and does nothing. A `#` or `;` after a
space starts a comment, so a pronunciation holding one needs quotes too
(`hash = "number # sign"`), while `C# = C sharp` needs none. There are no escapes.

Pronunciations apply wherever readaloud speaks: prose, code blocks, piped output and the
cells of a `-md` table, in the reader and in a `--save` WAV alike. Only what is said
changes. The screen shows the text as written, search finds it as written, and the
highlight stays on the written word while its respelling is said: `kubectl` stays lit
through "cube control". Control Center and the lock screen show what is said, so
`kubectl get pods` is titled "cube control get pods" there.

Text with no letter or digit in it is not a word readaloud would ever light on its own,
but a pronunciation of it gets one: with `"''" = empty string`, the `''` of "Pass '' to
skip the field." lights while "empty string" is said, and clicking it starts there, just
like a word. The same goes for `✓ = check` in a sentence and `-> = to` in a code block.
Nothing else moves: the same text is spoken either way, the chunk count in the status bar
and what `--start` counts stay as they are, and a line or cell that holds nothing but such
text (a `''` on a line of its own) is still skipped, as a line with no words always is.
Two kinds stay unlit while still being said: an entry whose text holds a space
(`. . = stop stop`), since a word never spans one, and, in a table cell, text the render
wrapped onto a line of its own.

A `-md` table says its ticks and crosses as "yes" and "no", and its arrows and keys by
name (see [above](#markdown-with--md)). Those names are not text an entry can match, so
`yes = yep` changes a written "yes" but no tick, and `right arrow = next` leaves every `→`
alone. An entry for the symbol itself replaces its name: with `✓ = check`, a `✓` cell says
"check" and `✓ (partial)` says "check (partial)". Each symbol is an entry of its own, so
that leaves `✅` saying "yes", and an entry reaches a named symbol only when its text is
that one symbol: `⌘C = copy` leaves a `⌘C` cell saying "command C", while `⌘ = cmd` makes
it "cmd C". Outside a table Kokoro says nothing at all for a `✓` beside other words, and
`✓ = check` makes it say "check" there too, lighting the tick while it does. A line or
cell holding nothing but symbols readaloud has no name for is still skipped, so
`★ = star` does nothing for a `★★★★` rating.

Some tips:

- Capitals usually get a word spelled out, which is why `id = ID` works; `API` and `URL`
  come out as letters too. A few are still said as words (`SQL` is "sequel"), and spaces
  between the letters spell those out: `SQL = S Q L`.
- Other forms of a word are entries of their own. `id` does not match `ids`, so add
  `ids = IDs`.
- An entry that says its text as written guards it: `macOS = macOS` keeps `OS = O S` off
  the "OS" in "macOS". A guard changes nothing in any case it matches, so a lowercase
  `macos = macos` works too.
- Try an entry before relying on it. `readaloud 'user.id'` reads just that with your
  pronunciations, and `readaloud --no-config 'user.id'` reads it without them.

A pronunciation is text for Kokoro to read, never phonemes: `id = /aɪdiː/` and misaki's
`[id](/ˈaɪdi/)` links are refused with a warning, so write the word the way it sounds. Nor
can a pronunciation make readaloud skip a word, because it needs at least one letter or
digit.

A line readaloud cannot use is reported with its line number, like any other problem in
the file, and skipped. It costs only itself: every other pronunciation and every setting
still applies.

```
readaloud: /Users/you/.readaloud.conf: [pronunciations] line 99: 'kubectl' has no "="; write it as: text = how to say it
```

The same text on two lines is reported too, and the later line wins; `id`, `Id` and `ID`
are three different entries. An existing `~/.readaloud.conf` is never touched, so a file
written by an older readaloud has neither the shipped entries nor the `[pronunciations]`
line: add the line above your own entries, since an entry left under `[readaloud]` is
ignored with a warning that it belongs under `[pronunciations]`. To take the shipped ones,
copy them from a fresh template (`readaloud --write-config` writes one, discarding your
edits, so save the file first if you have any). A fresh template ends
with `[pronunciations]`, so a setting added at the bottom of it lands there: `speed = 1.5`
is then reported as a setting to move under `[readaloud]`, not taken for a word (quote it,
`"speed" = spead`, if you do mean the word). A misspelled heading such as
`[pronunciation]` is ignored with a warning
that names the section it probably meant, and past five problems the rest are counted
rather than listed.

### How far ahead follow mode scrolls

Follow mode used to do the smallest scroll that kept the spoken word on screen, which
pins the reading position to the bottom row: you can see everything you have already
heard and nothing you are about to. `follow_lead` scrolls that many rows *further* at the
moment a scroll is needed, so the view advances in stable jumps and the next chunk lands
around the middle of the screen.

On a 30-row terminal with the default `follow_lead = 20`, the spoken word lands on about
row 7 and roughly 23 rows of unread text stay visible below it. It then drifts down to
the bottom margin and the view jumps again. `follow_lead = 0` restores the old
creep-one-row-at-a-time behaviour. The lead never scrolls so far that the spoken word
would leave the screen, and it applies only when scrolling *down*: moving back up to a
word behind the viewport is unchanged.

`c` uses this same placement, which is what distinguishes it from `F`.

## The system play/pause button

The play/pause button on a Bluetooth headset, on the keyboard's F8, and in Control Center
does not reach the terminal at all. macOS routes it through MediaRemote to whichever
process holds the "Now Playing" role, and when nothing holds it the system **launches
Apple Music**. Claiming that role is the only way to get the button, and it is what stops
Music from stealing it.

readaloud claims it at startup, says `media keys on` in the status bar, and hands it back
when you quit. While it holds it:

| Button | Does |
| --- | --- |
| play / pause | pause or resume, exactly like `Space` |
| next track | next chunk, like the `→` key |
| previous track | previous chunk, like the `←` key |

Control Center and the lock screen show the chunk being read (a paragraph, or a table cell
with `-md`) as the track title and the document's name as the artist.

This needs [PyObjC][pyobjc]. A Homebrew install already bundles it. In a checkout it is an
optional extra, so the reader itself stays a small install:

```bash
uv tool install --editable '.[mediakeys]'   # installing readaloud
uv sync --extra mediakeys                   # working in a checkout
```

Without it the setting is inert: nothing is claimed, nothing is printed, and the spacebar
carries on as before. Turn it off with `--no-media-keys`, or `media_keys = false`, if you
would rather the button kept going to another player.

## Status bar

```
PLAY     af_heart  1.15x  chunk 7/26  follow on  |  synthesizing chunk 8...   42%
```

`LOADING` / `PLAY` / `PAUSE`, the voice, the speed, the current chunk out of the speakable
ones, whether follow mode is on, a transient message, and how far down the document the
viewport is (`ALL` / `END` / a percentage).

## Voices

`--list-voices` prints the ones your language code can use. The language is derived from
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

## Development

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

### Cutting a release

Bump `version` in `pyproject.toml` and `__version__` in `src/readaloud/__init__.py`, run
`uv lock`, commit, then push a `vX.Y.Z` tag. The `release` workflow builds the bundle on a
`macos-14` runner and attaches it. Then update three lines in the tap's
`Formula/readaloud.rb`: `url`, `version`, `sha256`.

The runner's macOS version is load-bearing. mlx publishes one wheel per macOS generation
and `uv` picks the newest the build host can run, so the build machine's macOS becomes the
minimum every user needs. Building on a newer machine silently excludes older ones.

## License

MIT.

If anything here ends up being useful to you and you feel like saying thanks, my PayPal is https://paypal.me/genericJE. Truly no expectation either way, just leaving the option here in case.

[kokoro]: https://huggingface.co/hexgrad/Kokoro-82M
[mdcat]: https://github.com/BIRSAx2/mdcat
[mlx-audio]: https://github.com/Blaizzy/mlx-audio
[pyobjc]: https://pyobjc.readthedocs.io/
[uv]: https://docs.astral.sh/uv/
