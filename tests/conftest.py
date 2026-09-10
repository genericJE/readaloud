"""Keep the suite away from the developer's real ``~/.readaloud.conf``.

`cli` now loads that file for every run and calls `config.ensure` on startup, so
without this fixture ``pytest`` would (a) inherit whatever speed and voice the
person running it happens to prefer and (b) *create* a config file in their home
directory as a side effect of running the tests.

Every test gets its own empty sandbox path instead: no file there, so `load`
returns plain defaults, and anything `ensure` writes lands in ``tmp_path``.
Tests that want a config file write one and point `--config` (or
`config.DEFAULT_PATH`) at it explicitly.

Subprocesses do not see this -- the pty tests give their child a temporary
``HOME`` of its own.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from readaloud import config as _config  # noqa: E402


@pytest.fixture(autouse=True)
def _sandbox_config_path(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "DEFAULT_PATH", tmp_path / "sandbox.readaloud.conf")
