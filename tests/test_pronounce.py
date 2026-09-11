"""Tests for `readaloud.pronounce` -- the pronunciations from the config file.

Two properties matter more than any single case:

1. `Lexicon.find` finds what the rules say and nothing else.  Besides the
   tables below, it is checked against `reference_find`, a slow matcher that
   tries every key at every position the obvious way, over thousands of
   random texts and lexicons.
2. `respell` never hands the Engine a broken chunk.  Whatever the slots and
   the matches, every slot comes back non-empty, in order and slice equal,
   and the new text is the old one with the respellings in place plus nothing
   but the spaces that keep a respelling off its neighbours.
"""

from __future__ import annotations

import random
import re
import string
import time
import unicodedata

import pytest

from readaloud.pronounce import Lexicon, respell

ACUTE = "\N{COMBINING ACUTE ACCENT}"
VS15 = "\N{VARIATION SELECTOR-15}"
VS16 = "\N{VARIATION SELECTOR-16}"


def found(pairs, text: str, symbols=()) -> list[tuple[str, str]]:
    """What `find` matched in `text`, as (text matched, how to say it)."""
    return [(text[s:e], say)
            for s, e, say in Lexicon(pairs).find(text, symbols)]


def slots(text: str, *words: str) -> list[tuple[int, int]]:
    """The spans of `words` in `text`, each searched after the one before."""
    out, at = [], 0
    for word in words:
        start = text.index(word, at)
        out.append((start, start + len(word)))
        at = start + len(word)
    return out


def said(pairs, text: str, *words: str) -> tuple[str, list[str]]:
    """`text` respelled with `pairs`, with a slot on each of `words`."""
    spans = slots(text, *words)
    matches = Lexicon(pairs).find(text)
    new, offsets, texts = respell(text, spans, matches)
    check_respelled(text, spans, matches, (new, offsets, texts))
    return new, texts


def alnum(ch: str) -> bool:
    """A letter or digit or a combining mark, as the reader counts them."""
    return bool(ch) and (ch.isalnum() or 0x300 <= ord(ch) <= 0x36F)


def check_respelled(text, spans, matches, result) -> None:
    """Every guarantee `respell` makes, checked at once."""
    new, offsets, texts = result
    assert len(offsets) == len(texts) == len(spans)
    end = 0
    for (a, b), o, t in zip(spans, offsets, texts):
        assert t, f"slot {(a, b)} of {text!r} came back empty: {texts}"
        assert o >= end, f"slots of {text!r} out of order: {offsets}"
        assert new[o:o + len(t)] == t
        end = o + len(t)
        if not any(s < b and a < e for s, e, _say in matches):
            assert t == text[a:b], "a slot no match touches changed"
    # the new text is the text between the matches and the respellings, with
    # at most one space before a respelling and spaces after it
    pattern, cursor = "", 0
    for s, e, say in matches:
        pattern += re.escape(text[cursor:s]) + " ?(" + re.escape(say) + ") *"
        cursor = e
    pattern += re.escape(text[cursor:])
    m = re.fullmatch(pattern, new, re.DOTALL)
    assert m, (text, matches, new)
    # and a respelling never runs into a letter or digit beside it
    for g, (_s, _e, say) in enumerate(matches, 1):
        before, after = new[m.start(g) - 1:m.start(g)], new[m.end(g):][:1]
        assert not (alnum(before) and alnum(say[0])), (text, matches, new)
        assert not (alnum(after) and alnum(say[-1])), (text, matches, new)


# ---------------------------------------------------------------------------
# the lexicon
# ---------------------------------------------------------------------------


def test_an_empty_lexicon_is_falsy_and_finds_nothing():
    lex = Lexicon()
    assert not lex and len(lex) == 0
    assert lex.find("id foo.id") == []


def test_pairs_with_an_empty_side_are_skipped():
    lex = Lexicon([("", "x"), ("x", ""), ("  ", " \t"), ("id", "ID")])
    assert lex and len(lex) == 1
    assert lex.find("an id") == [(3, 5, "ID")]


def test_spaces_in_a_pair_are_collapsed():
    assert found([("  New \t York ", " the  big   apple ")],
                 "New York") == [("New York", "the big apple")]


