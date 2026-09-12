# Reading with readaloud

The keys readaloud listens for, what the status bar shows, and how it takes over the
system play/pause button.

## Keys

### Reading

| Key | Action |
| --- | --- |
| `Space` | play / pause |
| `.` &middot; `→` | next chunk (in a table read with `-md`, the next cell) |
| `,` &middot; `←` | previous chunk (in a table read with `-md`, the previous cell) |
| `]` &middot; `[` | speed up / slow down (0.1x steps, 0.5x to 3.0x) |
| `F` | jump to the spoken word, centred, and follow it from there |
| `c` | jump to the spoken word and leave the view there, placed exactly where follow mode would (see [`follow_lead`](configuration.md)), with follow mode as it was |
| click a word | jump playback to that word |
| `q` | quit |

The two differ in what happens after the jump. `F` centres the spoken word and follows it
from there, so the view keeps up with the reading; pressing it again changes nothing,
since it is not a toggle. `c` only looks: it places the word as the next auto-scroll
would, so the view does not jump again a moment later, and it leaves follow mode exactly
as it was. Scrolling away is what stops the view following.

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
it talks. `F` switches it back on; `c` shows you where the reading is without switching
anything on.

Changing the speed re-synthesizes from the word you are on, so the cache is dropped and
there is a short pause before the audio resumes.

## Status bar

```
PLAY     af_heart  1.15x  chunk 7/26  follow on  |  synthesizing chunk 8...   42%
```

`LOADING` / `PLAY` / `PAUSE`, the voice, the speed, the current chunk out of the speakable
ones, whether follow mode is on, a transient message, and how far down the document the
viewport is (`ALL` / `END` / a percentage).

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

[pyobjc]: https://pyobjc.readthedocs.io/

[Back to the README](../README.md)
