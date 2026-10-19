"""readaloud.pronounce in Rust: the literal keys against the Python it
replaced, and the patterns (``/:(\\d+)/ = line $1``) against a slow matcher
that is obviously right.

`tests/test_pronounce.py` still holds the rules of the literal keys; this file
adds the patterns and checks the port.
"""

from __future__ import annotations

import random
import re
import string
import time
import unicodedata

import pytest

from readaloud import config
from readaloud.document import Document
from readaloud.pronounce import Lexicon, Literal, check_pattern, respell
from reference import pronounce as reference

LINES = [(".py", "dot pie"),
         (r"/:(\d+)-(\d+)/", "lines $1 to $2"),
         (r"/:(\d+)/", "line $1")]


def said(pairs, text: str) -> str:
    """`text` respelled as one slot."""
    lex = Lexicon(pairs)
    return respell(text, [(0, len(text))], lex.find(text))[0]


def found(pairs, text: str) -> list[tuple[str, str]]:
    return [(text[s:e], say) for s, e, say in Lexicon(pairs).find(text)]


# ---------------------------------------------------------------------------
# patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, spoken", [
    ("settings.py:281-283", "settings dot pie lines 281 to 283"),
    ("settings.py:498", "settings dot pie line 498"),
    ("main.py:7-9,", "main dot pie lines 7 to 9,"),
    ("settings.py", "settings dot pie"),
])
def test_a_line_number_is_said_as_a_line(text, spoken):
    assert said(LINES, text) == spoken


def test_a_line_range_in_a_sentence_keeps_its_word_lit():
    doc = Document.from_text("See settings.py:281-283 for it.",
                             pronunciations=Lexicon(LINES))
    (chunk,) = doc.chunks
    assert chunk.text == "See settings dot pie lines 281 to 283 for it."
    assert chunk.word_texts == ["See", "settings dot pie lines 281 to 283",
                                "for", "it"]
    assert [doc.words[w].text for w in chunk.words] == [
        "See", "settings.py:281-283", "for", "it"]


def test_the_longest_pattern_wins():
    assert found(LINES[1:], "x:12-14") == [(":12-14", "lines 12 to 14")]


def test_a_pattern_takes_its_turn_in_the_scan():
    # "::" starts first and takes the colon the pattern would have begun with
    assert found([("::", "colon colon"), (r"/:(\d+)/", "line $1")],
                 "a::123 b:4") == [("::", "colon colon"), (":4", "line 4")]


def test_a_pattern_ranks_as_a_key_with_a_capital():
    pairs = [("id", "eye dee"), (r"/id/", "I D")]
    assert found(pairs, "id") == [("id", "I D")]
    assert found(list(reversed(pairs)), "id") == [("id", "I D")]
    # a later key with a capital wins the tie
    assert found([(r"/id/", "I D"), ("Id", "ident")], "Id") == [("Id", "ident")]


def test_a_pattern_matches_as_written():
    assert found([(r"/id/", "ID")], "idle Id") == [("id", "ID")]
    assert found([(r"/(?i)\bid\b/", "ID")], "idle Id") == [("Id", "ID")]


@pytest.mark.parametrize("template, spoken", [
    ("$1 and ${2}", "a and b"),
    ("${first} then ${second}", "a then b"),
    ("cost $$1", None),            # "$$" is a dollar sign: says "cost $1"
    ("$9 x", "x"),                 # a group that does not exist says nothing
])
def test_a_pronunciation_names_what_the_pattern_captured(template, spoken):
    pairs = [(r"/(?<first>a)-(?<second>b)/", template)]
    if spoken is None:
        assert found(pairs, "a-b") == [("a-b", "cost $1")]
    else:
        assert found(pairs, "a-b") == [("a-b", spoken)]


def test_a_match_that_says_nothing_is_passed_over():
    # the group holds no letter or digit, so the shorter pattern applies
    assert found([(r"/x(-+)/", "$1"), (r"/x/", "ex")], "x--") == [("x", "ex")]