# ---------------------------------------------------------------------------
# boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,match", [
    ("id", "id"),
    ("foo.id", "id"),
    ("user_id", "id"),
    ("foo-id", "id"),
    ("/id", "id"),
    ("?id=", "id"),
    ("userId", "Id"),
    ("getId", "Id"),
    ("idToken", "id"),
    ("(id)", "id"),
    ("id's", "id"),
    ("an iD", "iD"),
    ("Id", "Id"),
])
def test_a_word_matches_on_its_own_and_as_part_of_a_name(text, match):
    assert found([("id", "ID")], text) == [(match, "ID")]


@pytest.mark.parametrize("text", [
    "idle", "grid", "ids", "IDs", "id2", "userid", "IDToken", "Kid", "2id",
])
def test_a_word_never_matches_inside_another(text):
    assert found([("id", "ID")], text) == []


def test_a_key_edge_that_is_a_symbol_needs_no_break():
    assert found([(".NET", "dot net")], "ASP.NET") == [(".NET", "dot net")]
    assert found([("NET", "net")], "ASP.NET") == [("NET", "net")]
    assert found([("->", "to")], "a->b") == [("->", "to")]
    # the letter edge of a key still needs its break
    assert found([(".NET", "dot net")], "ASP.NETwork") == []
    assert found([("C#", "C sharp")], "C#1 abC#") == [("C#", "C sharp")] * 2
    assert found([("C#", "C sharp")], "ABC#") == []


def test_a_hump_is_an_ascii_lowercase_letter_before_a_capital():
    assert found([("token", "t")], "idToken") == [("Token", "t")]
    assert found([("token", "t")], "IDToken") == []        # D then T: no hump
    assert found([("token", "t")], "1Token") == []
    assert found([("ok", "o k")], "éOk") == []             # not ASCII


def test_a_key_with_a_hump_matches_it_in_the_text():
    assert found([("getId", "get ID")], "x.getId() getIdx xgetId") == [
        ("getId", "get ID")]
    assert found([("userid", "user ID")], "userId USERID") == [
        ("userId", "user ID"), ("USERID", "user ID")]


# ---------------------------------------------------------------------------
# case
# ---------------------------------------------------------------------------


def test_a_key_without_a_capital_matches_any_case():
    assert found([("gif", "jif")], "gif GIF Gif gIF") == [
        ("gif", "jif"), ("GIF", "jif"), ("Gif", "jif"), ("gIF", "jif")]


def test_a_key_with_a_capital_matches_that_case_only():
    assert found([("GIF", "jif")], "gif GIF Gif") == [("GIF", "jif")]
    assert found([("Id", "eye dee")], "id Id ID") == [("Id", "eye dee")]


@pytest.mark.parametrize("pairs", [
    [("GIF", "jif"), ("gif", "gift")],
    [("gif", "gift"), ("GIF", "jif")],
])
def test_an_exact_case_key_beats_an_any_case_key(pairs):
    assert found(pairs, "GIF gif Gif") == [
        ("GIF", "jif"), ("gif", "gift"), ("Gif", "gift")]


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


def test_the_longest_match_wins():
    pairs = [("New York City", "NYC"), ("New York", "the big apple")]
    assert found(pairs, "New York City") == [("New York City", "NYC")]
    assert found(pairs[::-1], "New York City") == [("New York City", "NYC")]
    assert found(pairs, "New York State") == [("New York", "the big apple")]


def test_the_leftmost_match_wins_over_a_longer_one_later():
    pairs = [("NET", "net"), (".NET", "dot net"), ("ASP", "a s p")]
    assert found(pairs, "ASP.NET") == [("ASP", "a s p"), (".NET", "dot net")]
    pairs = [("b c d", "long"), ("a b", "short")]
    assert found(pairs, "a b c d") == [("a b", "short")]


def test_the_later_pair_wins_a_tie():
    assert found([("id", "eye dee"), ("id", "ID")], "id") == [("id", "ID")]
    assert found([("id", "ID"), ("id", "eye dee")], "id") == [
        ("id", "eye dee")]
    assert found([("GIF", "jif"), ("GIF", "gif")], "GIF") == [("GIF", "gif")]


