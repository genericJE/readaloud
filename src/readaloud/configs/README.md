# Shipped preferences

The files a first run copies into `~/.readaloud/`, and what `--write-config` rewrites:

| File | Applies to |
| --- | --- |
| `readaloud.conf` | everything: the settings, the shapes and parts, the words Kokoro gets wrong; the `[files]` map from extensions to configs; the `code` layer every source file shares (`format = code`, the words for the operators) |
| `markdown.conf` | Markdown files: drawn by mdcat when installed, tables read cell by cell |
| `python.conf` | Python files: how a `def`, a decorator or an attribute is said |

A language's file is named after its grammar (`python`, `rust`, `javascript`: the name
after `tree-sitter-` in the package, and in Neovim's `parser/`), and holds plain
`[config]`, `[pronunciations]`, `[shapes]`, `[parts]` and `[syntax]` sections that apply to
the files the `[files]` section of `readaloud.conf` sends to it (`.rs = code rust`). See [Configuration](../../../docs/configuration.md#file-types)
and [Syntax](../../../docs/pronunciations.md#syntax).

## Sharing a language

A `rust.conf`, `go.conf` or `javascript.conf` that reads well is worth more here than in
one `~/.readaloud/`. To contribute one:

1. Write it against the grammar's node kinds (`readaloud --tree lib.rs` prints them).
2. Check it loads without a notice: `readaloud --config /path/to/dir -f lib.rs`, with the
   file in a directory beside a `readaloud.conf`.
3. Open a pull request adding it to this directory, with a few lines of the language in
   the description and how they are read.

A file here is copied on a first run only when readaloud ships the language's grammar
(Python today); the others are for people to copy by hand, with the grammar installed next
to readaloud (`uv tool install readaloud --with tree-sitter-rust`).
