"""Compatibility import for planetbaltan.auth."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.auth")
sys.modules[__name__] = _module
