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