def test_a_key_said_as_written_shields_its_text():
    pairs = [("macOS", "macOS"), ("OS", "O S")]
    assert found(pairs, "macOS and OS") == [("OS", "O S")]
    assert found(pairs, "iOS") == [("OS", "O S")]         # i then O: a hump
    assert found(pairs[::-1], "macOS") == []
    # a lowercase guard keeps every case it matches as written: lowering
    # "macOS" would have misaki say "mah coze"
    assert found([("macos", "macos")], "macOS MacOS macos") == []
    assert found([("os", "O S"), ("macos", "macos")], "macOS and OS") == [
        ("OS", "O S")]
    # text that already says what a real respelling says is left alone too
    assert found([("id", "ID")], "ID id") == [("id", "ID")]


def test_a_respelling_is_never_matched_again():
    lex = Lexicon([("id", "id card"), ("card", "badge")])
    text = "id card"
    assert lex.find(text) == [(0, 2, "id card"), (3, 7, "badge")]
    new, _offsets, texts = respell(text, slots(text, "id", "card"),
                                   lex.find(text))
    assert (new, texts) == ("id card badge", ["id card", "badge"])


# ---------------------------------------------------------------------------
# phrases, punctuation, unicode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "New York City",
    "New  York\tCity",
    "New York\nCity",
    "New York\n    City",
    "New York \n City",
    "New\N{NO-BREAK SPACE}York City",
    # mdcat's blockquote gutter, one level and two
    "New York\n│ City",
    "New York\n│ │ City",
])
def test_a_phrase_matches_any_spacing_with_one_line_break(text):
    assert found([("New York City", "NYC")], text) == [(text, "NYC")]


@pytest.mark.parametrize("text", [
    "New York\n\nCity",
    "New York\n  \n  City",
    "New York\n│ \n│ City",          # two paragraphs of one quote
    "New York │ City",                # a gutter only follows a line break
    "NewYork City",
    "New York-City",
    "New York Cityscape",
])
def test_a_phrase_never_spans_a_paragraph_or_a_missing_space(text):
    assert found([("New York City", "NYC")], text) == []


def test_punctuation_inside_a_key_must_be_there_as_written():
    pairs = [("std::vector", "standard vector")]
    assert found(pairs, "std::vector<int>") == [
        ("std::vector", "standard vector")]
    assert found(pairs, "std: :vector std vector std::Vector") == [
        ("std::Vector", "standard vector")]


@pytest.mark.parametrize("key,text,match", [
    ("C++", "C++ and C", "C++"),
    ("(id)", "call(id) now", "(id)"),
    ("[1]", "see[1] and [2]", "[1]"),
    ("$4", "only $4 today", "$4"),
    ("a.b", "a.b axb", "a.b"),
    ("a|b", "a|b ab", "a|b"),
    ("\\d", "\\d 1", "\\d"),
    ("x*", "x* xx", "x*"),
    ("^$", "^$ $", "^$"),
])
def test_a_key_is_literal_text_never_a_regex(key, text, match):
    assert found([(key, "said")], text) == [(match, "said")]


def test_regex_metacharacters_keep_their_boundaries():
    assert found([("$4", "four dollars")], "$40 $4.50") == [
        ("$4", "four dollars")]
    assert found([("C++", "C plus plus")], "xC++") == [("C++", "C plus plus")]


def test_text_with_decomposed_accents_matches_a_composed_key():
    pairs = [("café", "caff ay")]
    nfd = "cafe" + ACUTE
    assert found(pairs, nfd) == [(nfd, "caff ay")]
    assert found(pairs, "café CAFÉ") == [("café", "caff ay"),
                                         ("CAFÉ", "caff ay")]
    assert found(pairs, "cafe cafés " + nfd + ACUTE + " " + nfd + "s") == []
    assert found(pairs, nfd + "'s") == [(nfd, "caff ay")]


def test_a_decomposed_key_is_composed_first():
    lex = Lexicon([("Re" + ACUTE + "sume" + ACUTE, "resume")])
    assert lex.find("Résumé") == [(0, 6, "resume")]
    assert lex.find("résumé") == []                         # a capital: exact


def test_a_key_led_by_a_decomposable_symbol_matches_both_spellings():
    not_equal = "\N{NOT EQUAL TO}"
    decomposed = unicodedata.normalize("NFD", not_equal)
    assert decomposed != not_equal
    assert found([(not_equal, "is not")], f"a {not_equal} b, a {decomposed} b"
                 ) == [(not_equal, "is not"), (decomposed, "is not")]


# ---------------------------------------------------------------------------
# symbols a table cell says by name
# ---------------------------------------------------------------------------


