# Markdown

This page covers reading Markdown from its source, and how the tables in it are read.

## Markdown, drawn by mdcat

A `.md` file is read with `markdown.conf`, whose `renderer = mdcat` has readaloud run mdcat
itself: the file is shown as `mdcat --ansi` draws it, headings, colours and tables
included, and each table is read one cell at a time.

```bash
readaloud notes.md
readaloud notes.md --save notes.wav
cat notes.md | readaloud --conf md
```

The file's name is enough: a lone argument that names a file is read as that file (`-f
notes.md` is the explicit form). A pipe or TEXT has no name, so `--conf md` says to read it
as a `.md` file would be. Which configs a `.md` file gets is the `.md = markdown` line of
`[files]` in `readaloud.conf` (see [File types](configuration.md#file-types)); set
`renderer = none` in `markdown.conf` to read Markdown as stripped text instead.

mdcat draws a table without pipes, padding its columns with spaces and wrapping a long
cell onto extra lines beside its neighbours. Once it is rendered nothing says where one
cell ends and the next begins, so a piped table is read line by line, the words of
neighbouring cells mixed together. From the source, the cells are known:

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
  cell holding nothing but symbols is read by their names, so the
  [Keys table](reading.md) says "dot, right arrow" for its `.` · `→` row, and `⇧⌘` is
  "shift, command". A symbol-only cell with a symbol readaloud has no name for is
  skipped whole. A [pronunciation](pronunciations.md) can change what a named symbol
  says, but it cannot make a skipped cell speak.
- Footnote numbers stay on screen and are read where they stand, and the footnotes
  themselves are read; the pipe removes the numbers and skips the footnotes. A badge, or
  any image inside a link, is read by its alt text without the `[1]` mdcat puts after it,
  and the numbered URL line mdcat adds for it is shown but not read.
- A table readaloud cannot map is read line by line, as it would be when piped, and a
  notice in the status bar says so (in full on stderr for `--save`).
- The layout is fixed at startup: the terminal's width, capped at 80 columns as mdcat
  itself does (`--save` always uses 80). Narrow the terminal afterwards and the lines soft
  wrap instead of being redrawn.

Without mdcat on your `PATH`, the Markdown is read as written, its syntax stripped, and the
status bar says so once (`brew install mdcat`). Input already rendered with `mdcat --ansi`
is read as it came, colours and all, since there is no source left to find the cells in.

[Back to the README](../README.md)
