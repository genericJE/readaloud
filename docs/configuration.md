# Configuration

What lives in `~/.readaloud.conf`: what takes precedence over what, every setting the file
holds, and how readaloud reports a problem with it.

Most flags can also be preferences in `~/.readaloud.conf`; the ones that pick the input or
the output (`-f`, `-md`, `--start`, `--save`) cannot. A `[pronunciations]` section in the
same file tells readaloud how to say particular words, and the `[shapes]` and `[parts]`
sections how it says a file with its line number or a host with its port (see
[Pronunciations](pronunciations.md)). The first run writes a copy of the file readaloud
ships, shown in full [below](#the-file-a-first-run-writes), and says so on stderr once:

```
readaloud: created /Users/you/.readaloud.conf -- your defaults live there now
```

Precedence is `explicit flag > ~/.readaloud.conf > built-in default`, key by key rather
than file by file: `readaloud --speed 2.0` with `speed = 1.4` and `voice = bm_george` in
the file runs at 2.0x in `bm_george`. A flag counts as explicit even when it matches the
built-in default, so `--speed 1.0` really does mean 1.0 whatever the file says.

`--config PATH` reads a different file, `--no-config` ignores the file entirely, and
`--write-config` writes the shipped file over whatever is there.

## Every setting

Settings go under `[config]`. A setting the file leaves out keeps its default.

The section was called `[readaloud]` up to readaloud 1.2; a file that still says so is warned about and its settings are ignored until the header is renamed.

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
| `markdown` | `true` | Strip Markdown syntax (`#`, `**`, `[text](url)`) from a plain file, a string or a pipe before it is spoken. `false` reads it as written, which suits source code. Coloured input and `-md` are never stripped. |
| `media_keys` | `true` | Take over the system play/pause button. Needs the optional `[mediakeys]` extra; without it this does nothing. `false` is `--no-media-keys`. |

## The file a first run writes

This is what `~/.readaloud.conf` holds after the first run, and what `--write-config`
writes. It leaves every setting at its default, and fills in the parts, shapes and
pronunciations: delete any line you do not want.

```ini
[config]
# settings; docs/configuration.md lists them all

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

[pronunciations]
# text = how to say it; lowercase matches any case, a capital only that case

# names, and quotes that stand for nothing
id = ID
ids = IDs
"''" = empty string
'""' = empty string
"'''" = triple quote
'"""' = triple quote

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

# what the punctuation of code is called
!= = not equals
:: = colon colon
&& = and
|| = or
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

On a 30-row terminal with the defaults, `follow_lead = 20` and `follow_margin = 5`, the
spoken word lands on about row 6, just below the top margin, and roughly 23 rows of unread
text stay visible below it. It then drifts down to
the bottom margin and the view jumps again. `follow_lead = 0` restores the old
creep-one-row-at-a-time behaviour. The lead never scrolls so far that the spoken word
would leave the screen, and it applies only when scrolling *down*: moving back up to a
word behind the viewport is unchanged.

`c` uses this same placement without switching follow mode on, where `F` centres the word
and follows it. Both keys are listed in [Reading](reading.md).

[Back to the README](../README.md)
