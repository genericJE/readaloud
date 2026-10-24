"""Tests for `readaloud.syntax`: the tree-sitter runtime with a grammar loaded
at run time.  The Python grammar comes from the tree-sitter-python wheel, a
dev dependency; readaloud itself ships none.
"""

from __future__ import annotations

import ctypes
import pathlib

import pytest

from readaloud import syntax
from readaloud.syntax import Grammar, GrammarError

tree_sitter_python = pytest.importorskip("tree_sitter_python")


@pytest.fixture(scope="module")
def python() -> Grammar:
    return syntax.from_module("python")


def test_the_wheel_gives_a_grammar_the_runtime_accepts(python):
    assert 13 <= python.abi_version <= 15
    kinds = python.node_kinds()
    assert {"module", "function_definition", "parameters", "typed_parameter",
            "typed_default_parameter", "binary_operator", "type"} <= set(kinds)
    # named kinds first, then the anonymous ones
    assert kinds.index("function_definition") < kinds.index("def")
    assert {"name", "parameters", "return_type", "type", "value"} <= set(
        python.field_names())


def test_a_def_is_a_tree_with_fields_and_byte_ranges(python):
    text = "def foo(bar: int, baz: str='') -> str:\n    pass\n"
    tree = syntax.outline(text, python)
    assert tree.startswith("module [0..")
    assert "  function_definition [0..47]\n" in tree
    assert '    name: identifier [4..7]  "foo"\n' in tree
    assert "      typed_default_parameter [18..29]\n" in tree
    assert "        value: string [27..29]\n" in tree
    assert "    return_type: type [34..37]\n" in tree
    assert " !" not in tree


def test_a_union_type_is_a_binary_operator_under_a_type(python):
    tree = syntax.outline("x: str | None = 3\nr = a | b\n", python)
    assert "      type: type [3..13]\n        binary_operator [3..13]\n" in tree
    assert "      right: binary_operator [22..27]\n" in tree


def test_a_cut_off_signature_is_an_error_node_with_its_parts(python):
    tree = syntax.outline("def foo(bar: int,\n    baz: str", python)
    assert "  ERROR [0..30] !\n" in tree
    assert tree.count("typed_parameter [") == 2


def test_a_missing_wheel_says_what_to_install():
    with pytest.raises(GrammarError) as err:
        syntax.from_module("nothing_at_all")
    assert str(err.value) == (
        "no grammar for nothing_at_all: install the tree-sitter-nothing-at-all "
        "package next to readaloud (uv tool install readaloud --with "
        "tree-sitter-nothing-at-all)")


def test_only_a_language_capsule_is_a_grammar():
    with pytest.raises(TypeError, match="tree_sitter.Language capsule"):
        Grammar.from_capsule(object())
    new = ctypes.pythonapi.PyCapsule_New
    new.restype = ctypes.py_object
    new.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
    other = new(ctypes.c_void_p(8), b"something.else", None)
    with pytest.raises(TypeError, match="tree_sitter.Language capsule"):
        Grammar.from_capsule(other)


def test_a_library_without_the_grammar_is_refused():
    with pytest.raises(GrammarError, match="cannot load /nonexistent/x.so"):
        syntax.from_file("/nonexistent/x.so", "python")
    # the wheel's own binary keeps tree_sitter_python to itself
    binding = next(pathlib.Path(tree_sitter_python.__file__).parent.glob("_binding*"))
    with pytest.raises(GrammarError, match='no grammar called "python"'):
        syntax.from_file(binding, "python")


@pytest.mark.parametrize("suffix, override, language", [
    (".py", "", "python"), (".PY", "", "python"), (".rs", "", "rust"),
    (".txt", "", None), (".txt", "Tree-Sitter", "tree_sitter"),
    (".py", " rust ", "rust"),
])
def test_the_language_comes_from_the_suffix_unless_set(suffix, override, language):
    assert syntax.language_for(suffix, override) == language


