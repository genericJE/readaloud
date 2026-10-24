"""Shapes and parts: text the config file describes with a pattern and says a
set way.

readaloud knows no shape and no part of its own.  A shape is a
``/pattern/ = wording`` line under [shapes], matched where words start and
end, the first line that fits winning; a part is a ``name = pattern`` line
under [parts] that patterns use as ``%{name}``.  What a shape says goes
through the literal pronunciations once more, so ``.py = dot pie`` still
applies inside it.  The shipped lines are tested here as any user's would be.
"""

from __future__ import annotations

import time

import pytest

from readaloud import config
from readaloud.document import Document
from readaloud.pronounce import Lexicon

SHIPPED = config.TEMPLATE_PRONUNCIATIONS
SHIPPED_SHAPES = config.Config(shapes=config.TEMPLATE_SHAPES,
                               parts=config.TEMPLATE_PARTS).shape_patterns


def said(text: str, pairs=SHIPPED, shapes=SHIPPED_SHAPES
         ) -> tuple[str, list[str]]:
    """What `text` says, and the words on screen of its slots."""
    doc = Document.from_text(text, pronunciations=Lexicon(pairs, shapes))
    (chunk,) = doc.chunks
    return chunk.text, [doc.words[w].text for w in chunk.words]


# ---------------------------------------------------------------------------
# the shipped lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word, spoken", [
    ("settings.py:281-283", "settings dot pie lines 281 to 283"),
    ("settings.py:498", "settings dot pie line 498"),
    ("src/app/main.rs:12:5", "src/app/main dot R S line 12, column 5"),
    ("x.py:0", "x dot pie line 0"),
    ("node.js:20", "node dot J S line 20"),
    ("192.168.178.88:8123", "192.168.178.88 port 8123"),
    ("localhost:8080", "local host port 8080"),
    ("example.com:443", "example dot com port 443"),
    ("EXAMPLE.COM:80", "EXAMPLE dot com port 80"),
    ("www.example.co.uk:8443", "www.example dot co dot U K port 8443"),
    ("https://example.com:8443/status?verbose=1&since=14:35",
     "H T T P S example dot com port 8443 slash status?verbose=1&since=14:35"),
    ("https://example.com:8443/docs/v2/index.html#setup",
     "H T T P S example dot com port 8443 slash docs slash v2 slash index dot "
     "H T M L#setup"),
    ("https://localhost:8443/health", "H T T P S local host port 8443 slash health"),
    ("http://192.168.178.88:8123", "H T T P 192.168.178.88 port 8123"),
    ("ssh://build-01.internal:2222/", "S S H build-01.internal port 2222"),
])
def test_a_shipped_shape_is_said_its_way_and_lit_whole(word, spoken):
    text, words = said(f"See {word} now")
    assert text == f"See {spoken} now"
    assert words == ["See", word, "now"]


@pytest.mark.parametrize("word", [
    "14:35", "9:05:59", "16:9", "25:99", "14:30-15:00", "00:01:23.456",
    "2026-10-19T14:35:00Z", "python:3.12", "node:20", "8080:80",
    "300.1.1.1:80", "localhost:70000", "2001:db8::1",
])
def test_everything_else_is_left_as_written(word):
    text, _words = said(f"At {word} now", pairs=[])
    assert text == f"At {word} now"


def test_a_wording_can_say_each_character_of_a_group_as_a_word():
    shapes = [(r"(?<path>[\w/.-]+)", "${path|/=slash|.=dot}")]
    assert said("a/b.c/d", pairs=[], shapes=shapes)[0] == (
        "a slash b dot c slash d")
    # each replacement is made once, in what the group matched: the s of
    # "slash" is not said again as "ess"
    shapes = [(r"(?<path>[\w/]+)", "${path|/=slash|s=ess}")]
    assert said("s/s", pairs=[], shapes=shapes)[0] == "ess slash ess"


def test_a_pattern_wording_can_replace_too():
    pairs = [(r"/v(\d+(?:\.\d+)+)/", "version ${1|.=point}")]
    assert said("Use v1.2.3 now", pairs=pairs, shapes=())[0] == (
        "Use version 1 point 2 point 3 now")


