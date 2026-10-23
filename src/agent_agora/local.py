"""Compatibility import for planetbaltan.local."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.local")
sys.modules[__name__] = _module