def test_a_symbols_name_is_never_matched_as_text():
    tick = [(0, 3, "✓")]
    assert found([("yes", "yep")], "yes", tick) == []
    assert found([("yes", "yep")], "yes") == [("yes", "yep")]
    arrow = [(5, 16, "→")]
    assert found([("right arrow", "next")], "dot, right arrow", arrow) == []
    assert found([("arrow", "pointer")], "dot, right arrow", arrow) == []
    assert found([("dot", "period")], "dot, right arrow", arrow) == [
        ("dot", "period")]


def test_a_shorter_match_still_applies_beside_a_name():
    pairs = [("Done", "finished"), ("Done yes", "all done")]
    assert found(pairs, "Done yes", [(5, 8, "✓")]) == [("Done", "finished")]
    assert found(pairs, "Done yes") == [("Done yes", "all done")]


def test_an_entry_for_the_symbol_replaces_the_whole_name():
    assert found([("✓", "check")], "yes (partial)", [(0, 3, "✓")]) == [
        ("yes", "check")]
    text = "left arrow back, right arrow next"
    names = [(0, 10, "←"), (17, 28, "→")]
    assert found([("→", "to"), ("next", "then")], text, names) == [
        ("right arrow", "to"), ("next", "then")]


@pytest.mark.parametrize("key,symbol", [
    ("✔", "✔" + VS16),
    ("✔" + VS16, "✔"),
    ("✔" + VS15, "✔" + VS16),
    ("✔", "✔"),
])
def test_variation_selectors_do_not_tell_symbols_apart(key, symbol):
    assert found([(key, "tick")], "yes", [(0, 3, symbol)]) == [("yes", "tick")]


def test_a_symbol_entry_that_says_the_name_changes_nothing():
    assert found([("✓", "yes"), ("yes", "yep")], "yes", [(0, 3, "✓")]) == []


def test_the_later_symbol_entry_wins():
    pairs = [("✓", "check"), ("✓" + VS16, "tick")]
    assert found(pairs, "yes", [(0, 3, "✓")]) == [("yes", "tick")]


# ---------------------------------------------------------------------------
# respell
# ---------------------------------------------------------------------------


def test_respell_without_matches_keeps_everything():
    text = "foo.id  and\n  more"
    spans = slots(text, "foo.id", "and", "more")
    assert respell(text, spans, []) == (text, [0, 8, 14],
                                        ["foo.id", "and", "more"])


def test_a_slot_keeps_what_lies_outside_the_match():
    assert said([("id", "ID")], "foo.id", "foo.id") == ("foo.ID", ["foo.ID"])
    assert said([("id", "ID")], "(id)", "id") == ("(ID)", ["ID"])
    assert said([("id", "ID")], "user_id_list", "user_id_list") == (
        "user_ID_list", ["user_ID_list"])


def test_one_slot_says_the_whole_respelling():
    assert said([("kubectl", "cube control")], "kubectl apply",
                "kubectl", "apply") == ("cube control apply",
                                        ["cube control", "apply"])
    assert said([("(id)", "paren I D")], "(id)", "id") == (
        "paren I D", ["paren I D"])


def test_more_slots_than_words_cut_the_last_word():
    pairs = [("New York City", "NYC")]
    assert said(pairs, "New York\nCity", "New", "York", "City") == (
        "NYC", ["N", "Y", "C"])
    assert said([("id card", "badge")], "id\ncard", "id", "card") == (
        "badge", ["bad", "ge"])
    assert said([("a b c", "go to it")], "a b c", "a", "b", "c") == (
        "go to it", ["go", "to", "it"])
    assert said([("a b c d", "go ahead")], "a b c d", "a", "b", "c", "d") == (
        "go ahead", ["go", "ah", "ea", "d"])


def test_slots_left_without_a_character_say_a_space():
    assert said([("A B C D", "Q")], "A B C D", "A", "B", "C", "D") == (
        "Q   ", ["Q", " ", " ", " "])
    assert said([("A B C", "Q")], "A B C.", "A", "B", "C") == (
        "Q  .", ["Q", " ", " "])


def test_more_words_than_slots_leave_the_rest_to_the_last_slot():
    assert said([("New York", "the big apple")], "New York-City",
                "New", "York-City") == ("the big apple-City",
                                        ["the", "big apple-City"])
    assert said([("NYC", "New York City")], "NYC", "NYC") == (
        "New York City", ["New York City"])