def test_a_url_says_its_scheme_only_while_its_wording_names_it():
    url = "Open https://localhost:8443/health"
    assert said(url)[0] == "Open H T T P S local host port 8443 slash health"
    shapes = [(p, w.replace("$scheme ", "")) for p, w in SHIPPED_SHAPES]
    assert said(url, shapes=shapes)[0] == (
        "Open local host port 8443 slash health")


def test_a_url_keeps_sentence_punctuation_outside_its_path():
    text, words = said("Open https://localhost:8443/health. Then wait.")
    assert text == "Open H T T P S local host port 8443 slash health. Then wait."
    assert words[1] == "https://localhost:8443/health"


@pytest.mark.parametrize("url", ["https://example.com/status",
                                 "https://example.com:99999/x"])
def test_a_url_without_a_port_is_left_alone(url):
    assert said(f"See {url} now", pairs=[])[0] == f"See {url} now"


def test_bracketed_ipv6_is_two_words_and_left_alone():
    assert said("Try [::1]:8080 now", pairs=[])[0] == "Try [::1]:8080 now"


def test_a_sentence_full_of_them():
    text, _ = said("See src/settings.py:281-283, then main.rs:12:5. It runs "
                   "at 14:35 on localhost:8080, ratio 16:9.")
    assert text == ("See src/settings dot pie lines 281 to 283, then main dot "
                    "R S line 12, column 5. It runs at 14:35 on local host "
                    "port 8080, ratio 16:9.")


def test_a_host_is_tried_before_a_file():
    assert said("a.py:12", pairs=[])[0] == "a.py line 12"
    assert said("a.com:12", pairs=[])[0] == "a.com port 12"


def test_the_shipped_shapes_are_fast_over_a_long_text():
    text = ("See settings.py:281-283 and localhost:8080 at 14:35, then "
            "the rest of an ordinary sentence with many plain words. ") * 2000
    lex = Lexicon(SHIPPED, SHIPPED_SHAPES)
    start = time.perf_counter()
    doc = Document.from_text(text, pronunciations=lex)
    assert time.perf_counter() - start < 5.0
    assert doc.respell_failures == 0
    assert "lines 281 to 283" in doc.chunks[0].text


# ---------------------------------------------------------------------------
# shapes of one's own
# ---------------------------------------------------------------------------


def test_the_first_line_that_fits_wins():
    shapes = [(r"(?<a>\w+)\.py:(?<n>\d+)", "first $n"), (r"\S+:\d+", "second")]
    assert said("x.py:1", pairs=[], shapes=shapes)[0] == "first 1"
    assert said("x.py:1", pairs=[], shapes=shapes[::-1])[0] == "second"


def test_a_shape_may_span_words():
    shapes = [(r"(?<file>\S+\.py)#L(?<line>\d+)", "$file line $line"),
              (r"(?<file>\S+\.py)\((?<line>\d+)\)", "$file line $line"),
              (r'File "(?<file>[^"]+)", line (?<line>\d+)', "$file line $line")]
    pairs = [(".py", "dot pie")]
    assert said("see a.py#L12 now", pairs, shapes)[0] == (
        "see a dot pie line 12 now")
    assert said("see a.py(12) now", pairs, shapes)[0] == (
        "see a dot pie line 12 now")
    # the wording says nothing for "File", so that word says a space
    assert said('at File "x.py", line 3 in', pairs, shapes)[0] == (
        "at  x dot pie line 3 in")


def test_a_shape_starts_and_ends_where_words_do():
    shapes = [(r"py:\d+", "found")]
    assert said("a.py:12", pairs=[], shapes=shapes)[0] == "a.py:12"
    assert said("py:12x", pairs=[], shapes=shapes)[0] == "py:12x"
    assert said("py:12", pairs=[], shapes=shapes)[0] == "found"


def test_an_entry_for_the_whole_text_beats_the_shape():
    pairs = list(SHIPPED) + [("settings.py:498", "the settings")]
    assert said("settings.py:498", pairs=pairs)[0] == "the settings"


def test_an_empty_wording_keeps_a_pattern_off_a_time():
    pairs = [(r"/:(\d+)/", "line $1")]
    assert said("At 14:35 ok", pairs=pairs)[0] == "At 14:35 ok"
    assert said("At 16:9 ok", pairs=pairs)[0] == "At 16 line 9 ok"


