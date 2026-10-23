"""Compatibility import for planetbaltan.sdk."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.sdk")
sys.modules[__name__] = _module