def test_grammar_for_tries_the_wheel_and_then_the_parsers_directory(tmp_path):
    assert syntax.grammar_for("python").abi_version >= 13
    with pytest.raises(GrammarError) as err:
        syntax.grammar_for("nothing_at_all")
    assert str(err.value).startswith("no grammar for nothing_at_all: install")
    assert "; " not in str(err.value)          # no directory, so one reason
    with pytest.raises(GrammarError) as err:
        syntax.grammar_for("nothing_at_all", tmp_path)
    assert str(err.value).endswith(f"; no nothing_at_all.so in {tmp_path}")
    (tmp_path / "nothing_at_all.so").write_bytes(b"not a library")
    with pytest.raises(GrammarError) as err:
        syntax.grammar_for("nothing_at_all", tmp_path)
    assert f"; cannot load {tmp_path / 'nothing_at_all.so'}" in str(err.value)


RULES = [
    ("function_definition", "function $name $parameters $return_type $body"),
    ("function_definition.return_type", "and returns type $0"),
    ("parameters", "takes ${*|,=and}"),
    ("typed_parameter", "${*|:=type}"),
    ("typed_default_parameter", "$name type $type with a default of $value"),
    ("type/binary_operator", "$left or $right"),
    ("list_splat_pattern", "${0|*=star}"),
]
PAIRS = [("str", "string"), ("''", "empty string")]


def read(text: str, python, rules=RULES, pairs=PAIRS):
    """Each speakable chunk's text and ``(word on screen, what it says)``."""
    from readaloud.document import Document
    from readaloud.pronounce import Lexicon

    doc = Document.from_text(text, pronunciations=Lexicon(pairs, (), (), python, rules))
    assert doc.respell_failures == 0
    return [(c.text, list(zip([doc.words[w].text for w in c.words], c.word_texts)))
            for c in doc.chunks if c.speakable]


def test_rules_say_a_def_and_light_the_words_they_came_from(python):
    (chunk,) = read("def foo(bar: int, baz: str='') -> str:\n    pass\n", python)
    # the line break before the body is kept
    assert chunk[0] == ("function foo takes bar type int and baz type string "
                        "with a default of empty string and returns type string\npass")
    assert chunk[1] == [
        ("def", "function"), ("foo", "foo"), ("bar", "takes bar"),
        ("int", "type int"), ("baz", "and baz"),
        ("str", "type string with a default of empty string"),
        ("str", "and returns type string"), ("pass", "pass")]


def test_an_ancestor_rule_reaches_every_node_under_it(python):
    # both binary_operators of "str | Path | None" are under the type
    (chunk,) = read("def for_file(self, path: str | Path | None) -> Config:\n"
                    "    return self\n", python)
    assert chunk[0] == ("function for_file takes self and path type string or "
                        "Path or None and returns type Config\nreturn self")
    assert chunk[1][2:8] == [
        ("self", "takes self"), ("path", "and path"), ("str", "type string"),
        ("Path", "or Path"), ("None", "or None"),
        ("Config", "and returns type Config")]


def test_the_whole_file_is_parsed_so_a_method_in_a_class_is_found(python):
    chunks = read("class A:\n    x = 1\n\n    def m(self, *args) -> None:\n"
                  "        pass\n\nr1 = a | b\n", python)
    assert [c[0] for c in chunks] == [
        "class A:\n    x = 1",
        "    function m takes self and star args and returns type None\npass",
        "r1 = a | b"]                          # an operator, not a type: as is


def test_a_def_whose_body_runs_past_the_chunk_is_said_as_far_as_it_goes(python):
    body = "".join(f"    x{i} = {i}\n" for i in range(60))      # well past 380 chars
    chunks = read("def f(a: int) -> str:\n" + body + "\n\nclass B:\n    pass\n", python)
    first = chunks[0][0]
    assert first.startswith("function f takes a type int and returns type string\nx0 = 0\nx1 = 1\n")
    assert "function" not in "".join(c[0] for c in chunks[1:])
    assert chunks[-1][0] == "class B:\n    pass"


def test_replacements_stay_at_their_level_and_comments_are_no_parts(python):
    (chunk,) = read("def f(a: dict[str, int],  # which\n      b=2) -> None:\n    pass\n",
                    python, RULES + [("default_parameter", "$name defaulting to $value")])
    # the comma inside dict[...] is not a parameter's comma, and the comment
    # is not a parameter (its line break stays, a pause)
    assert chunk[0] == ("function f takes a type dict[string, int] and\nb defaulting "
                        "to 2 and returns type None\npass")