def test_a_match_said_as_written_is_a_guard():
    assert found([(r"/macOS/", "macOS"), ("OS", "O S")], "macOS OS") == [
        ("OS", "O S")]


def test_a_pattern_never_reaches_into_a_symbols_name():
    text = "1 yes"
    pairs = [(r"/\d \w+/", "X")]
    assert Lexicon(pairs).find(text, [(2, 5, "✓")]) == []
    assert Lexicon(pairs).find(text) == [(0, 5, "X")]


def test_a_pattern_that_cannot_be_used_is_skipped():
    for bad in (r"/:(\d+/", r"/\d*/", r"/(?<=a)b/"):
        assert found([(bad, "x"), ("b", "bee")], "a b:1") == [("b", "bee")]


def test_a_literal_that_looks_like_a_pattern():
    assert found([(Literal("/usr/"), "user")], "a /usr/ b") == [
        ("/usr/", "user")]
    assert found([("/usr/", "user")], "a /usr/ b") == [("usr", "user")]


def test_check_pattern():
    assert check_pattern(r":(\d+)") is None
    assert "unclosed" in check_pattern(r":(\d+")
    assert check_pattern(r"\d*") == "it can match empty text"
    assert "look-around" in check_pattern(r"(?<=a)b")


def test_patterns_count_in_the_length():
    assert len(Lexicon(LINES)) == 3
    assert Lexicon([(r"/(/", "x")]).__len__() == 0


# ---------------------------------------------------------------------------
# the config file
# ---------------------------------------------------------------------------


def load(tmp_path, body: str):
    path = tmp_path / "c.conf"
    path.write_text("[pronunciations]\n" + body, encoding="utf-8")
    return config.load(path)


def test_the_config_reads_a_pattern_as_written(tmp_path):
    cfg, warnings = load(tmp_path, "/:(\\d+)  -  (\\d+)/ = lines $1 to $2\n")
    assert warnings == []
    assert cfg.pronunciations == ((r"/:(\d+)  -  (\d+)/", "lines $1 to $2"),)


def test_the_config_reports_a_pattern_it_cannot_use(tmp_path):
    cfg, warnings = load(tmp_path, "id = ID\n/:(\\d+/ = line $1\n/x*/ = y\n")
    assert cfg.pronunciations == (("id", "ID"),)
    assert len(warnings) == 2
    assert "line 3: the pattern /:(\\d+/ cannot be used: " in warnings[0]
    assert "unclosed group" in warnings[0]
    assert warnings[1].endswith(
        "line 4: the pattern /x*/ cannot be used: it can match empty text")


def test_a_pattern_may_say_nothing_but_a_group(tmp_path):
    cfg, warnings = load(tmp_path, "/#(\\d+)/ = $1\n/#/ = $$\n")
    assert cfg.pronunciations == ((r"/#(\d+)/", "$1"),)
    assert "has nothing to say" in warnings[0]


def test_a_quoted_pattern_is_literal_text(tmp_path):
    cfg, warnings = load(tmp_path, '"/usr/" = user\n')
    assert warnings == []
    ((key, _say),) = cfg.pronunciations
    assert isinstance(key, Literal)
    assert said(cfg.pronunciations, "/usr/") == "user"


# ---------------------------------------------------------------------------
# differential: the port against the Python it replaced
# ---------------------------------------------------------------------------


DIFF_TEXT = ["a", "b", "A", "B", "i", "d", "I", "D", "x", "X", "1", "_", ".",
             "-", "#", "(", ")", "=", " ", "  ", "\n", "\t", "│", "é", "É",
             "é", "́", "✓", "ab", "Id", "id", "iD", ":", "12",
             "userId"]
DIFF_KEY = ["a", "b", "A", "B", "i", "d", "I", "x", "1", "_", ".", "-", "#",
            " ", "é", "É", "é", "✓", "id", "ab", "Id", ":"]


