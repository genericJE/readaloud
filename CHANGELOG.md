# Changelog

## 2.1.0 (2026-10-25)

The preferences are a directory of files, one per kind of input, picked by the file's
extension: a Markdown file is drawn by mdcat and read cell by cell without a flag, and a
Python file is read through its grammar by the shipped `python.conf`.

- **The preferences are a directory, `~/.readaloud/`**: `readaloud.conf` for what applies
  to everything, `markdown.conf` for Markdown, and a `<language>.conf` per language
  (`python.conf`, `rust.conf`), named after the grammar, with plain `[config]`,
  `[pronunciations]`, `[shapes]`, `[parts]` and `[syntax]` sections inside. The first run
  of this version moves an existing `~/.readaloud.conf` into the directory, section by
  section, deletes it and says so. The three shipped files live in
  `src/readaloud/configs/`, where a language's file can be shared.
- **`[files]` in `readaloud.conf` says which configs read a file**, by its extension:
  `.py .pyi = code python`, `.rs = code rust`, `.md = markdown`. Each name is a layer, the
  `[config.code]`-style sections in `readaloud.conf` plus a `<name>.conf`, laid over one
  another in that order; the last names the grammar. Adding Rust is one line and a
  `rust.conf`. The `code` layer, every source file's `format = code` and the words for the
  operators, lives in `readaloud.conf`.
- **`-md` and `--markdown` are gone** (a hint says what to do). `readaloud notes.md` reads
  the file: a lone argument that names a file is that file, and `.md` gets
  `markdown.conf`, whose new `renderer = mdcat` draws it with
  mdcat and reads its tables cell by cell, falling back to stripped text with a notice when
  mdcat is not installed. A pipe or TEXT gets the same with `--conf md`; `--conf NAME`
  reads any input with the configs of a `.NAME` file or the layer NAME.
- A Python comment is read without its `#`, and a triple-quoted string without its quotes:
  `[syntax]` rules in the shipped `python.conf`; a language's file carries its own markers.
  A rule may say nothing, and the text it covers is then neither read nor reached by an
  entry; a rule beats an entry of the same length, where a shape loses to one. The two
  "triple quote" entries are gone from the shipped `readaloud.conf`.
- A comment leading a `def`'s body, which the grammar hangs on the `def` itself rather
  than in the body, is said before the body instead of being dropped by the rule.
- `readaloud --tree app.py` prints a file's syntax tree, the node kinds and fields a
  `[syntax]` rule can name, and the docs say how to change the reading of a piece of code:
  find its kind in the tree, write the line, delete it to go back.
- The `markdown = yes|no` setting of 2.0 is `format = markdown | code | plain`: how plain
  input is read. `code` is what the `code` layer sets; `plain` is new, for text that is
  neither Markdown nor code and should keep its `*asterisks*`. The first run rewrites an old
  `markdown = no` as `format = code`.
- With `format = code`, a source file's indented lines are cut into chunks of at most
  `chars` like any paragraph, at line ends, instead of being read as one code block: a
  class body no longer waits on one long synthesis.
- In a source file, operators (`==`, `<=`, `->`, the `*` of `*args`) are words of their
  own, lit while they are spoken; before, their speech ran under the neighbouring word.
  A rule's opening words light the token before the node, so "and returns type" lights
  the `->` and "with a default of" the `=`.

## 2.0.0 (2026-10-24)

The computational half of readaloud is now Rust, and a source file is read through its
syntax tree and coloured like an editor would.

### A Rust core

- Everything that is computation moved out of Python into a compiled extension,
  `readaloud._core`, built with PyO3 and maturin: terminal cell widths (wide CJK and
  emoji), the pronunciation engine, word and sentence splitting and chunking, the
  escape-sequence parser with its Markdown cleanup, and the alignment of Kokoro's
  timestamps onto the spoken words. The Python that remains is the interface: the
  config file, the document model, the TUI, playback.
- Each port is checked against the Python it replaced, on random input as well as the
  known cases, so a file reads exactly as it did. The Unicode tables are generated from
  Python's own database, so the two sides never disagree about a character.
- The Homebrew bundle ships the extension built on the macOS 14 runner with the pinned
  Rust; nothing is compiled on your machine.

### Source code, read and coloured through its grammar

- **A `.py` file is parsed with tree-sitter** and said node by node the way you describe
  it in a `[syntax.py]` section: `def foo(bar: int, baz: str='') -> str:` can be
  "function foo takes bar type int and baz type string with a default of empty string
  and returns type string", a `|` in a type annotation "or" and one between sets
  "pipe", `foo().bar` "foo dot bar", `__init__` "dunder init". A rule names a node's
  fields (`$name`, `$parameters`), the node in full (`$0`) or its parts (`$*`), and may
  be confined to a context (`type/binary_operator`) or a field
  (`function_definition.return_type`). Comments, strings and docstrings are untouched
  unless a rule names them. See [Syntax](docs/pronunciations.md#syntax).
- **The file is coloured** by the grammar's own highlights query, in the terminal's
  colours: keywords, strings, comments, function and type names. `highlight = no` or
  `--no-color` leaves it plain.
- **Grammars are plug-ins.** readaloud ships the tree-sitter runtime and no grammar; a
  language's grammar is loaded at run time from its `tree-sitter-<language>` package
  (prebuilt wheels, no compiler) or from a Neovim `parser/` directory. The Homebrew
  install has Python's; `uv tool install readaloud --with tree-sitter-rust` adds Rust.
- **Each spoken word lights the word it came from:** "takes" lights the first
  parameter, "or" the type after the `|`, and a body of code keeps its lines.
- **A section can apply to one file type**: `[config.py]`, `[parts.py]`, `[shapes.py]`,
  `[pronunciations.py]` and `[syntax.py]` lay over the general ones for a file with
  that suffix, and `markdown = no` reads it as written, so code keeps its `#`, `*args`
  and underscores. See [File types](docs/configuration.md#file-types).

### Shapes

- **Shapes and parts live in the config file**, with none built in: a `[shapes]` line is
  a regular expression and how to say what it matched, `[parts]` the named pieces the
  patterns are built from. The shipped file reads `settings.py:281-283` as "settings dot
  pie lines 281 to 283", `localhost:8080` as "local host port 8080", a URL with its
  scheme, host, port and each slash of its path, and leaves a time as written.
- **Named shapes** (`param: /pattern/ = wording`) let a wording say a list of any
  length: `${params|param|,=and}` says every match of `param` in the group, with "and"
  for each comma. A shape may span 48 words and several lines.
- What a shape or a rule says goes through the pronunciation entries and the patterns
  once more, so `.py = dot pie` and `/__(\w+)__/ = dunder $1` apply inside them.

### Config file: one change to make

- The settings section is `[config]`, not `[readaloud]`. A file that still says
  `[readaloud]` is warned about and its settings fall back to their defaults until the
  header is renamed. Everything else in an existing file reads as before.
- The file a first run writes, and `--write-config`, is `readaloud.conf` shipped as it
  is, with one-line comments and the pronunciations in groups.

### Other changes

- `follow_margin` defaults to 5 rows: the spoken word stays about six rows from the top
  in follow mode, with the text below it in view.
- The README no longer suggests `mdcat --ansi notes.md | readaloud`: a piped table reads
  line by line; `-md` reads it cell by cell.
- `tests/fixtures/edge-cases.md` gathers the text that is hardest to read well, for
  listening to by hand.

## 1.2.3 and earlier

See the [releases](https://github.com/genericJE/readaloud/releases).