def test_a_wording_word_is_kept_apart_from_punctuation_but_text_is_not(python):
    rules = [("attribute", "$object dot $attribute")]
    (chunk,) = read('x = foo().bar().value\ny = f(a, "x.y")\nz = Sequence[str]\n',
                    python, rules)
    # "dot" gets its spaces; the string and the brackets keep their text as is
    assert chunk[0] == ('x = foo() dot bar() dot value\ny = f(a, "x.y")\n'
                        'z = Sequence[string]')
    assert ("value", "dot value") in chunk[1] and ("x.y", "x.y") in chunk[1]


def test_an_empty_rule_says_the_node_as_written_and_claims_it(python):
    (chunk,) = read("def f(a='x'):\n    pass\n", python, [("string", "")],
                    pairs=[("x", "ex"), ("f", "eff")])
    # the string is left alone, while the entry for f still applies
    assert chunk[0] == "def eff(a='x'):\n    pass"


def test_check_rules_names_what_the_grammar_lacks(python):
    problems = syntax.check_rules([
        ("function_definition", "function $name $parameters"),
        ("typed_param", "x"),
        ("type/binop", "y"),
        ("function_definition.returns", "z"),
        ("parameters", "takes ${*|,=and} $nothing $0"),
    ], python, "syntax.py")
    assert problems == [
        "[syntax.py] typed_param: no node is a 'typed_param'",
        "[syntax.py] type/binop: no node is a 'binop'",
        "[syntax.py] function_definition.returns: no node has a field 'returns'",
        "[syntax.py] parameters: no node has a field 'nothing'",
    ]


def test_the_wheel_ships_a_highlights_query():
    query = syntax.highlights_for("python")
    assert query and "@keyword" in query and "@comment" in query
    assert syntax.highlights_for("nothing_at_all") is None


def test_highlight_gives_each_stretch_its_capture(python):
    text = "def f(x):\n    return 'a'  # c\n"
    names = [name for name, _style in syntax.PALETTE]
    spans = python.highlight(text, syntax.highlights_for("python"), names)
    by_text = {text[s:e]: names[n] for s, e, n in spans}
    assert by_text["def"] == "keyword" and by_text["return"] == "keyword"
    assert by_text["f"] == "function"
    assert by_text["'a'"] == "string"
    assert by_text["# c"] == "comment"
    assert "x" not in by_text                       # a variable: as it is


def test_a_broken_query_is_a_grammar_error(python):
    with pytest.raises(GrammarError, match="the highlights query cannot be used"):
        syntax.highlighter(python, "(nothing_at_all) @keyword")


def test_coloured_splits_the_runs_where_the_captures_are(python):
    from readaloud import ansi

    lines = ansi.parse("def f(x):\n    return 'a'  # c\n")
    colour = syntax.highlighter(python, syntax.highlights_for("python"))
    painted = colour(lines)
    assert [(r.text, r.style.fg, r.style.italic) for r in painted[0]] == [
        ("def", 5, False), (" ", None, False), ("f", 4, False),
        ("(x):", None, False)]
    assert [(r.text, r.style.fg, r.style.italic) for r in painted[1]] == [
        ("    ", None, False), ("return", 5, False), (" ", None, False),
        ("'a'", 2, False), ("  ", None, False), ("# c", 8, True)]
    # the text is untouched
    assert ["".join(r.text for r in line) for line in painted] == [
        "".join(r.text for r in line) for line in lines]


def test_a_neovim_parser_file_loads_when_there_is_one():
    candidates = [
        pathlib.Path.home() / ".local/share/nvim/lazy/nvim-treesitter/parser/python.so",
        pathlib.Path.home() / ".local/share/nvim/site/parser/python.so",
    ]
    found = next((p for p in candidates if p.exists()), None)
    if found is None:
        pytest.skip("no nvim-treesitter python parser on this machine")
    grammar = syntax.from_file(found, "python")
    assert "function_definition" in grammar.node_kinds()
