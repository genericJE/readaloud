# Pronunciations

The `[pronunciations]` section of `~/.readaloud.conf`: how to say a word or phrase your
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
✓ = check                  # a tick in a -md table says "check", not "yes"
macOS = macOS              # said as written, which keeps "OS" below off it
OS = O S
"#include" = hash include  # quote text that starts with # or ; or a quote
```

A fresh `~/.readaloud.conf` already holds about ninety of these, each one a word Kokoro
was measured saying wrongly: the common file types (`main.py` is "main dot pie",
`config.yml` is "config dot yaml", `README.md` keeps its name instead of being spelled
out), `id` and `ids`, the quotes that stand for nothing (`''` is "empty string", `"""` is
"triple quote"), languages and tools (`C#`, `.NET`, `json`, `YAML`, `systemd`,
`journalctl`, `kubectl`, `k8s`, `redis`, `postgresql`, `PyPI`), and what the punctuation
of code is called (`!=`, `->`, `=>`, `::`, `&&`, `||`). They are ordinary lines in your
file: delete one to be rid of it, or edit it to say it your way. Anything readaloud
already says correctly is deliberately absent, so `SQL` stays "sequel", `nginx` stays
"engine x" and `C++` stays "C plus plus".

How the text on the left is found:

- **It is text, not a pattern.** `C#`, `std::vector` and `.NET` mean exactly those
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
  `id = id card` is safe.

Put quotes, single or double, round text that starts with `#`, `;` or a quote, or that has
an `=` with spaces round it: `"#include" = hash include`, `"a = b" = c`. Without the
quotes, `#include = hash include` is a comment and does nothing. A `#` or `;` after a
space starts a comment, so a pronunciation holding one needs quotes too
(`hash = "number # sign"`), while `C# = C sharp` needs none. There are no escapes.

Pronunciations apply wherever readaloud speaks: prose, code blocks, piped output and the
cells of a `-md` table, in the reader and in a `--save` WAV alike. Only what is said
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

A `-md` table says its ticks and crosses as "yes" and "no", and its arrows and keys by
name (see [Markdown with `-md`](markdown.md)). Those names are not text an entry can
match, so `yes = yep` changes a written "yes" but no tick, and `right arrow = next` leaves
every `→` alone. An entry for the symbol itself replaces its name: with `✓ = check`, a `✓`
cell says "check" and `✓ (partial)` says "check (partial)". Each symbol is an entry of its
own, so that leaves `✅` saying "yes", and an entry reaches a named symbol only when its
text is that one symbol: `⌘C = copy` leaves a `⌘C` cell saying "command C", while
`⌘ = cmd` makes it "cmd C". Outside a table Kokoro says nothing at all for a `✓` beside
other words, and `✓ = check` makes it say "check" there too, lighting the tick while it
does. A line or cell holding nothing but symbols readaloud has no name for is still
skipped, so `★ = star` does nothing for a `★★★★` rating.

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
readaloud: /Users/you/.readaloud.conf: [pronunciations] line 99: 'kubectl' has no "="; write it as: text = how to say it
```

The same text on two lines is reported too, and the later line wins; `id`, `Id` and `ID`
are three different entries. An existing `~/.readaloud.conf` is never touched, so a file
written by an older readaloud has neither the shipped entries nor the `[pronunciations]`
line: add the line above your own entries, since an entry left under `[readaloud]` is
ignored with a warning that it belongs under `[pronunciations]`. To take the shipped ones,
copy them from a fresh template (`readaloud --write-config` writes one, discarding your
edits, so save the file first if you have any). A fresh template ends
with `[pronunciations]`, so a setting added at the bottom of it lands there: `speed = 1.5`
is then reported as a setting to move under `[readaloud]`, not taken for a word (quote it,
`"speed" = spead`, if you do mean the word). A misspelled heading such as
`[pronunciation]` is ignored with a warning
that names the section it probably meant, and past five problems the rest are counted
rather than listed.

[Back to the README](../README.md)
