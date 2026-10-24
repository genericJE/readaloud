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
import re
from collections.abc import Iterable
from pathlib import Path

from . import _core

__all__ = ["Grammar", "GrammarError", "LANGUAGES", "check_rules", "from_file",
           "from_module", "grammar_for", "language_for", "outline"]

Grammar = _core.Grammar

#: The grammar a file suffix usually wants: the name after ``tree_sitter_``
#: in the wheel's module, and of Neovim's ``parser/<name>.so``.  The
#: ``language`` setting under ``[config.<ext>]`` overrides it.
LANGUAGES = {
    ".py": "python", ".pyi": "python", ".rs": "rust", ".js": "javascript",
    ".mjs": "javascript", ".ts": "typescript", ".tsx": "tsx", ".go": "go",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".lua": "lua", ".toml": "toml", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".html": "html", ".css": "css", ".md": "markdown",
    ".sql": "sql", ".hs": "haskell", ".ex": "elixir", ".exs": "elixir",
    ".zig": "zig", ".nix": "nix",
}


def language_for(suffix: str, override: str = "") -> str | None:
    """The grammar's name for a file `suffix` (``.py``), `override` first,
    or None when readaloud has no idea."""
    if override.strip():
        return override.strip().lower().replace("-", "_")
    return LANGUAGES.get(suffix.lower())


def grammar_for(language: str, parsers: str | Path = "") -> Grammar:
    """The grammar called `language`: from the ``tree-sitter-<language>``
    wheel, or else from ``<parsers>/<language>.so`` (``.dylib`` too) when a
    `parsers` directory is given.

    :raises GrammarError: with every place that was tried and why it failed.
    """
    try:
        return from_module(language)
    except GrammarError as from_wheel:
        reasons = [str(from_wheel)]
    if str(parsers).strip():
        folder = Path(parsers).expanduser()
        for name in (f"{language}.so", f"{language}.dylib"):
            path = folder / name
            if path.is_file():
                try:
                    return from_file(path, language)
                except GrammarError as from_path:
                    reasons.append(str(from_path))
                    break
        else:
            reasons.append(f"no {language}.so in {folder}")
    raise GrammarError("; ".join(reasons))


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


_REFERENCE_RE = re.compile(r"\$\$|\$\{([^}|]*)(?:\|[^}|]*)*\}|\$(\w+|\*)")


def check_rules(rules: Iterable[tuple[str, str]], grammar: Grammar,
                section: str = "syntax") -> list[str]:
    """What `grammar` makes of the ``[syntax]`` `rules`, ``(key, wording)``:
    a problem per rule whose node kind, parent kind or field the grammar does
    not have, each starting ``[section]``.  Such a rule is never met, so it
    costs nothing but the warning."""
    kinds = set(grammar.node_kinds())
    fields = set(grammar.field_names())
    problems = []
    for key, wording in rules:
        kind, dot, field = key.partition(".")
        names = [(k, True) for k in kind.split("/")] + ([(field, False)] if dot else [])
        for name, is_kind in names:
            if is_kind and name not in kinds:
                problems.append(f"[{section}] {key}: no node is a {name!r}")
                break
            if not is_kind and name not in fields:
                problems.append(f"[{section}] {key}: no node has a field {name!r}")
                break
        else:
            for m in _REFERENCE_RE.finditer(wording):
                name = m.group(1) if m.group(1) is not None else m.group(2)
                if name and name not in ("0", "*") and name not in fields:
                    problems.append(f"[{section}] {key}: no node has a field "
                                    f"{name!r}")
                    break
    return problems


def outline(text: str, grammar: Grammar) -> str:
    """`text`'s syntax tree, one node per line with its kind, byte range and
    field name: for looking at what a grammar makes of some text."""
    return _core.syntax_tree(text, grammar)
