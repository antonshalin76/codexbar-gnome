from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "bin" / "codexbar-gnome-indicator"


def load_indicator(name: str = "codexbar_gnome_indicator"):
    spec = importlib.util.spec_from_loader(
        name, SourceFileLoader(name, str(MODULE_PATH))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