def test_a_wording_that_says_nothing_leaves_the_text_alone():
    assert said("a.py:12", pairs=[], shapes=[(r"\S+:\d+", "$$")])[0] == "a.py:12"


def test_no_shapes_is_no_shapes():
    lex = Lexicon()
    assert not lex and lex.shapes == 0
    assert lex.find("a.py:1", slots=[(0, 6)]) == []
    assert Lexicon(shapes=SHIPPED_SHAPES)
    assert Lexicon(shapes=SHIPPED_SHAPES).find("a.py:1") == []   # no slots


def test_a_shape_that_cannot_be_used_is_skipped():
    shapes = [(r"(x", "bad"), (r"\d*", "empty"), (r"\d+", "number")]
    assert said("7", pairs=[], shapes=shapes)[0] == "number"


def test_a_patterns_wording_goes_through_the_entries_too():
    pairs = [(".py", "dot pie"), (r"/(\w+\.py)!/", "run $1")]
    assert said("go main.py! now", pairs=pairs, shapes=())[0] == (
        "go run main dot pie now")


def test_the_second_pass_is_the_literal_entries_only():
    pairs = [(r"/a/", "b"), (r"/b/", "c")]
    assert said("a", pairs=pairs, shapes=())[0] == "b"


# ---------------------------------------------------------------------------
# the config file
# ---------------------------------------------------------------------------


def load(tmp_path, body: str):
    path = tmp_path / "c.conf"
    path.write_text(body, encoding="utf-8")
    return config.load(path)


def test_no_sections_is_no_shapes_and_no_parts(tmp_path):
    cfg, warnings = load(tmp_path, "[config]\nspeed = 1.5\n")
    assert (cfg.shapes, cfg.parts, warnings) == ((), (), [])
    assert config.Config().shapes == () and config.Config().parts == ()


def test_shapes_and_parts_come_from_the_file(tmp_path):
    cfg, warnings = load(tmp_path, "[shapes]\n"
                                   "/(?<f>%{file}):(?<n>\\d+)/ = $f at $n  # c\n"
                                   "/\\d\\d:\\d\\d/ =\n"
                                   "[parts]\next = py|rs\nfile = \\S+\\.(?:%{ext})\n")
    assert warnings == []
    assert cfg.parts == (("ext", "py|rs"), ("file", r"\S+\.(?:%{ext})"))
    assert cfg.shapes == ((r"/(?<f>%{file}):(?<n>\d+)/", "$f at $n"),
                          (r"/\d\d:\d\d/", ""))
    assert cfg.shape_patterns == (
        (r"(?<f>(?:\S+\.(?:(?:py|rs)))):(?<n>\d+)", "$f at $n"),
        (r"\d\d:\d\d", ""))
    lex = Lexicon([], cfg.shape_patterns)
    doc = Document.from_text("see a.rs:4 and a.go:4", pronunciations=lex)
    assert doc.chunks[0].text == "see a.rs at 4 and a.go:4"


def test_a_part_uses_only_the_parts_above_it(tmp_path):
    cfg, warnings = load(tmp_path, "[parts]\nb = %{a}x\na = y\nc = %{a}z\n")
    assert cfg.parts == (("a", "y"), ("c", "%{a}z"))
    assert [w.split(": ", 1)[1] for w in warnings] == [
        "[parts] line 2: 'b' uses %{a}, which is not a part on a line above it"]


