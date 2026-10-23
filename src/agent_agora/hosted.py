"""Compatibility import for planetbaltan.hosted."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.hosted")
sys.modules[__name__] = _module
