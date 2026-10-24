# Changelog

## 2.0.0 (2026-10-24)

### Config file: one change to make

- The settings section is `[config]`, not `[readaloud]`. A file that still says
  `[readaloud]` is warned about and its settings fall back to their defaults until the
  header is renamed. Everything else in an existing file reads as before.

### Reading code

- **Source files are read through their grammar.** A `[syntax.py]` section says a
  Python file's syntax tree node by node the way you describe it, so
  `def foo(bar: int, baz: str='') -> str:` can be "function foo takes bar type int and
  baz type string with a default of empty string and returns type string", a `|` in a
  type annotation "or" and one between sets whatever you choose. readaloud ships the
  tree-sitter runtime and loads a language's grammar at run time: the Homebrew install
  has Python's; any other language is its `tree-sitter-<language>` package installed
  next to readaloud, or a Neovim `parser/` directory. See
  [Syntax](docs/pronunciations.md#syntax).
- **Source files are coloured** by the grammar's own highlights query, in the terminal's
  colours. `highlight = no` or `--no-color` leaves them plain.
- **A section can apply to one file type**: `[config.py]`, `[parts.py]`, `[shapes.py]`,
  `[pronunciations.py]` and `[syntax.py]` lay over the general ones when the file read
  has that suffix. See [File types](docs/configuration.md#file-types).
- **`markdown = no`** reads a plain file as written, so code keeps its `#`, `*args` and
  `__dunders__`.

### Shapes

- **Shapes and parts live in the config file**, with none built in: a `[shapes]` line is
  a regular expression and how to say what it matched, `[parts]` the named pieces the
  patterns are built from. The shipped file reads `settings.py:281-283` as "settings dot
  pie lines 281 to 283", `localhost:8080` as "local host port 8080", a URL with its
  scheme, host, port and each slash of its path, and leaves a time as written.
- **Named shapes** (`param: /pattern/ = wording`) let a wording say a list of any
  length: `${params|param|,=and}` says every match of `param` in the group, with "and"
  for each comma. A shape may span 48 words and several lines.
- **Each spoken word lights the word it came from**: `$file` lights the file name,
  "takes" the first parameter, punctuation stays with its word. A wording used to be
  shared out a word per slot.
- What a shape or a rule says goes through the pronunciation entries *and* the patterns
  once more, so `/__(\w+)__/ = dunder $1` reaches a name a `def` rule said.

### Other changes

- The computation moved to a Rust extension, `readaloud._core`: terminal widths, the
  pronunciation engine, word and sentence splitting, the escape-sequence parser and the
  timing of spoken words. Each port is checked against the Python it replaced.
- `follow_margin` defaults to 5 rows: the spoken word stays about six rows from the top
  in follow mode, with the text below it in view.
- The file a first run writes, and `--write-config`, is `readaloud.conf` shipped as it
  is, with one-line comments and the pronunciations in groups.
- The README no longer suggests `mdcat --ansi notes.md | readaloud`: a piped table reads
  line by line; `-md` reads it cell by cell.
- `tests/fixtures/edge-cases.md` gathers the text that is hardest to read well, for
  listening to by hand.

## 1.2.3 and earlier

See the [releases](https://github.com/genericJE/readaloud/releases).
