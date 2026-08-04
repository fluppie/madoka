"""Test-only helper for importing this integration's dependency-free modules
(decoder.py, helpers.py, framer.py) in isolation.

Those modules only use package-relative imports (e.g. `from .helpers import
...`), which normally requires importing them through the real `madoka`
package - but that package's __init__.py pulls in Home Assistant, which
we don't want as a test dependency just to exercise pure parsing/framing
logic. Instead, register a lightweight stand-in `madoka` package (pointed at
this repo) without ever executing the real __init__.py, then load modules
as submodules of that stand-in.
"""
import importlib.util
import pathlib
import sys
import types

_ROOT = pathlib.Path(__file__).resolve().parent.parent

if "madoka" not in sys.modules:
    _pkg = types.ModuleType("madoka")
    _pkg.__path__ = [str(_ROOT)]
    sys.modules["madoka"] = _pkg


def load(name):
    """Import madoka.<name> (e.g. load("decoder")) without running the real
    madoka/__init__.py, and return the loaded module."""
    full_name = f"madoka.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    spec = importlib.util.spec_from_file_location(full_name, _ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "madoka"
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module