def test_a_space_keeps_a_respelling_off_a_letter_or_digit():
    assert said([(".NET", "dot net")], "ASP.NET", "ASP.NET") == (
        "ASP dot net", ["ASP dot net"])
    assert said([("->", "to")], "a->b", "a", "b") == ("a to b", ["a", "b"])
    assert said([("C#", "C sharp")], "C#1", "C#1") == ("C sharp 1",
                                                       ["C sharp 1"])
    assert said([("id", "ID")], "getId", "getId") == ("get ID", ["get ID"])
    # none where the neighbour is not a letter or digit
    assert said([("->", "to")], "a -> b", "a", "b") == ("a to b", ["a", "b"])
    assert said([("->", "=>")], "a->b", "a", "b") == ("a=>b", ["a", "b"])


def test_adjacent_matches_get_exactly_one_space():
    pairs = [("user", "yoozer"), ("id", "ID")]
    assert said(pairs, "userId", "userId") == ("yoozer ID", ["yoozer ID"])
    assert said(pairs, "user_id", "user_id") == ("yoozer_ID", ["yoozer_ID"])
    assert said([("a", "x"), ("->", "to"), ("b", "y")], "a->b", "a", "b") == (
        "x to y", ["x", "y"])
    # the space depends on the next respelling, not on the text it replaces
    assert said([("user", "yoozer"), ("id", "(ID)")], "userId", "userId") == (
        "yoozer(ID)", ["yoozer(ID)"])


def test_a_match_covering_no_slot_only_moves_the_text_after_it():
    text = "a -> b -> c"
    spans = slots(text, "a", "b", "c")
    matches = Lexicon([("->", "leads to")]).find(text)
    new, offsets, texts = respell(text, spans, matches)
    assert (new, texts) == ("a leads to b leads to c", ["a", "b", "c"])
    assert offsets == [0, 11, 22]


def test_a_match_inside_one_slot_between_two_others():
    text = "x foo.id.bar y"
    new, texts = said([("id", "I D")], text, "x", "foo.id.bar", "y")
    assert (new, texts) == ("x foo.I D.bar y", ["x", "foo.I D.bar", "y"])


def test_a_symbol_forced_match_says_the_entry():
    text = "yes (partial)"
    spans = slots(text, "yes", "partial")
    matches = Lexicon([("✓", "check"), ("yes", "yep")]).find(
        text, [(0, 3, "✓")])
    assert respell(text, spans, matches) == ("check (partial)", [0, 7],
                                             ["check", "partial"])
    text = "dot, right arrow"
    spans = slots(text, "dot", "right arrow")
    matches = Lexicon([("→", "to")]).find(text, [(5, 16, "→")])
    assert respell(text, spans, matches) == ("dot, to", [0, 5], ["dot", "to"])


# ---------------------------------------------------------------------------
# property: respell keeps every slot usable
# ---------------------------------------------------------------------------

TOKENS = ["id", "Id", "ID", "user", "foo", "a", "b", "New", "York", "City",
          "é", "e" + ACUTE, "1", "42", "✓", "→", "yes", "_", ".", "-", "->",
          "::", "#", "(", ")", " ", " ", "  ", "\n", "\n  ", "\t", "="]
KEYS = ["id", "ID", "user", "userid", "foo.id", "New York", "New York City",
        "a b", "->", ".", "(id)", "é", "e" + ACUTE, "yes", "✓", "→", "42",
        "::", "#", "b", "a", "-", "=", "York City"]
SAYS = ["ID", "I D", "yoozer", "NYC", "the big apple", "to", "dot", "x",
        "check", "a b c d e", "1", "==", "--x--"]


def random_slots(rng: random.Random, text: str) -> list[tuple[int, int]]:
    """Random disjoint non-empty spans: words, pieces of words, whole runs."""
    cuts = sorted(rng.sample(range(len(text) + 1),
                             min(len(text) + 1, rng.randint(0, 12))))
    spans = []
    for a, b in zip(cuts, cuts[1:]):
        if b > a and rng.random() < 0.7:
            spans.append((a, b))
    return spans