def test_a_line_that_makes_no_sense_costs_only_itself(tmp_path):
    cfg, warnings = load(tmp_path, "[parts]\nd = \\d\n2x = y\nbad = (x\nnone =\n"
                                   "[shapes]\n"
                                   "file line = $file\n"
                                   "/%{nope}/ = x\n"
                                   "/(x/ = x\n"
                                   "/\\d*/ = x\n"
                                   "/(?<n>%{d}+)/ = $m\n"
                                   "/(?<n>%{d}+)/ = --\n"
                                   "nothing\n"
                                   "/(?<n>%{d}+)/ = number $n\n"
                                   "/(?<n>%{d}+)/ = digits ${n}\n")
    assert cfg.parts == (("d", r"\d"),)
    assert cfg.shapes == ((r"/(?<n>%{d}+)/", "digits ${n}"),)
    problems = [w.split(": ", 1)[1] for w in warnings]
    assert problems[:3] == [
        "[parts] line 3: '2x' is not a name: use letters, digits and _, "
        "starting with a letter",
        "[parts] line 4: 'bad' cannot be used: unclosed group",
        "[parts] line 5: 'none' has no pattern",
    ]
    assert problems[3:5] == [
        "[shapes] line 7: 'file line' is not a pattern between slashes",
        "[shapes] line 8: /%{nope}/ uses %{nope}, which is not a part",
    ]
    assert problems[5].startswith("[shapes] line 9: the shape /(x/ cannot be used:")
    # five problems a section, the rest counted: seven bad lines and the
    # last one, which repeats the line above it
    assert len(problems) == 3 + 5 + 1
    assert problems[-1] == "[shapes]: 3 more problems not shown"


def test_each_shape_problem_says_what_is_wrong(tmp_path):
    for line, problem in [
        ("/\\d*/ = x", "the shape /\\d*/ cannot be used: it can match empty text"),
        ("/(?<n>\\d+)/ = $m", "/(?<n>\\d+)/ has no group $m; its named groups "
                              "are $n, and $0 is the whole match"),
        ("/(?<n>\\d+)/ = --", "'--' has nothing to say; leave it empty to say "
                              "the text as written"),
        ("nothing", "'nothing' has no \"=\"; write it as: /pattern/ = wording"),
    ]:
        cfg, warnings = load(tmp_path, f"[shapes]\n{line}\n")
        assert cfg.shapes == ()
        assert warnings == [f"{tmp_path / 'c.conf'}: [shapes] line 2: {problem}"]


def test_a_replacement_names_a_group_and_says_text_as_a_word(tmp_path):
    cfg, warnings = load(tmp_path, "[shapes]\n/(?<p>/\\w+)/ = ${p|/=slash|_=under}\n")
    assert warnings == [] and len(cfg.shapes) == 1
    for wording, problem in [
        ("${q|/=slash}", "has no group $q"),
        ("${p|/slash}", "write a replacement as |text=word"),
        ("${p|=slash}", "write a replacement as |text=word"),
    ]:
        cfg, warnings = load(tmp_path, f"[shapes]\n/(?<p>/\\w+)/ = {wording}\n")
        assert cfg.shapes == () and problem in warnings[0], (wording, warnings)


def test_numbered_groups_and_the_whole_match_are_allowed(tmp_path):
    cfg, warnings = load(tmp_path, "[shapes]\n/(\\d+)-(\\d+)/ = $1 to $2, $0\n")
    assert warnings == [] and len(cfg.shapes) == 1
    _, warnings = load(tmp_path, "[shapes]\n/(\\d+)/ = $2\n")
    assert "has no group $2" in warnings[0]


def test_the_same_shape_twice_keeps_the_later(tmp_path):
    cfg, warnings = load(tmp_path, "[shapes]\n/\\d+/ = a\n/x+/ = b\n/\\d+/ = c\n")
    assert cfg.shapes == ((r"/x+/", "b"), (r"/\d+/", "c"))
    assert warnings[0].endswith("[shapes] line 4: /\\d+/ is also on line 2; "
                                "line 4 wins")


# ---------------------------------------------------------------------------
# named shapes
# ---------------------------------------------------------------------------


DEF = r"""[parts]
ident = [A-Za-z_]\w*

[shapes]
dflt: /=\s*(?<v>[^,)]+)/ = with a default of $v
param: /(?<name>%{ident})\s*(?<type>:\s*[^,=)]+)?\s*(?<default>=\s*[^,)]+)?/ = $name ${default|dflt} ${type|:=type}
/def\s+(?<fn>%{ident})\s*\((?<params>[^()]*?),?\s*\)\s*(?<ret>->\s*[^:]+)?:/ = function $fn takes ${params|param|,=and} ${ret|->=and returns a}

[pronunciations]
"''" = empty string
str = string
"""

SAID_DEF = ("function foo takes bar type int and baz with a default of empty "
            "string type string and returns a string")


def _said_with(lex, text: str) -> tuple[str, list[str]]:
    doc = Document.from_text(text, pronunciations=lex)
    (chunk,) = doc.chunks
    return chunk.text, [doc.words[w].text for w in chunk.words]


