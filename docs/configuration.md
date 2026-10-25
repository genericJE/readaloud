# Configuration

What lives in `~/.readaloud/`: what takes precedence over what, every setting the files
hold, and how readaloud reports a problem with them.

The preferences are a directory of plain INI files:

| File | Applies to |
| --- | --- |
| `~/.readaloud/readaloud.conf` | everything: the settings, the words Kokoro gets wrong, the shapes; the `[files]` table; the `code` layer every source file shares |
| `~/.readaloud/markdown.conf` | Markdown files: drawn by mdcat, tables read cell by cell |
| `~/.readaloud/python.conf`, `rust.conf`, ... | that language's files alone, named after the grammar |

Most flags can also be preferences; the ones that pick the input or the output (`-f`,
`--conf`, `--start`, `--save`) cannot. A `[pronunciations]` section tells readaloud how to
say particular words, the `[shapes]` and `[parts]` sections how it says a file with its
line number or a host with its port, and a `[syntax]` section how it says a source file's
syntax tree (see [Pronunciations](pronunciations.md)). The same sections in a language's
file, or headed with a layer's name (`[config.code]`), apply to some files only (see
[File types](#file-types)). The first run writes the three files readaloud ships, shown
in full [below](#the-files-a-first-run-writes), and says so on stderr:

```
readaloud: created /Users/you/.readaloud/ -- your defaults live there now (readaloud.conf, markdown.conf, python.conf)
```

Precedence is `explicit flag > ~/.readaloud/ > built-in default`, key by key rather than
file by file: `readaloud --speed 2.0` with `speed = 1.4` and `voice = bm_george` in the
file runs at 2.0x in `bm_george`. A flag counts as explicit even when it matches the
built-in default, so `--speed 1.0` really does mean 1.0 whatever the file says.

`--config PATH` reads a different directory, or a single `.conf` file; `--no-config`
ignores the preferences entirely; `--write-config` writes the shipped files over whatever
is there.

Up to readaloud 2.0 the preferences were one file, `~/.readaloud.conf`. The first run of a
newer readaloud moves it into the directory: the sections for a file type go to that
language's file with the suffix taken off their headers (`[syntax.py]` becomes the
`[syntax]` of `python.conf`), the rest stays in `readaloud.conf`, the `[x.code]` sections
included, a `markdown = no` becomes `format = code`, and the old file is deleted; stderr
says what moved where. Any shipped file that was not among them is added beside them.

## Every setting

Settings go under `[config]` in `readaloud.conf`. A setting the file leaves out keeps its
default.

The section was called `[readaloud]` up to readaloud 1.2; a file that still says so is
warned about and its settings are ignored until the header is renamed.

| Setting | Default | What it does |
| --- | --- | --- |
| `voice` | `af_heart` | Kokoro voice name. `readaloud --list-voices` prints the ones your model repo has; the first letter picks the language (a = American English, b = British, ...). |
| `speed` | `1.0` | Speech rate multiplier, clamped to 0.5 to 3.0. |
| `lang` | empty | Kokoro language code. Empty derives it from the voice name. |
| `repo` | `mlx-community/Kokoro-82M-4bit` | HuggingFace model repo to load the voice from. |
| `sentences` | `4` | Most sentences in one spoken chunk, at least 1. |
| `chars` | `380` | Most characters in one spoken chunk, whichever limit is hit first, at least 1. |
| `prefetch` | `2` | Chunks synthesized ahead of the one being spoken. 0 turns prefetching off. |
| `device` | empty | Audio output: an index, part of a device name, or `default`. Empty is the system default. `readaloud --list-devices` lists them. |
| `color` | `true` | Render the input's colours. `false` is `--no-color`. Takes true/false, yes/no, on/off, 1/0. |
| `follow_lead` | `20` | Extra rows follow mode scrolls past the strict minimum (see below). |
| `follow_margin` | `5` | Rows kept between the spoken word and the top or bottom edge before follow mode scrolls. |
| `format` | `markdown` | How plain input (a file, a string, a pipe without colours) is read. `markdown`: its Markdown syntax (`#`, `**`, `[text](url)`) is stripped before it is spoken and a fenced or indented block is read in one go. `code`: as written, indented lines cut into chunks of at most `chars` like any paragraph, and each operator (`==`, `<=`, `->`) lit while it is spoken. `plain`: as written, read as prose, for text whose `*asterisks*` are meant. Coloured input, and what `renderer = mdcat` draws, are none of these. |
| `highlight` | `true` | Colour a source file by its grammar (see [Syntax](pronunciations.md#syntax)): keywords, strings, comments, names. `false` reads it in the terminal's plain colours; `--no-color` does too. |
| `language` | empty | The grammar a file is read with, where that differs from the last layer `[files]` gives it (`python` for `.py`). |
| `parsers` | empty | A directory of `<language>.so` parser files, as Neovim's `:TSInstall` builds them, tried when no `tree-sitter-<language>` package is installed. |
| `renderer` | `none` | `mdcat` draws plain input with mdcat before it is read, tables cell by cell (see [Markdown](markdown.md)); `markdown.conf` sets it for `.md` files. Without mdcat installed the input is read without it, its Markdown syntax stripped, with a notice. |
| `media_keys` | `true` | Take over the system play/pause button. Needs the optional `[mediakeys]` extra; without it this does nothing. `false` is `--no-media-keys`. |

## File types

Which configs read a file is a table in `readaloud.conf`, `[files]`: an extension, or
several, and the *layers* that read such a file, in order, each laid over the one before:

```ini
[files]
.py .pyi = code python
.rs = code rust
.md = markdown
.txt = plain
```

A layer is a name. Its sections are the ones headed with that name in any file
(`[config.code]`, `[pronunciations.code]` in `readaloud.conf`) plus a `<name>.conf` in the
directory (`python.conf`), both if both exist. The last layer of a line also names the
grammar the file is parsed with (`python`, as in `tree-sitter-python` and Neovim's
`parser/python.so`); the `language` setting overrides that. The shipped table maps the
common languages; adding one is a line here and a `rust.conf` beside the others, which
the [shipped directory](https://github.com/genericJE/readaloud/tree/main/src/readaloud/configs)
is the place to share. The shipped layers:

- **`code`**, in `readaloud.conf`: `format = code` and the words for the operators (`== = is
  equal to`, `-> = arrow`), which hold for every language and would misfire in prose.
- **`plain`**, in `readaloud.conf`: `format = plain`, for `.txt`.
- **`markdown`**, in `markdown.conf`: `format = markdown` and `renderer = mdcat`.
- **`python`**, in `python.conf`: how a `def`, a decorator, an attribute, a comment or a
  triple-quoted string is said (a comment without its `#`; the marker is each language's
  own, so the rule is the language's).

A file gets its layers when readaloud reads it by name, `readaloud app.py` or `-f app.py`;
piped input and TEXT have no name, so only `readaloud.conf` applies to them, unless
`--conf NAME` says otherwise: `--conf md` reads the input with the layers of a `.md` file,
`--conf py` with a `.py` file's, `--conf python` with that one layer, whatever the input
is called. The layers are laid over
one another in the line's order, each winning over the one before:

- a **`[config]`** setting replaces the one before; a setting left out keeps the earlier
  value, and a flag still beats them all;
- **`[shapes]`** of a later layer are tried first, so a line that fits wins over an earlier
  one;
- a **`[pronunciations]`** entry for the same text replaces the earlier one;
- **`[parts]`** and named shapes of an earlier layer may be used by a later one, and a part
  of the same name replaces the earlier one for the later layer's shapes;
- **`[syntax]`** rules come after the earlier ones, so a later rule for the same kind wins.

A problem is reported with its file and section, as in `python.conf: [shapes] line 12:
...` or `[pronunciations.code] line 7: ...`. In a single `.conf` file (`--config one.conf`)
every layer is written with headers, `[syntax.python]` beside `[pronunciations.code]`.
The shipped `python.conf` is the worked example, in [Syntax](pronunciations.md#syntax).

## The files a first run writes

These are the files `~/.readaloud/` holds after the first run, and what `--write-config`
writes. `readaloud.conf` leaves every setting at its default and fills in the `[files]`
table, the parts, shapes and pronunciations, and the `code` and `plain` layers;
`markdown.conf` and `python.conf` are what they say on the tin. Delete any line you do
not want; a shipped file is written again when it is missing, so empty one rather than
delete it.

### readaloud.conf

```ini
[config]
# settings; docs/configuration.md lists them all

[files]
# which configs read a file, by its extension: each lays over the one before and
# the last names the grammar; a name is the [x.name] sections here plus name.conf
.py .pyi = code python
.rs = code rust
.js .mjs = code javascript
.ts = code typescript
.tsx = code tsx
.go = code go
.c .h = code c
.cc .cpp .hpp = code cpp
.java = code java
.kt = code kotlin
.rb = code ruby
.php = code php
.swift = code swift
.sh .bash .zsh = code bash
.lua = code lua
.toml = code toml
.json = code json
.yaml .yml = code yaml
.css = code css
.sql = code sql
.hs = code haskell
.ex .exs = code elixir
.zig = code zig
.nix = code nix
.md = markdown
.txt = plain

[config.code]
# source files: read as written, indented lines cut into chunks, operators lit
format = code

[config.plain]
# text that is neither Markdown nor code: as written, read as prose
format = plain

[parts]
# named pieces of pattern, used as %{name} by the shapes and by later parts
# files
ext = [A-Za-z]\w*
file = \S+\.%{ext}
# web domains
web = com|org|net|edu|gov|mil|int|info|biz|io|dev|ai|co|me|tv
newweb = cloud|tech|online|site|xyz
europe = de|uk|eu|fr|nl|ch|at|se|no|fi|dk|it|es|pt|pl|cz|be|ie
world = jp|cn|kr|in|us|ca|au|nz|br|ru
private = local|localdomain|lan|home|internal|intranet|corp
reserved = arpa|test|example|invalid
domain = %{web}|%{newweb}|%{europe}|%{world}|%{private}|%{reserved}
# hosts, ports and URLs
name = [A-Za-z][\w-]*(?:\.[A-Za-z][\w-]*)*\.(?i:%{domain})
octet = 25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d
ipv4 = %{octet}\.%{octet}\.%{octet}\.%{octet}
host = localhost|%{ipv4}|%{name}
port = 6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|\d{1,4}
hostport = (?<host>%{host}):(?<port>%{port})
scheme = (?<scheme>https?|ftps?|ssh|git)://
path = [^\s<>"'|)\]]*[^\s<>"'|)\].,;:!?]
# times
hour = [01]?\d|2[0-3]
minute = [0-5]\d

[shapes]
# /pattern/ = how to say it, $name being a named group; first fit wins
/%{scheme}%{hostport}(?<path>/%{path})/ = $scheme $host port $port ${path|/=slash}
/%{scheme}%{hostport}/?/ = $scheme $host port $port
/%{hostport}/ = $host port $port
/(?<file>%{file}):(?<line>\d+):(?<col>\d+)/ = $file line $line, column $col
/(?<file>%{file}):(?<from>\d+)-(?<to>\d+)/ = $file lines $from to $to
/(?<file>%{file}):(?<line>\d+)/ = $file line $line
/(?<hour>%{hour}):(?<minute>%{minute})(?::(?<second>%{minute}))?/ =

[pronunciations.code]
# the operators of a source file, which the voice would otherwise skip; every language
= = equals
== = is equal to
< = less than
> = greater than
<= = less than or equal to
>= = greater than or equal to
- = minus
* = times
/ = divided by
// = floor divided by
** = to the power of
% = modulo
& = bitwise and
| = pipe
|= = pipe equals
~ = bitwise not
^ = xor
<< = shift left
>> = shift right
+= = plus equals
-= = minus equals
*= = times equals
/= = divided equals
-> = arrow

[pronunciations]
# text = how to say it; lowercase matches any case, a capital only that case

# names, and quotes that stand for nothing
id = ID
ids = IDs
"''" = empty string
'""' = empty string

# file names and extensions
README.md = readme dot M D
CHANGELOG.md = changelog dot M D
TODO.md = todo dot M D
.md = dot M D
.txt = dot text
.rst = dot R S T
.json = dot jason
.yaml = dot yaml
.yml = dot yaml
.toml = dot toml
.ini = dot ini
.cfg = dot config
.conf = dot conf
.env = dot env
.csv = dot C S V
.tsv = dot T S V
.xml = dot X M L
.html = dot H T M L
.css = dot C S S
.scss = dot S C S S
.js = dot J S
.jsx = dot J S X
.ts = dot T S
.tsx = dot T S X
.py = dot pie
.rb = dot R B
.go = dot go
.rs = dot R S
.java = dot java
.kt = dot K T
.swift = dot swift
.php = dot P H P
.lua = dot lua
.cpp = dot C P P
.hpp = dot H P P
.sh = dot S H
.bash = dot bash
.zsh = dot Z S H
.sql = dot S Q L
.sqlite = dot sequel lite
.db = dot D B
.log = dot log
.lock = dot lock
.pem = dot pem
.crt = dot C R T
.zip = dot zip
.tar = dot tar
.gz = dot G Z
.png = dot P N G
.jpg = dot J peg
.jpeg = dot J peg
.gif = dot gif
.svg = dot S V G
.webp = dot web P
.pdf = dot P D F
.mp3 = dot M P three
.mp4 = dot M P four
.wav = dot wave
.exe = dot E X E
.dll = dot D L L
.dylib = dot dylib
.app = dot app
.git = dot git
.dmp = dot D M P

# protocols and hosts
https = H T T P S
http = H T T P
ftps = F T P S
ftp = F T P
ssh = S S H
localhost = local host
IPv4 = eye pea V four
IPv6 = eye pea V six

# top-level domains
.com = dot com
.org = dot org
.net = dot net
.de = dot D E
.uk = dot U K
.io = dot I O
.dev = dot D E V
.ai = dot A I
.co = dot co
.info = dot info
.gov = dot guv
.edu = dot E D U
.eu = dot E U
.me = dot me
.tv = dot T V
.us = dot U S
.xyz = dot X Y Z
.ch = dot C H
.fr = dot F R
.nl = dot N L
.jp = dot J P
.ru = dot R U
.cn = dot C N
.in = dot I N
.it = dot I T

# languages, tools and formats
C# = C sharp
F# = F sharp
.NET = dot net
YAML = yammle
TOML = tommle
cli = C L I
aws = A. W. S.
redis = red iss
postgresql = Postgres Q L
sqlite = sequel lite
systemd = system dee
journalctl = journal control
kubectl = cube control
k8s = kubernetes
PyPI = pie pea eye
enum = ee num
dateutils = date utils

# code punctuation that turns up in prose too
!= = not equals
:: = colon colon
&& = and
|| = or
```

### markdown.conf

```ini
# ~/.readaloud/markdown.conf: Markdown files (.md).  Drawn by mdcat when it is
# installed, so a table is read one cell at a time; without mdcat the syntax
# is stripped and the text read as prose.

[config]
format = markdown
renderer = mdcat
```

### python.conf

```ini
# ~/.readaloud/python.conf: Python files, read through the tree-sitter-python
# grammar.  `readaloud --tree app.py` shows the node kinds a rule can name;
# docs/pronunciations.md, "Syntax", explains the wording.

[syntax]
# node kind = wording; $field is that part of the node, $* its parts, $0 the whole
function_definition = function $name $parameters $return_type $body
class_definition/function_definition = method $name $parameters $return_type $body
function_definition.return_type = and returns type $0
parameters = takes ${*|,=and}
typed_parameter = ${*|:=type}
typed_default_parameter = $name type $type with a default of $value
default_parameter = $name with a default of $value
type/binary_operator = $left or $right
list_splat_pattern = ${0|*=star}
dictionary_splat_pattern = ${0|**=star star}
attribute = $object dot $attribute
decorator = ${0|@=decorator}
dotted_name = ${*|.=dot}
# a comment without its #: "the limit", not "hash the limit"; a // in it stays
comment = ${0|#=}
# a string's triple quotes are not said; a '' still says "empty string"
string_start = ${0|"""=|'''=}
string_end = ${0|"""=|'''=}

[pronunciations]
# what is Python's alone; the operators every language shares are in
# readaloud.conf's [pronunciations.code]
str = string
:= = walrus
```

## How far ahead follow mode scrolls

Follow mode used to do the smallest scroll that kept the spoken word on screen, which
pins the reading position to the bottom row: you can see everything you have already
heard and nothing you are about to. `follow_lead` scrolls that many rows *further* at the
moment a scroll is needed, so the view advances in stable jumps and the next chunk lands
around the middle of the screen.

On a 30-row terminal with the defaults, `follow_lead = 20` and `follow_margin = 5`, the
spoken word lands on about row 6, just below the top margin, with roughly 23 rows of unread
text below it; it drifts down to the bottom margin and the view jumps again.
`follow_lead = 0` restores the old creep-one-row-at-a-time behaviour. The lead never
scrolls the spoken word off the screen, and it applies only when scrolling *down*: moving
back up to a word behind the viewport is unchanged.

`c` uses this same placement without switching follow mode on, where `F` centres the word
and follows it. Both keys are listed in [Reading](reading.md).

[Back to the README](../README.md)
