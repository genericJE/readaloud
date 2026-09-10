"""readaloud — terminal read-aloud with Kokoro TTS.

Chunk text, speak it in a realistic voice, highlight the word you are hearing,
and navigate the viewport with `less` keys while playback carries on.

Only `__version__` is exported eagerly.  Everything else is imported lazily by
attribute so that ``import readaloud`` stays cheap: pulling in `speech` drags
mlx/torch/spaCy along with it, and `player` opens PortAudio.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__ = "1.0.1"

__all__ = [
    "__version__",
    "ansi",
    "app",
    "cli",
    "document",
    "keys",
    "player",
    "speech",
    "ui",
    "main",
]

_SUBMODULES = frozenset(
    {"ansi", "app", "cli", "document", "keys", "player", "speech", "ui"}
)

if TYPE_CHECKING:  # pragma: no cover - for type checkers only
    from . import ansi, app, cli, document, keys, player, speech, ui
    from .cli import main


def __getattr__(name: str):
    import importlib

    if name in _SUBMODULES:
        return importlib.import_module(f".{name}", __name__)
    if name == "main":
        return importlib.import_module(".cli", __name__).main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
