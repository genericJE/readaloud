# Pronunciations

The `[pronunciations]` section of `~/.readaloud/readaloud.conf`: how to say a word or phrase your
way.

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
✓ = check                  # a tick in an mdcat table says "check", not "yes"
macOS = macOS              # said as written, which keeps "OS" below off it
OS = O S
"#include" = hash include  # quote text that starts with # or ; or a quote
```

A fresh `~/.readaloud/readaloud.conf` already holds about 125 of these, each one a word Kokoro
was measured saying wrongly: the common file types (`main.py` is "main dot pie",
`config.yml` is "config dot yaml", `README.md` keeps its name instead of being spelled
out), `id` and `ids`, the quotes that stand for nothing (`''` is "empty string", `"""` is
"triple quote"), languages and tools (`C#`, `.NET`, `json`, `YAML`, `systemd`,
`journalctl`, `kubectl`, `k8s`, `redis`, `postgresql`, `PyPI`), and what the punctuation
of code is called (`!=`, `->`, `=>`, `::`, `&&`, `||`), the URL schemes Kokoro says
nothing for (`https`, `ssh`) and the common web domains (`.org`, `.io`, `.co.uk`). They are
ordinary lines in your
file: delete one to be rid of it, or edit it to say it your way. Anything readaloud
already says correctly is deliberately absent, so `SQL` stays "sequel", `nginx` stays
"engine x" and `C++` stays "C plus plus".

How the text on the left is found:

- **It is text, not a pattern,** unless it is between slashes (see
  [Patterns](#patterns) below). `C#`, `std::vector` and `.NET` mean exactly those
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
  `id = id card` is safe. The one exception is what a [shape](#shapes) or a
  [pattern](#patterns) says, which goes through these entries once more.

Put quotes, single or double, round text that starts with `#`, `;` or a quote, or that has
an `=` with spaces round it: `"#include" = hash include`, `"a = b" = c`. Without the
quotes, `#include = hash include` is a comment and does nothing. A `#` or `;` after a
space starts a comment, so a pronunciation holding one needs quotes too
(`hash = "number # sign"`), while `C# = C sharp` needs none. There are no escapes.

A respelling is kept off the text beside it by a space, wherever running into it would
change how Kokoro reads it: a letter, a digit or an underscore. So `ASP.NET` says "ASP dot
net", and `unique_provider_subscription_id` says "unique provider subscription, eye dee"
rather than one long token whose tail Kokoro reads as Freud's id. A hyphen gets no space,
measured the same way: one there reads worse.

Pronunciations apply wherever readaloud speaks: prose, code blocks, piped output and the
cells of a table mdcat drew, in the reader and in a `--save` WAV alike. Only what is said
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

A table mdcat drew says its ticks and crosses as "yes" and "no", and its arrows and keys
by name (see [Markdown](markdown.md)). Those names are not text an entry can
match, so `yes = yep` changes a written "yes" but no tick, and `right arrow = next` leaves
every `→` alone. An entry for the symbol itself replaces its name: with `✓ = check`, a `✓`
cell says "check" and `✓ (partial)` says "check (partial)". Each symbol is an entry of its
own, so that leaves `✅` saying "yes", and an entry reaches a named symbol only when its
text is that one symbol: `⌘C = copy` leaves a `⌘C` cell saying "command C", while
`⌘ = cmd` makes it "cmd C". Outside a table Kokoro says nothing at all for a `✓` beside
other words, and `✓ = check` makes it say "check" there too, lighting the tick while it
does. A line or cell holding nothing but symbols readaloud has no name for is still
skipped, so `★ = star` does nothing for a `★★★★` rating.

## Shapes

Some text is a shape rather than a word: a file name with a line number after it, a host
with its port. A `[shapes]` line describes one with a [regular expression][regex] between
slashes and says how to read it, naming what the pattern's groups matched:

```ini
[shapes]
/%{scheme}%{hostport}(?<path>/%{path})/ = $scheme $host port $port ${path|/=slash}
/%{scheme}%{hostport}/?/ = $scheme $host port $port
/%{hostport}/ = $host port $port
/(?<file>%{file}):(?<line>\d+):(?<col>\d+)/ = $file line $line, column $col
/(?<file>%{file}):(?<from>\d+)-(?<to>\d+)/ = $file lines $from to $to
/(?<file>%{file}):(?<line>\d+)/ = $file line $line
/(?<hour>%{hour}):(?<minute>%{minute})(?::(?<second>%{minute}))?/ =
```

| Text | Said, with the lines above |
| --- | --- |
| `https://example.com:8443/status?verbose=1` | H T T P S example dot com port 8443 slash status?verbose=1 |
| `https://example.com:8443/docs/v2/index.html` | H T T P S example dot com port 8443 slash docs slash v2 slash index dot H T M L |
| `localhost:8080`, `192.168.178.88:8123`, `example.com:443` | local host port 8080 |
| `main.rs:12:5` | main dot R S line 12, column 5 |
| `settings.py:281-283` | settings dot pie lines 281 to 283 |
| `settings.py:498` | settings dot pie line 498 |
| `14:35`, `9:05:59` | as written: Kokoro says "fourteen thirty-five" |

readaloud has no shape of its own: those seven lines are what a fresh `readaloud.conf`
comes with, and you add, change and delete them like any other line. A file with no
`[shapes]` section has none, which is the case for a file written by an older readaloud;
copy this section and the `[parts]` section below into it, before `[pronunciations]`.
`--no-config` reads with no shapes, as with no pronunciations.

- **A shape starts where a word starts and ends where a word ends,** or just past the
  punctuation after a word, so it sees the whole of `settings.py:498`, both sides of the
  colon, and never matches inside a word: `/py:\d+/` does not fire in `a.py:12`. It may
  span up to 48 words and several lines, so `/(?<file>%{file})\((?<line>\d+)\)/` can read
  `x.py(12)`, `/File "(?<file>[^"]+)", line (?<line>\d+)/` a Python traceback, and a
  [named shape](#named-shapes) a function signature. To cross a line, a pattern uses `\s`
  or a class such as `[^()]`; `.` and `\S` stop at the end of a line.
- **The first line that fits wins.** At each word the lines are tried in order, and the
  first that fits takes its longest fit there. That is why the hosts come first: they
  keep `example.com:443` a host, while `a.py:12`, whose `.py` is no web domain, falls
  through to the files. A URL with a port says the port and then its path with each `/` said
  as "slash": Kokoro is silent for a slash, and a path that starts with one makes it drop
  the query. A URL without a port is left as written. The scheme is said through `$scheme`; Kokoro
  is silent for `https` on its own, so the shipped pronunciations spell it out
  (`https = H T T P S`). Take `$scheme ` out of the wording to leave it unsaid.
- **Each spoken word lights the word it came from.** `$file` lights the file name and
  `$line` the number; a word of the wording's own lights the next word of the text after
  the group before it, so the "takes" of `function $fn takes $params` lights the first
  parameter, and punctuation written up against a group (`$line, column`) stays with it.
  A word the wording leaves unsaid lights nothing. A wording that says the groups out of
  the text's order can only light the later words, so keep the text's order where you can.
- **What it says goes through your pronunciations.** The `.py` of `settings.py:498`
  stays "dot pie" and `localhost` stays "local host", because a shape's wording is read
  by your entries, patterns included, once more before it is said.
- **An empty wording says the text as written.** That is not the same as no line: the
  shape still claims the text, so a pattern such as `/:(\d+)/` cannot reach into a time.
- **Your entries win.** An entry for the same text (`settings.py:498 = the settings`)
  beats the shape.

In a wording, `$name` or `${name}` is what the group `(?<name>...)` matched, `$1` and so on
a group by number, `$0` the whole match, and `$$` a dollar sign. `${name|from=to}` is the
group with every `from` in it said as the word `to`, so `${path|/=slash}` says each slash
of a path, however many there are; more replacements can follow, as in
`${path|/=slash|.=dot}`, and each is made once, in what the group matched. The same works
in the wording of a pattern under `[pronunciations]`. A line that cannot be used
is reported with its line number and costs only itself: a pattern that will not compile or
could match empty text, a group the pattern does not have, a part that does not exist.

### Named shapes

A pattern can say a list only if it knows how long the list is, since a group that repeats
keeps only its last match. A named shape says a list of any length. Give a `[shapes]` line
a name, `name: /pattern/ = wording`, and it is never looked for on its own; the lines
below it use it instead:

- **In a wording, `${group|name}`** says every match of the named shape in what the group
  matched, each by the named shape's wording with its own groups. The text between the
  matches goes through the `|from=to` replacements of the same `${...}`, so
  `${params|param|,=and}` says each parameter and an "and" for each comma.
- **In a pattern, `%{name}`** stands for the named shape's pattern, as a part would, with
  its groups unnamed so it may appear more than once.

This reads a Python `def`, on one line or spread over several:

```ini
[parts]
ident = [A-Za-z_]\w*

[shapes]
dflt: /=\s*(?<v>[^,)]+)/ = with a default of $v
param: /(?<name>%{ident})\s*(?<type>:\s*[^,=)]+)?\s*(?<default>=\s*[^,)]+)?/ = $name ${default|dflt} ${type|:=type}
/def\s+(?<fn>%{ident})\s*\((?<params>[^()]*?),?\s*\)\s*(?<ret>->\s*[^:]+)?:/ = function $fn takes ${params|param|,=and} ${ret|->=and returns a}

[pronunciations]
str = string
```

`def foo(bar: int, baz: str='') -> str:` is then "function foo takes bar type int and baz
with a default of empty string type string and returns a string": `''` is the shipped
"empty string" and `str` the entry above, since the whole wording goes through your
pronunciations once more. A group that took no part, such as a missing default, says
nothing. A shape may name only the named shapes above it, one per `${...}`, and named
shapes may call on one another eight deep. `dflt` is a named shape, not `|=...`, because
a replacement cannot start with `=`. `[^,=)]` keeps the example short: a type such as
`dict[str, int]` breaks at its comma, and `*args` keeps its star unless you add `|*=star`.
A shape is found within one stretch of text: a blank line ends one, and so can a long
paragraph, which is cut at about 380 characters. Shapes like these belong to one language,
so they go in that language's file, `~/.readaloud/python.conf`, which applies only to
`.py` files (see [file types](configuration.md#file-types)).

## Syntax

For source code, a shape is the wrong tool: a parameter list can hold commas inside
`dict[str, int]`, a comment, a decorator, any number of lines. The `[syntax]` section of
`~/.readaloud/python.conf` reads a `.py` file through a real parser instead, and says the
parts of its syntax tree the way you describe them, node kind by node kind.

readaloud ships the parser engine ([tree-sitter](https://tree-sitter.github.io/)) and no
grammar. A language's grammar comes from the `tree-sitter-<language>` package, prebuilt
for macOS, Linux and Windows, installed next to readaloud:

```bash
uv tool install readaloud --with tree-sitter-python
```

The Homebrew install ships the Python grammar already (the `grammars` extra); for another
language, `brew`'s readaloud needs that language's package installed into its own Python,
or a `parsers` directory. Neovim users may point the `parsers` setting at their `nvim-treesitter/parser` directory
instead and install nothing. The grammar is picked by the file's suffix (`.py` is `python`,
`.rs` is `rust`, and so on, as the `[files]` table of `readaloud.conf` says; the
`language` setting overrides it),
and only a named file (`-f app.py`) has one: piped text has no suffix. Without a grammar,
or with a rule the grammar has no node or field for, the status bar says so and the file is
read without the rules.

The shipped `~/.readaloud/python.conf` (with `format = code` coming from the `code` layer
of `readaloud.conf`, since `[files]` says `.py = code python`):

```ini
[syntax]
function_definition = function $name $parameters $return_type $body
class_definition/function_definition = method $name $parameters $return_type $body
function_definition.return_type = and returns type $0
parameters = takes ${*|,=and}
typed_parameter = ${*|:=type}
typed_default_parameter = $name type $type with a default of $value
type/binary_operator = $left or $right
list_splat_pattern = ${0|*=star}
attribute = $object dot $attribute
decorator = ${0|@=decorator}
comment = ${0|#=}

[pronunciations]
str = string
```

With these, `def for_file(self, path: str | Path | None) -> Config:` is said "function
for_file takes self and path type string or Path or None and returns type Config", with
`def` lit for "function", `self` for "takes self", `Path` for "or Path" and `Config` for
"and returns type Config"; `r1 = a | b` is left as it is, since that `|` is not under a
type. A rule's key is:

- **`kind`**: every node of that kind, as `readaloud --tree` prints them (below).
- **`ancestor/kind`**: such a node anywhere under a node of the first kind, which is how
  "or" is confined to the `|` of a type annotation.
- **`kind.field`**: whatever node is that field of such a node, such as a `def`'s
  `return_type`.

The wording names the node's fields as `$name` or `${name}`: the field's node, said by its
own rule if it has one, else as written with the rules inside it applied. `$0` is the node
in full the same way, and `$*` its named parts in order with the text between them, so
`${*|,=and}` says a list with "and" for each comma; comments among the parts are left out.
`|from=to` replacements apply to the text said as written at that level, between the parts
and a field's own, never inside a part (the comma of a `dict[str, int]` is not a
parameter's comma) and never to a rule's own words. A field left out of the wording is left
unsaid, so a `def`'s rule names `$body` to keep its body. An empty wording says the node as
written and keeps every rule off it.

A file read through a grammar is also coloured by it: keywords, strings, comments, function
and type names, in the terminal's own colours, from the `highlights.scm` query the grammar
package ships (or the one under `queries/<language>/` next to a Neovim `parser` directory).
`highlight = no` under `[config]` or `[config.<ext>]`, or `--no-color`, leaves the file plain.
Colouring needs no `[syntax]` rules: a grammar alone is enough.

### Changing how a piece of code is read

Every rule is a line in `~/.readaloud/python.conf`, the shipped ones included, so a
wording you dislike is yours to change or delete; a language with no file is read as
written. To change how a kind of code is read, find its node kind and write a rule for it:

```bash
readaloud --tree app.py
```

prints the file's syntax tree, one node per line, indented by depth: the field the node
is of its parent, its kind, its byte range, and a leaf's text. For `class A(Base):` it
prints

```
class_definition [0..48]
  class [0..5]  "class"
  name: identifier [6..7]  "A"
  superclasses: argument_list [7..13]
    ( [7..8]  "("
    identifier [8..12]  "Base"
    ) [12..13]  ")"
  body: block [19..48]
```

so a class is a `class_definition` with the fields `name`, `superclasses` and `body`, and
`class_definition = class $name from $superclasses $body` says it that way. Then
`readaloud -f app.py` again. A rule's key can also be the field alone
(`class_definition.superclasses = based on $0`) or a context (`type/binary_operator`), as
above. To have a kind read as written again, delete its line; to keep it as written and
stop deeper rules too, give it an empty wording (`string =`). Neovim's `:InspectTree`
shows the same tree.

The shipped `python.conf` reads a comment without its marker, `comment = ${0|#=}`: `x = 3
# the limit` is "x equals 3 the limit", not "hash the limit", while a `#` inside a string,
which is no comment node, stays. The marker is each language's own (`//` is a comment in
Rust and floor division in Python), so the rule belongs in the language's file, as
`comment = ${0|//=}` would in a `rust.conf`.

What a rule says goes through your entries and patterns once more, like a shape's wording:
a pattern `/__(\w+)__/ = dunder $1` under `[pronunciations]` turns the `__init__` a `def`
rule says into "dunder init". Each spoken word lights the word it came from, as a shape's
does, and a line break between two words is kept, so a body keeps its lines. A `def` whose body runs past the chunk being
read is said as far as the chunk goes. Not in any tree: whether `a | b` is two sets or two
integers; that takes a type checker, not a parser.

### Parts

The shapes are built from parts: named pieces of pattern under `[parts]`, which a shape,
or a part further down, uses as `%{name}`. A fresh file comes with these:

```ini
[parts]
ext = [A-Za-z]\w*
file = \S+\.%{ext}
web = com|org|net|edu|gov|mil|int|info|biz|io|dev|ai|co|me|tv
newweb = cloud|tech|online|site|xyz
europe = de|uk|eu|fr|nl|ch|at|se|no|fi|dk|it|es|pt|pl|cz|be|ie
world = jp|cn|kr|in|us|ca|au|nz|br|ru
private = local|localdomain|lan|home|internal|intranet|corp
reserved = arpa|test|example|invalid
domain = %{web}|%{newweb}|%{europe}|%{world}|%{private}|%{reserved}
name = [A-Za-z][\w-]*(?:\.[A-Za-z][\w-]*)*\.(?i:%{domain})
octet = 25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d
ipv4 = %{octet}\.%{octet}\.%{octet}\.%{octet}
host = localhost|%{ipv4}|%{name}
port = 6553[0-5]|655[0-2]\d|65[0-4]\d\d|6[0-4]\d{3}|[1-5]\d{4}|\d{1,4}
hostport = (?<host>%{host}):(?<port>%{port})
scheme = (?<scheme>https?|ftps?|ssh|git)://
path = [^\s<>"'|)\]]*[^\s<>"'|)\].,;:!?]
hour = [01]?\d|2[0-3]
minute = [0-5]\d
```

They are lines like the shapes: add a domain to `web` or `private`, give `ext` your own
file extensions, or add a part of your own. readaloud has none built in. A part may use
only the parts on the lines above it, so they can never go round in a circle, and a shape
may use any of them. Each `%{name}` is put in as a group of its own, so `%{web}|%{world}`
means one or the other however many `|` each holds. A part may hold named groups of its own, as
`hostport` does, so a shape using it can say `$host` and `$port`; such a part can appear
once in a shape, since a name can only name one group.

## Patterns

For text readaloud has no shape for, put a [regular expression][regex] between slashes on
the left, and on the right say how to read what it matched, naming its groups:

```ini
/#(\d+)\b/ = issue $1           # #123 says "issue 123"
/(\w+\.py) --help/ = $1 help    # main.py --help says "main dot pie help"
```

Like a shape's wording, what a pattern says goes through your entries once more, which is
why the second example says "dot pie"; the entries alone, not the patterns, so a pattern
never reads its own output. What those entries say is final, so nothing is read more than
twice. A pattern takes its turn with the other entries, left to
right, and a shape is only ever a whole word, so a pattern can still reach where a shape
does not.

- **Groups.** `$1`, `$2` and so on are what the pattern's parentheses captured, and `$0`
  is the whole match. Write `${1}` when a letter or digit follows (`${1}st`, since `$1st`
  would look for a group called `1st`), `${name}` for a group written `(?<name>...)`, and
  `$$` for a dollar sign. A group that does not exist, or did not take part in the match,
  says nothing.
- **A pattern matches as written.** None of the rules for text above apply to it: no
  word breaks, no smart case, no accent forms. Say what you mean with the pattern itself:
  `\b` for a word break, `(?i)` to ignore case, `\d` for a digit in any script.
- **It takes turns with the rest.** Reading goes left to right, and where a pattern and
  other entries match at the same place, the longest match wins. A tie counts the pattern
  as text with a capital, and then the later line wins.
- **A match must say something.** One whose groups leave no letter or digit to say is
  passed over, and one that says exactly what it matched is a guard, like `macOS = macOS`.
- **What a pattern can hold.** It is a regular expression for Rust's [`regex`][regex]
  crate, which never backtracks, so no pattern can slow readaloud down however much text
  it reads. That leaves out lookahead and lookbehind (`(?=...)`, `(?<=...)`) and
  backreferences (`\1`). Everything else you would expect is there: classes, `|`,
  `*`, `+`, `?`, `{2,4}`, `^` and `$` at the ends of a chunk.

A pattern that will not compile, or that could match empty text (`/\d*/`), is reported
with its line number and skipped, like any other line readaloud cannot use:

```
readaloud: /Users/you/.readaloud/readaloud.conf: [pronunciations] line 101: the pattern /:(\d+/ cannot be used: unclosed group
```

Text that only looks like a pattern is literal when quoted: `"/usr/" = slash user slash`.
A pattern is matched against the text as it is spoken, which is why it cannot see the
name a table mdcat drew gives a tick, and it cannot reach into one either.

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
readaloud: /Users/you/.readaloud/readaloud.conf: [pronunciations] line 99: 'kubectl' has no "="; write it as: text = how to say it
```

The same text on two lines is reported too, and the later line wins; `id`, `Id` and `ID`
are three different entries. An existing `readaloud.conf` is never touched, so a file
written by an older readaloud has neither the shipped entries nor the `[pronunciations]`
line: add the line above your own entries, since an entry left under `[config]` is
ignored with a warning that it belongs under `[pronunciations]`. To take the shipped ones,
copy them from a fresh file (`readaloud --write-config` writes one, discarding your
edits, so save the file first if you have any), or from
[Configuration](configuration.md#the-file-a-first-run-writes). A fresh file ends
with `[pronunciations]`, so a setting added at the bottom of it lands there: `speed = 1.5`
is then reported as a setting to move under `[config]`, not taken for a word (quote it,
`"speed" = spead`, if you do mean the word). A misspelled heading such as
`[pronunciation]` is ignored with a warning
that names the section it probably meant, and past five problems the rest are counted
rather than listed.

[regex]: https://docs.rs/regex/latest/regex/#syntax

[Back to the README](../README.md)