def test_random_respellings_keep_every_slot_guarantee():
    rng = random.Random(20260911)
    for _ in range(3000):
        text = "".join(rng.choice(TOKENS) for _ in range(rng.randint(0, 14)))
        pairs = [(rng.choice(KEYS),
                  rng.choice(SAYS + KEYS) if rng.random() < 0.9
                  else rng.choice(KEYS))
                 for _ in range(rng.randint(0, 6))]
        spans = random_slots(rng, text)
        names = [(a, b, rng.choice(["✓", "✓" + VS16, "→"]))
                 for a, b in spans if rng.random() < 0.2]
        matches = Lexicon(pairs).find(text, names)
        check_respelled(text, spans, matches, respell(text, spans, matches))


def test_the_slots_of_a_phrase_share_every_character_of_its_respelling():
    rng = random.Random(7)
    words = ["alpha", "beta", "gamma", "delta"]
    for _ in range(300):
        k = rng.randint(1, 4)
        key = " ".join(words[:k])
        say = " ".join(rng.choice(["x", "yy", "zzz"])
                       for _ in range(rng.randint(1, 5)))
        text = "\n".join(words[:k])
        new, texts = said([(key, say)], text, *words[:k])
        assert new.rstrip(" ") == say
        joined = "".join(texts)
        assert joined.replace(" ", "") == say.replace(" ", "")


# ---------------------------------------------------------------------------
# differential: find agrees with a slow matcher that is obviously right
# ---------------------------------------------------------------------------


def _break(text: str, i: int) -> bool:
    """Whether a word may start or end at `i`."""
    if i == 0 or i == len(text):
        return True
    left, right = text[i - 1], text[i]
    return (not alnum(left) or not alnum(right)
            or (left in string.ascii_lowercase
                and right in string.ascii_uppercase))


def _match_at(form: str, exact: bool, text: str, p: int) -> int | None:
    """Where `form` ends when it matches `text` at `p`, else None."""
    if alnum(form[0]) and not _break(text, p):
        return None
    i = p
    for n, part in enumerate(form.split()):
        if n:
            j, breaks = i, 0
            # blanks, one line break, and after it a blockquote's │ gutter
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
    if alnum(form[-1]) and not _break(text, i):
        return None
    return i


def reference_find(pairs, text: str, symbols=()) -> list[tuple[int, int, str]]:
    """`Lexicon.find` the slow way: every key at every position."""
    entries = []
    for rank, (written, spoken) in enumerate(pairs):
        key = " ".join(unicodedata.normalize("NFC", written).split())
        say = " ".join(spoken.split())
        if key and say:
            entries.append((key, say, rank, any(ch.isupper() for ch in key)))
    out = []
    p = 0
    while p < len(text):
        best = None
        for key, say, rank, exact in entries:
            for form in (key, unicodedata.normalize("NFD", key)):
                end = _match_at(form, exact, text, p)
                if end is None or any(p < b and a < end
                                      for a, b, _symbol in symbols):
                    continue
                if best is None or (end, exact, rank) > best[0]:
                    best = ((end, exact, rank), say, key)
        if best is None:
            p += 1
            continue
        end, say, key = best[0][0], best[1], best[2]
        if key != say and text[p:end] != say:
            out.append((p, end, say))
        p = end
    bare = str.maketrans("", "", VS15 + VS16)
    for a, b, symbol in symbols:
        says = [(rank, say) for key, say, rank, _exact in entries
                if key.translate(bare) == symbol.translate(bare)]
        if says and text[a:b] != max(says)[1]:
            out.append((a, b, max(says)[1]))
    return sorted(out)


# Left out: İ, ß and ǅ change length or case class when lowered, and ſ, K
# (the kelvin sign) and ı are case variants to the regex engine but not to
# str.lower, which the index looks keys up with.  Neither is a case variant
# the reader needs to find.
DIFF_TEXT = ["a", "b", "A", "B", "i", "d", "I", "D", "x", "X", "1", "_", ".",
             "-", "#", "+", "(", ")", "=", " ", "  ", "\n", "\t",
             "\N{NO-BREAK SPACE}", "│",
             "é", "É", "e" + ACUTE, ACUTE, "✓", VS16, "ab", "Id", "id", "iD"]
DIFF_KEY = ["a", "b", "A", "B", "i", "d", "I", "D", "x", "1", "_", ".", "-",
            "#", "+", "(", ")", "=", " ", "é", "É", "e" + ACUTE, ACUTE, "✓",
            VS16, "id", "ab", "Id"]