def random_case(rng: random.Random):
    text = "".join(rng.choice(DIFF_TEXT) for _ in range(rng.randint(0, 24)))
    pairs = []
    for _ in range(rng.randint(1, 6)):
        if text.strip() and rng.random() < 0.5:
            a = rng.randrange(len(text))
            key = text[a:a + rng.randint(1, 6)]
        else:
            key = "".join(rng.choice(DIFF_KEY) for _ in range(rng.randint(1, 4)))
        pairs.append((key, rng.choice(["X", "yes", "a b", "ID", key])))
    return text, pairs


def random_slots(rng: random.Random, text: str) -> list[tuple[int, int]]:
    cuts = sorted(rng.sample(range(len(text) + 1),
                             min(len(text) + 1, 2 * rng.randint(0, 4))))
    return [(a, b) for a, b in zip(cuts[::2], cuts[1::2]) if a < b]


def test_find_agrees_with_the_python_it_replaced():
    rng = random.Random(20261019)
    for _ in range(4000):
        text, pairs = random_case(rng)
        assert (Lexicon(pairs).find(text)
                == reference.Lexicon(pairs).find(text)), (pairs, text)


def test_respell_agrees_with_the_python_it_replaced():
    rng = random.Random(7)
    for _ in range(4000):
        text, pairs = random_case(rng)
        matches = reference.Lexicon(pairs).find(text)
        slots = random_slots(rng, text)
        try:
            want = reference.respell(text, slots, matches)
        except Exception:  # noqa: BLE001 - the slots do not fit the matches
            continue
        assert respell(text, slots, matches) == want, (text, slots, matches)


# ---------------------------------------------------------------------------
# differential: patterns among literal keys, against a slow matcher
# ---------------------------------------------------------------------------


def _alnum(ch: str) -> bool:
    return bool(ch) and (ch.isalnum() or 0x300 <= ord(ch) <= 0x36F)


def _break(text: str, i: int) -> bool:
    if i == 0 or i == len(text):
        return True
    left, right = text[i - 1], text[i]
    return (not _alnum(left) or not _alnum(right)
            or (left in string.ascii_lowercase
                and right in string.ascii_uppercase))


def _literal_at(form: str, exact: bool, text: str, p: int) -> int | None:
    if _alnum(form[0]) and not _break(text, p):
        return None
    i = p
    for n, part in enumerate(form.split()):
        if n:
            j, breaks = i, 0
            while j < len(text) and (text[j].isspace()
                                     or (breaks and text[j] == "│")):
                breaks += text[j] == "\n"
                j += 1
            if j == i or breaks > 1:
                return None
            i = j
        piece = text[i:i + len(part)]
        if piece != part and (exact or piece.lower() != part.lower()):
            return None
        i += len(part)
    if _alnum(form[-1]) and not _break(text, i):
        return None
    return i


def _expand(template: str, m: re.Match[str]) -> str:
    """The Rust regex crate's $1 / ${name} / $$, with Python's re."""
    def group(g: re.Match[str]) -> str:
        if g.group(0) == "$$":
            return "$"
        name = g.group(1) or g.group(2)
        ref = int(name) if name.isdigit() else name
        try:
            return m.group(ref) or ""
        except (IndexError, error):
            return ""
    error = re.error
    return re.sub(r"\$\$|\$\{(\w+)\}|\$(\w+)", group, template)


def second_pass(pairs, said: str) -> str:
    """What a pattern says, through the literal entries once more."""
    literal = [(w, s) for w, s in pairs
               if not (len(w) >= 3 and w[0] == w[-1] == "/")]
    matches = reference_find(literal, said)
    if not matches:
        return said
    return reference.respell(said, [(0, len(said))], matches)[0]