def said_by(cfg, text: str) -> tuple[str, list[str]]:
    """What `text` says with the shapes, named ones too, of `cfg`."""
    return _said_with(Lexicon(cfg.pronunciations, cfg.shape_patterns,
                              cfg.sub_patterns), text)


def test_a_named_shape_says_each_match_in_a_group(tmp_path):
    cfg, warnings = load(tmp_path, DEF)
    assert warnings == []
    assert [name for name, _p, _w in cfg.named_shapes] == ["dflt", "param"]
    assert len(cfg.shapes) == 1
    text, words = said_by(cfg, "def foo(bar: int, baz: str='') -> str:")
    assert text == SAID_DEF
    assert words == ["def", "foo", "bar", "int", "baz", "str", "str"]


def test_each_spoken_word_lights_the_word_it_came_from(tmp_path):
    cfg, _ = load(tmp_path, DEF)
    # "takes" comes from the "(" before bar, "type" from the ":" before its
    # type, "and" from the "," before baz; a default has no word of its own,
    # so it goes with the word before it; "->" is no word, so "and returns"
    # goes with the type after it
    text, _ = said_by(cfg, "def foo(bar: int, baz: str='') -> str:")
    doc = Document.from_text("def foo(bar: int, baz: str='') -> str:",
                             pronunciations=Lexicon(cfg.pronunciations,
                                                    cfg.shape_patterns,
                                                    cfg.sub_patterns))
    assert doc.chunks[0].word_texts == [
        "function", "foo", "takes bar", "type int", "and baz",
        "with a default of empty string type string", "and returns a string"]
    assert text == " ".join(doc.chunks[0].word_texts)
    doc = Document.from_text(
        "def for_file(self, path: str | Path | None) -> Config:",
        pronunciations=Lexicon(cfg.pronunciations, cfg.shape_patterns,
                               cfg.sub_patterns))
    assert doc.chunks[0].word_texts == [
        "function", "for_file", "takes self", "and path", "type string",
        "| Path", "| None", "and returns a Config"]


def test_a_wording_out_of_the_text_order_keeps_its_order(tmp_path):
    cfg, _ = load(tmp_path, "[shapes]\n/(?<f>[a-z]+) (?<n>\\d+)/ = $n then $f\n")
    doc = Document.from_text("see a 12 now", pronunciations=Lexicon(
        (), cfg.shape_patterns))
    # "a" cannot go back to the word before "12", so "a" says nothing
    assert doc.chunks[0].word_texts == ["see", " ", "12 then a", "now"]
    assert doc.chunks[0].text == "see  12 then a now"
    # (that slot says one space, which stands in for the "a " it covered)


def test_a_def_spread_over_lines(tmp_path):
    cfg, _ = load(tmp_path, DEF)
    signature = "def foo(\n    bar: int,\n    baz: str = '',\n) -> str:"
    assert said_by(cfg, signature)[0] == SAID_DEF


def test_a_named_shape_is_not_found_on_its_own(tmp_path):
    cfg, _ = load(tmp_path, DEF)
    assert said_by(cfg, "bar: int")[0] == "bar: int"


def test_a_group_that_did_not_take_part_says_nothing(tmp_path):
    cfg, _ = load(tmp_path, DEF)
    assert said_by(cfg, "def go():")[0] == "function go takes"
    assert said_by(cfg, "def f(a, *args):")[0] == "function f takes a and * args"


def test_a_named_shape_fills_a_pattern_with_its_groups_unnamed(tmp_path):
    cfg, warnings = load(tmp_path, r"""[shapes]
n: /(?<d>\d+)/ = number $d
/(?<a>%{n})\+%{n}/ = $a plus another
""")
    assert warnings == []
    assert cfg.shape_patterns == ((r"(?<a>(?:(?:\d+)))\+(?:(?:\d+))",
                                   "$a plus another"),)
    assert cfg.sub_patterns == (("n", r"(?<d>\d+)", "number $d"),)
    assert said_by(cfg, "2+3")[0] == "2 plus another"


def test_a_pattern_may_say_a_group_through_a_named_shape():
    lex = Lexicon([(r"/\[(?<l>[^\]]+)\]/", "list of ${l|n|,=and}")], (),
                  [("n", r"\d+", "number $0")])
    assert _said_with(lex, "see [1, 2]")[0] == "see list of number 1 and number 2"


