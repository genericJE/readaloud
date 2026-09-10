# readaloud — module contracts (authoritative)

A terminal "Read Aloud" clone: chunk text, speak it with Kokoro (mlx-audio) in a realistic
voice, highlight the current word, navigate the viewport with `less` keys, click any chunk
to jump playback there.

Target: macOS arm64 (Apple M1), Python 3.12, all deps via `uv` ONLY (never Homebrew).

## Verified environment facts (already measured — do not re-litigate)

- `mlx_audio.tts.models.kokoro.KokoroPipeline` + `mlx_audio.tts.utils.load_model`.
- Model repo: `mlx-community/Kokoro-82M-4bit`. Warm load 0.4s; pipeline construct 3.2s (spaCy).
- `pipe(text, voice="af_heart", speed=1.0)` yields `Result` objects with:
  `.audio` (mx.array, shape **(1, N)** — must `.reshape(-1)`), sample rate **24000**,
  `.tokens` -> list of misaki `MToken` with `.text`, `.whitespace`, `.start_ts`, `.end_ts`,
  `.phonemes`. Timestamps are **seconds, relative to the start of that Result's audio**.
- `MToken.start_ts`/`end_ts` **can be `None`** (observed on a bare `$`). Handle it.
- Punctuation arrives as its own token (e.g. `'.'`, `';'`).
- Warm RTF ~0.13 (7.7x faster than real time) — prefetch stays ahead easily.
- `sounddevice.OutputStream(samplerate=24000, channels=1, blocksize=512, latency='low')`
  works; measured output latency 0.036s. Counting frames in the callback gives a
  sample-accurate play position.
- espeak fallback for out-of-dictionary words works via the pip package `espeakng-loader`
  (NO `brew install espeak-ng` — that violates a standing user rule).
- `KokoroPipeline.__call__` default `split_pattern=r"\n+"` splits input, and long input is
  further split internally at 510 phonemes. Pass `split_pattern=None` and keep chunks small,
  but STILL handle the generator yielding more than one Result per call.

## Input formats

1. `readaloud -f FILE`, `readaloud "some text"`, or piped stdin.
2. Piped stdin is the primary path, notably `mdcat --ansi doc.md | readaloud`.
   mdcat emits:
   - SGR: `ESC[0m`, `ESC[1m` bold, `ESC[3m` italic, `ESC[4m`, `ESC[7m`,
     fg 30-37 / 90-97, bg 40-47 / 100-107, plus `ESC[38;5;Nm` / `ESC[38;2;R;G;Bm`.
   - OSC 8 hyperlinks: `ESC]8;;URL ESC\ link text ESC]8;; ESC\`  (also `BEL`-terminated form).
   - Bullets as literal `•`, quote bars as `│`.
   - When NOT a tty it degrades to plain text with `text[1]` markers and a trailing
     `[1]: https://...` reference block.
3. Raw markdown (no ANSI) should be lightly cleaned so syntax is not spoken.

Because stdin is a pipe, the TUI MUST reopen the terminal: read stdin fully, then
`os.dup2(os.open("/dev/tty", os.O_RDONLY), 0)` before `curses.initscr()`.

## Modules and exact contracts