def reference_find(pairs, text: str, symbols=()) -> list[tuple[int, int, str]]:
    """`Lexicon.find` the slow way: every entry at every position."""
    entries = []
    for rank, (written, spoken) in enumerate(pairs):
        say = " ".join(spoken.split())
        if not say:
            continue
        if len(written) >= 3 and written[0] == written[-1] == "/":
            entries.append(("re", re.compile(written[1:-1]), say, rank))
            continue
        key = " ".join(unicodedata.normalize("NFC", written).split())
        if key:
            entries.append(("lit", key, say, rank))
    out = []
    p = 0
    while p < len(text):
        best = None
        for kind, key, say, rank in entries:
            if kind == "re":
                m = key.match(text, p)
                if m is None or m.end() == p:
                    continue
                end, exact = m.end(), True
                said = " ".join(_expand(say, m).split())
                if not any(_alnum(ch) for ch in said):
                    continue
                guard = said == text[p:end]
                if not guard:
                    said = second_pass(pairs, said)
            else:
                exact = any(ch.isupper() for ch in key)
                end = None
                for form in (key, unicodedata.normalize("NFD", key)):
                    end = _literal_at(form, exact, text, p)
                    if end is not None:
                        break
                if end is None:
                    continue
                said, guard = say, key == say
            if any(p < b and a < end for a, b, _symbol in symbols):
                continue
            if best is None or (end, exact, rank) > best[0]:
                best = ((end, exact, rank), said, guard)
        if best is None:
            p += 1
            continue
        end, said, guard = best[0][0], best[1], best[2]
        if not guard and text[p:end] != said:
            out.append((p, end, said))
        p = end
    return out


PATTERNS = [r":(\d+)", r":(\d+)-(\d+)", r"\d+", r"[a-z]+", r"(?i)id",
            r"x+y?", r"\.(a|b)", r"[A-Z][a-z]", r"(a)|(b)", r"_+"]
TEMPLATES = ["line $1", "lines ${1} to ${2}", "N", "$0", "$$", "${1}${2} q",
             "$2"]


def test_patterns_among_literal_keys_agree_with_the_slow_matcher():
    rng = random.Random(281283)
    matched = 0
    for _ in range(4000):
        text, pairs = random_case(rng)
        for _ in range(rng.randint(1, 3)):
            pairs.insert(rng.randint(0, len(pairs)),
                         ("/" + rng.choice(PATTERNS) + "/",
                          rng.choice(TEMPLATES)))
        want = reference_find(pairs, text)
        assert Lexicon(pairs).find(text) == want, (pairs, text)
        matched += any(re.match(r"\d|line|N|q", s) for _a, _b, s in want)
    assert matched > 500


# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------


def test_find_with_patterns_is_fast_over_a_long_text():
    rng = random.Random(50)
    words = ["".join(rng.choice(string.ascii_lowercase)
                     for _ in range(rng.randint(2, 9))) for _ in range(3000)]
    pairs = [(rng.choice(words), "x y") for _ in range(500)]
    pairs += [(f"/{rng.choice(words)}:(\\d+)/", "w line $1")
              for _ in range(48)] + LINES[1:]
    parts, size = [], 0
    while size < 300_000:
        part = rng.choice(words)
        if rng.random() < 0.1:
            part += f".py:{rng.randint(1, 999)}"
            if rng.random() < 0.5:
                part += f"-{rng.randint(1000, 1999)}"
        part += rng.choice([" ", "\n", ", "])
        parts.append(part)
        size += len(part)
    text = "".join(parts)
    lex = Lexicon(pairs)
    start = time.perf_counter()
    found_ = lex.find(text)
    assert time.perf_counter() - start < 2.0
    assert sum(say.startswith("lines") for _s, _e, say in found_) > 500


def test_a_pattern_is_fast_on_hostile_text():
    lex = Lexicon([(r"/(a+)+b/", "x"), (r"/:(\d+)-(\d+)/", "y $1"),
                   ("New York", "NY")])
    for text in ("a" * 50_000, ":1-" * 30_000, "New " * 25_000):
        start = time.perf_counter()
        lex.find(text)
        assert time.perf_counter() - start < 2.0