def test_a_wording_may_name_only_a_shape_above_it(tmp_path):
    above = "a: /x/ = ex\nb: /y/ = why\n"
    for line, problem in [
        ("/(?<p>\\w+)/ = ${p|c}", "'${p|c}' names no shape above it"),
        ("/(?<p>\\w+)/ = ${p|a|b}", "'${p|a|b}' names two shapes; use one"),
        ("/(?<p>\\w+)/ = ${p|a|=x}", "write a replacement as |text=word"),
    ]:
        cfg, warnings = load(tmp_path, f"[shapes]\n{above}{line}\n")
        assert cfg.shapes == () and problem in warnings[0], (line, warnings)
    cfg, warnings = load(tmp_path, "[shapes]\n/(?<p>\\w+)/ = ${p|a}\n" + above)
    assert cfg.shapes == () and "'${p|a}' names no shape above it" in warnings[0]


def test_a_named_shape_is_checked_like_any_shape(tmp_path):
    cfg, warnings = load(tmp_path, "[parts]\nx = a\n[shapes]\nx: /b/ = bee\n"
                                   "e: /\\d*/ = n\nn: /\\d/ = one\nn: /\\d+/ = more\n")
    assert [name for name, _p, _w in cfg.named_shapes] == ["n"]
    assert cfg.named_shapes[0][1] == r"/\d+/"
    at = f"{tmp_path / 'c.conf'}: [shapes] line"
    assert warnings == [
        f"{at} 4: 'x' is also a part; give the shape another name",
        f"{at} 5: the shape /\\d*/ cannot be used: it can match empty text",
        f"{at} 7: 'n' is also on line 6; line 7 wins",
    ]


def test_named_shapes_cost_nothing_when_unused():
    assert config.Config().named_shapes == () and config.Config().sub_patterns == ()


def test_shapes_are_fast_over_a_long_code_text(tmp_path):
    import time
    cfg, _ = load(tmp_path, DEF)
    lex = Lexicon(cfg.pronunciations, cfg.shape_patterns, cfg.sub_patterns)
    body = "\n\n".join(
        f"def f{i}(a{i}: int, b: str = 'x', *args, **kw) -> dict:\n"
        f"    return {{'a': a{i}, 'b': b}}  # see x.py:{i}"
        for i in range(200))
    started = time.perf_counter()
    doc = Document.from_text(body, pronunciations=lex)
    assert time.perf_counter() - started < 5.0
    assert doc.respell_failures == 0
    assert doc.chunks[0].text.startswith("function f0 takes a0 type int and b")


def test_the_template_ships_every_part_and_shape(tmp_path):
    text = config.template()
    head, _, tail = text.partition("\n[parts]\n")
    assert "\n[pronunciations]\n" not in head
    assert tail.index("\n[shapes]\n") < tail.index("\n[pronunciations]\n")
    for name, pattern in config.TEMPLATE_PARTS:
        assert f"\n{name} = {pattern}\n" in tail
    for pattern, say in config.TEMPLATE_SHAPES:
        assert f"\n{pattern} = {say}".rstrip() + "\n" in tail
    cfg, warnings = load(tmp_path, text)
    assert warnings == []
    assert (cfg.parts, cfg.shapes) == (config.TEMPLATE_PARTS,
                                       config.TEMPLATE_SHAPES)


def test_deleting_lines_takes_shapes_and_parts_away(tmp_path):
    text = config.template()
    for pattern, say in config.TEMPLATE_SHAPES:
        if "%{hostport}" in pattern:
            text = text.replace(f"\n{pattern} = {say}\n", "\n")
    cfg, warnings = load(tmp_path, text)
    assert warnings == []
    assert said("on localhost:8080 and a.py:1",
                shapes=cfg.shape_patterns)[0] == (
        "on local host:8080 and a dot pie line 1")
    # a part every shape needs: those shapes go, and say why
    text = config.template().replace("\next = [A-Za-z]\\w*\n", "\n")
    cfg, warnings = load(tmp_path, text)
    assert "uses %{ext}, which is not a part on a line above it" in warnings[0]