def test_find_agrees_with_the_reference_matcher():
    rng = random.Random(20260912)
    checked = matched = 0
    for _ in range(4000):
        text = "".join(rng.choice(DIFF_TEXT)
                       for _ in range(rng.randint(0, 24)))
        pairs = []
        for _ in range(rng.randint(1, 6)):
            if text.strip() and rng.random() < 0.5:     # a piece of the text
                a = rng.randrange(len(text))
                key = text[a:a + rng.randint(1, 6)]
            else:
                key = "".join(rng.choice(DIFF_KEY)
                              for _ in range(rng.randint(1, 4)))
            say = rng.choice(["X", "yes", "a b", "ID", key, key.lower()])
            pairs.append((key, say))
        if rng.random() < 0.3:
            pairs.append((rng.choice(["✓", "✓" + VS16]),
                          rng.choice(["X", "yes"])))
        cuts = sorted(rng.sample(range(len(text) + 1),
                                 min(len(text) + 1, 2 * rng.randint(0, 2))))
        symbols = [(a, b, rng.choice(["✓", "✓" + VS16, "x"]))
                   for a, b in zip(cuts[::2], cuts[1::2]) if a < b]
        want = reference_find(pairs, text, symbols)
        assert Lexicon(pairs).find(text, symbols) == want, (pairs, text,
                                                            symbols)
        checked += 1
        matched += bool(want)
    # the random lexicons really do match something often
    assert matched > checked // 4


def test_find_agrees_with_the_reference_on_realistic_names():
    rng = random.Random(3)
    names = ["id", "userId", "user_id", "foo.id", "getID", "IDs", "idle",
             "New York", "kubectl", "ASP.NET", "std::vector", "C#", "macOS",
             "OS", "a->b", "==", "GIF", "gif", "Résumé", "café"]
    keys = ["id", "ID", "ids", "user", "New York", "kubectl", ".NET", "NET",
            "std::vector", "C#", "macOS", "OS", "->", "==", "GIF", "résumé",
            "café", "York", "get"]
    for _ in range(500):
        text = "".join(rng.choice(names) + rng.choice([" ", "\n", ", ", "."])
                       for _ in range(rng.randint(1, 12)))
        pairs = [(key, rng.choice(["x y", key, "Z"]))
                 for key in rng.sample(keys, rng.randint(1, len(keys)))]
        assert Lexicon(pairs).find(text) == reference_find(pairs, text), (
            pairs, text)


# ---------------------------------------------------------------------------
# performance
# ---------------------------------------------------------------------------


def best_time(fn, *args, runs: int = 3) -> float:
    """The fastest of `runs` calls: one slow run on a busy machine is noise."""
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn(*args)
        best = min(best, time.perf_counter() - start)
    return best


def test_find_is_fast_with_many_keys_over_a_long_text():
    rng = random.Random(500)

    def word() -> str:
        return "".join(rng.choice(string.ascii_lowercase)
                       for _ in range(rng.randint(2, 9)))

    vocab = [word() for _ in range(3000)]
    pairs = []
    for _ in range(500):
        shape = rng.randrange(5)
        key = rng.choice(vocab)
        if shape == 1:
            key = key.capitalize()
        elif shape == 2:
            key += " " + rng.choice(vocab)
        elif shape == 3:
            key = "." + key
        elif shape == 4:
            key += "::" + rng.choice(vocab)
        pairs.append((key, word() + " " + word()))
    parts, size = [], 0
    while size < 300_000:
        name = rng.choice(vocab)
        glue = rng.random()
        if glue < 0.1:
            name += rng.choice(vocab).capitalize()
        elif glue < 0.2:
            name += rng.choice("._") + rng.choice(vocab)
        sep = rng.choice([" ", " ", " ", "\n", ", ", ".\n\n", "  "])
        parts.append(name + sep)
        size += len(name) + len(sep)
    text = "".join(parts)
    lex = Lexicon(pairs)
    assert len(lex.find(text)) > 1000
    assert best_time(lex.find, text) < 2.0


def test_find_is_fast_on_hostile_text():
    long_key = "aB" * 40
    lex = Lexicon([(long_key, "x"), ("ab", "y"), ("Ba", "z"), ("a b", "w"),
                   ("New York City", "NYC"), (".NET", "dot net")])
    for text in ("aB" * 50_000, " " * 100_000 + "x", "New " * 25_000,
                 "." * 100_000, "a\n" * 50_000):
        assert best_time(lex.find, text, runs=2) < 2.0
