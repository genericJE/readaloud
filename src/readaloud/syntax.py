"""Source code as a syntax tree, with the grammar found at run time.

readaloud ships the tree-sitter runtime and no grammar.  A grammar comes from
one of two places, neither of which needs a compiler:

* the ``tree-sitter-<language>`` wheel on PyPI (``uv tool install readaloud
  --with tree-sitter-rust``): its module ``tree_sitter_<language>`` hands the
  grammar out as a capsule, see :func:`from_module`;
* a ``parser/<language>.so`` as Neovim's ``:TSInstall`` builds them, see
  :func:`from_file`.

Either way the runtime checks the grammar's ABI version first, as Neovim does.
A grammar that cannot be used raises :class:`GrammarError`, whose message says
what to install; nothing here is needed to read text.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from . import _core

__all__ = ["Grammar", "GrammarError", "from_file", "from_module", "outline"]

Grammar = _core.Grammar


class GrammarError(Exception):
    """No usable grammar: the message says why and what to install."""


def from_module(language: str) -> Grammar:
    """The grammar of ``tree_sitter_<language>``, the PyPI wheel's module.

    :raises GrammarError: when the module is not installed, has no
        ``language()``, or hands out a grammar the runtime cannot use.
    """
    name = f"tree_sitter_{language}"
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        raise GrammarError(
            f"no grammar for {language}: install the tree-sitter-"
            f"{language.replace('_', '-')} package next to readaloud "
            f"(uv tool install readaloud --with tree-sitter-"
            f"{language.replace('_', '-')})") from exc
    try:
        capsule = module.language()
        return Grammar.from_capsule(capsule)
    except (AttributeError, TypeError, ValueError) as exc:
        raise GrammarError(f"{name} holds no grammar readaloud can use: {exc}") from exc


def from_file(path: str | Path, language: str) -> Grammar:
    """The grammar the shared library `path` exports as
    ``tree_sitter_<language>``, as a Neovim ``parser/<language>.so`` does.

    :raises GrammarError: when the file cannot be loaded, has no such grammar,
        or hands out one the runtime cannot use.
    """
    try:
        return Grammar.load(str(path), language)
    except ValueError as exc:
        raise GrammarError(str(exc)) from exc


def outline(text: str, grammar: Grammar) -> str:
    """`text`'s syntax tree, one node per line with its kind, byte range and
    field name: for looking at what a grammar makes of some text."""
    return _core.syntax_tree(text, grammar)
