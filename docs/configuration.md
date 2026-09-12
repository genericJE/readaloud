# Configuration

What lives in `~/.readaloud.conf`: what takes precedence over what, every setting the file
holds, and how readaloud reports a problem with it.

Most flags can also be preferences in `~/.readaloud.conf`; the ones that pick the input or
the output (`-f`, `-md`, `--start`, `--save`) cannot. A `[pronunciations]` section in the
same file tells readaloud how to say particular words (see
[Pronunciations](pronunciations.md)). The first run writes a template with every setting
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
`--save`, `--write-config` and the `--list-*` flags, and in the
[status bar](reading.md) for the reader, because a `print()` would land on top of the
curses screen.

```
PLAY     af_heart  1.00x  chunk 1/12  follow on  |  config: speed: 'maybe' is not a number; using 1.0
```

## How far ahead follow mode scrolls

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

`c` uses this same placement, which is what distinguishes it from `F`. Both keys are
listed in [Reading](reading.md).

[Back to the README](../README.md)
