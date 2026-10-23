"""Compatibility import for planetbaltan.protocol."""
import importlib
import sys

_module = importlib.import_module("planetbaltan.protocol")
sys.modules[__name__] = _module