### `readaloud/ansi.py`
```python
@dataclass(frozen=True)
class Style:
    fg: int | None = None        # 0-255 palette index, or None = default
    bg: int | None = None
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    href: str | None = None      # from OSC 8

@dataclass
class Run:
    text: str                    # NO escape sequences, NO newlines
    style: Style

def parse(data: str) -> list[list[Run]]:
    """Split into logical lines; each line is a list of styled runs.
    Strips every escape sequence; keeps hyperlink *text* and records its URL in Style.href.
    Expands tabs to 4 spaces. Strips a trailing '\\r'."""

def has_ansi(data: str) -> bool: ...
def strip_markdown(lines: list[list[Run]]) -> list[list[Run]]:
    """Only used when has_ansi() is False: drop #/*/_/`/> syntax chars, unwrap
    [text](url) to text, keeping run structure."""
```

### `readaloud/document.py`
```python
@dataclass
class Word:
    text: str            # what gets spoken/highlighted, e.g. "Kubernetes"
    line: int            # index into Document.lines
    start: int           # char offset within that logical line (in Run-flattened chars)
    end: int             # exclusive
    idx: int             # global word index

@dataclass
class Chunk:
    idx: int
    words: list[int]     # global word indices, contiguous
    text: str            # exact plain text handed to the TTS
    # offsets[i] = char offset of words[i] inside `text`
    offsets: list[int]
    line_start: int
    line_end: int

class Document:
    lines: list[list[Run]]
    plain: list[str]                 # plain text per logical line
    words: list[Word]
    chunks: list[Chunk]
    def word_at(self, line: int, col: int) -> int | None: ...
    def chunk_of_word(self, widx: int) -> int: ...
```
Chunking rule: break on blank lines (paragraphs) first; then split any paragraph longer
than `max_sentences` (default 4) sentences or `max_chars` (default 380) at sentence
boundaries; never split mid-word. A fenced/indented code block or a table row group is one
chunk. Skip chunks that contain no speakable words (pure rules/whitespace) — they stay
visible but are never selected for playback.

### `readaloud/speech.py`
```python
@dataclass
class Timed:
    word_slot: int       # index into Chunk.words
    start: float         # seconds within this chunk's audio
    end: float

@dataclass
class Spoken:
    chunk_idx: int
    audio: np.ndarray    # float32, mono, 24000 Hz, shape (N,)
    timings: list[Timed] # sorted by start; monotonic; gaps allowed
    sample_rate: int = 24000

class Engine:
    def __init__(self, voice: str = "af_heart", speed: float = 1.0,
                 lang_code: str = "a", repo_id: str = "mlx-community/Kokoro-82M-4bit"): ...
    def load(self) -> None:          # blocking, ~3.5s; safe to call once from a worker thread
    def synth(self, chunk: Chunk) -> Spoken: ...
```
**Token alignment (the hard part).** misaki tokens preserve the original graphemes in
order. Align by walking a cursor through `chunk.text`: for each token, `find(token.text,
cursor)`; on success advance the cursor; on failure skip that token. Then map each matched
character span back to a word slot via `chunk.offsets`. A word with no timestamp inherits
`start` from the previous timed word's `end`. Concatenate multiple Results by offsetting
their timestamps by the cumulative audio duration so far.

### `readaloud/player.py`
Single persistent `sounddevice.OutputStream`; a callback drains the active `Spoken`.
```python
class Player:
    def play(self, spoken: Spoken, start_time: float = 0.0) -> None  # replaces current
    def pause(self) -> None
    def resume(self) -> None
    def toggle(self) -> bool
    def stop(self) -> None
    @property
    def position(self) -> float   # seconds into the active Spoken, latency-compensated
    @property
    def playing(self) -> bool
    @property
    def finished(self) -> bool    # active Spoken drained
    def set_on_finished(self, cb) -> None   # called FROM the audio thread: must be cheap
```
Never block or allocate in the audio callback. `position` must subtract
`stream.latency` (clamped at >= 0) so the highlight matches what is actually audible.

### `readaloud/ui.py` (curses)
- Wraps styled logical lines to terminal width into display rows; keeps a
  `row -> (line, col_start)` map so hit-testing a mouse click yields `(line, col)`.
- Renders each Run with the right curses attrs. 256-colour pairs allocated lazily via
  `curses.init_pair`, `curses.use_default_colors()`; degrade gracefully when
  `curses.COLORS < 256` and when `A_ITALIC` is unavailable.
- Highlight: the current word gets `A_REVERSE` (or a configurable pair); the current chunk
  gets a subtle background so you can see where you are.
- Status bar: voice, speed, play/pause, chunk m/n, follow on/off, and messages.

### `readaloud/keys.py` + `readaloud/app.py`
`less` keys (viewport moves independently of playback):
`j`/`Down`/`Enter` line down, `k`/`Up` line up, `d`/`Ctrl-D` half page down,
`u`/`Ctrl-U` half page up, `f`/`Ctrl-F`/`PgDn` page down, `b`/`Ctrl-B`/`PgUp` page up,
`g`/`Home` top, `G`/`End` bottom, `/` search, `?` search back, `n`/`N` repeat,
`q` quit, mouse wheel scrolls.
Reader keys: `Space` play/pause, `n`... conflicts with search-repeat, so use
`.`/`Right` next chunk, `,`/`Left` previous chunk, `[`/`]` speed down/up,
`F` toggle follow-the-word auto-scroll, `c` centre the view on the current word.
Any manual scroll turns follow OFF; `F` or `c` turns it back on.
Mouse: click a word -> jump playback to that word's chunk, starting at that word.

Prefetch: a worker thread keeps the current chunk plus the next `--prefetch` (default 2)
synthesized in a bounded cache. A jump re-prioritises the target chunk.
Startup: draw the document immediately, show "loading voice..." in the status bar, begin
playback once the engine is ready.

## Non-negotiables
- No Homebrew. No `brew install`. Everything through `uv`.
- Never block the curses loop on synthesis.
- Must survive: terminal resize, empty input, input with no speakable words, a chunk whose
  synthesis raises, and `q` during synthesis (clean exit, no hung threads, terminal restored).
